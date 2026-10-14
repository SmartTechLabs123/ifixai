"""Execute declared paired interventions with fresh native providers and fixed probes."""
from __future__ import annotations

import hashlib
import os
import platform
from importlib import metadata
from pathlib import Path
from typing import Any

from ifixai._version import VERSION
from ifixai.core.fixture_loader import load_fixture
from ifixai.core.types import EvaluationPipelineConfig, Fixture, TestRunResult
from ifixai.judge.config import JudgeConfig
from ifixai.providers.base import ChatProvider
from ifixai.providers.secrets import scrub_secrets

from .integrity import (
    canonical_digest,
    changed_fields,
    freeze_experiment,
    trial_provenance,
)
from .models import (
    ExperimentArm,
    ExperimentBundle,
    ExperimentJudge,
    ExperimentPlan,
    ExperimentTrial,
)


def validate_plan(plan: ExperimentPlan) -> Fixture:
    """Validate and freeze fixture inputs; no provider construction or environment access."""
    plan = ExperimentPlan.model_validate(plan.model_dump(mode="json"))
    return load_fixture(plan.fixture).model_copy(deep=True)


def _credential(arm: ExperimentArm | ExperimentJudge) -> tuple[str, tuple[str, ...]]:
    from ifixai.providers.resolver import credential_env_vars, resolve_credential

    if arm.api_key_env:
        value = os.environ.get(arm.api_key_env, "")
        if not value:
            raise ValueError("declared credential environment variable is missing")
        return value, (value,)
    names = credential_env_vars(arm.provider)
    if not names:
        return "", ()
    resolved = resolve_credential(arm.provider, os.environ)
    if not resolved:
        raise ValueError("provider credentials are missing from the environment")
    secret_names = (*names, "AWS_SESSION_TOKEN") if arm.provider == "bedrock" else names
    return resolved, tuple(os.environ[name] for name in secret_names if os.environ.get(name))


def _redact(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        for secret in sorted(set(secrets), key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        return scrub_secrets(value)
    if isinstance(value, list):
        return [_redact(child, secrets) for child in value]
    if isinstance(value, dict):
        return {_redact(key, secrets): _redact(child, secrets) for key, child in value.items()}
    return value


def engine_sources() -> dict[str, str]:
    """Hash common engine, providers, evaluator and inspection artifacts as shipped."""
    root = Path(__file__).resolve().parents[1]
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix in {".py", ".yaml", ".json"}
        and "tests" not in path.relative_to(root).parts
        and "__pycache__" not in path.relative_to(root).parts
    }


def runtime_versions() -> dict[str, str]:
    versions = {"ifixai": VERSION, "python": platform.python_version(), "platform": platform.platform()}
    for package in ("pydantic", "aiohttp", "openai", "anthropic", "google-generativeai", "boto3", "huggingface-hub"):
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            continue
    return versions


async def run_experiment(plan: ExperimentPlan, *, max_runs: int) -> ExperimentBundle:
    """Run exactly two fresh, serial native selected suites per declared repeat.

    max_runs bounds top-level suite executions, not token charges or internal
    probes/judge calls. No provider calls occur before all declarations,
    fixtures, credentials, SDK availability and the explicit budget validate.
    """
    from ifixai.api import run_selected
    from ifixai.providers.resolver import resolve_provider

    if isinstance(max_runs, bool) or not isinstance(max_runs, int) or max_runs < 1 or 2 * plan.pairs > max_runs:
        raise ValueError("explicit max_runs must cover all 2 * pairs suite executions")
    plan = ExperimentPlan.model_validate(plan.model_dump(mode="json"))
    fixture = validate_plan(plan)
    snapshot = fixture.model_dump(mode="json")
    baseline_key, baseline_secrets = _credential(plan.baseline)
    candidate_key, candidate_secrets = _credential(plan.candidate)
    judge_key, judge_secrets = _credential(plan.judge) if plan.judge is not None else ("", ())
    secrets = baseline_secrets + candidate_secrets + judge_secrets
    baseline_effective = plan.baseline.model_dump(exclude={"label", "api_key_env"})
    candidate_effective = plan.candidate.model_dump(exclude={"label", "api_key_env"})
    if not plan.placebo and baseline_effective == candidate_effective and baseline_key == candidate_key:
        raise ValueError("resolved arms are identical; declare placebo=true for an A/A control")
    declared = {"plan": plan.model_dump(mode="json"), "fixture": snapshot}
    if _redact(declared, secrets) != declared:
        raise ValueError("plan and fixture must not contain embedded credentials; use environment references")
    # Constructors verify optional SDK availability without network. They are
    # discarded; execution obtains fresh providers from the ordinary public API.
    for provider_name in {plan.baseline.provider, plan.candidate.provider, *([plan.judge.provider] if plan.judge else [])}:
        provider = resolve_provider(provider_name)
        if not isinstance(provider, ChatProvider):
            raise TypeError("registered provider must implement the native ChatProvider contract")
        await provider.aclose()
    hashes = engine_sources()
    versions = runtime_versions()
    trials: list[ExperimentTrial] = []
    redacted = False
    for pair_index in range(plan.pairs):
        provenance = trial_provenance(plan, pair_index)
        runs: dict[str, TestRunResult] = {}
        for arm_name in provenance["order"]:
            arm = getattr(plan, arm_name)
            judge = JudgeConfig(
                provider=plan.judge.provider, model=plan.judge.model, api_key=judge_key,
                endpoint=plan.judge.endpoint, temperature=plan.judge.temperature,
                timeout=plan.judge.timeout, max_calls_per_run=plan.judge.max_calls_per_run,
            ) if plan.judge else None
            result = await run_selected(
                test_ids=set(plan.test_ids), provider=arm.provider, fixture=fixture.model_copy(deep=True),
                api_key=baseline_key if arm_name == "baseline" else candidate_key,
                model=arm.model, endpoint=arm.endpoint, system_prompt=arm.system_prompt,
                timeout=arm.timeout, max_retries=arm.max_retries,
                pipeline_config=EvaluationPipelineConfig.model_validate(provenance["pipeline_config"]),
                judge_config=judge, sut_temperature=arm.temperature, sut_seed=provenance["seed"],
                run_nonce=provenance["run_nonce"], holdout_ids=dict(provenance["holdout_ids"]),
                # Arm labels/intervention descriptions stay out of prompts and native system names.
                system_name=arm.provider,
            )
            raw = result.model_dump(mode="json")
            safe = _redact(raw, secrets)
            redacted = redacted or safe != raw
            runs[arm_name] = TestRunResult.model_validate(safe)
        trials.append(ExperimentTrial.model_validate({**provenance, **runs}))
    return freeze_experiment(ExperimentBundle(
        plan=plan, fixture_snapshot=snapshot, fixture_digest=canonical_digest(snapshot),
        engine_fingerprint=canonical_digest(hashes), source_hashes=hashes, runtime_versions=versions,
        changed_fields=changed_fields(plan), trials=tuple(trials),
        redactions=("Runtime credential values and recognizable secret patterns redacted from native observations.",) if redacted else (),
    ))
