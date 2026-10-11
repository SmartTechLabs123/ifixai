"""Canonical freezing and structural replay validation (no model re-execution)."""
from __future__ import annotations

import hashlib
import json
import types
from pathlib import Path
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel

from ifixai.core.types import EvaluationPipelineConfig, Fixture
from ifixai.inspections.holdout_ids import generate_holdout_ids
from ifixai.providers.governance_fixture import GovernanceFixture

from .models import ExperimentBundle, ExperimentPlan


def canonical_digest(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def changed_fields(plan: ExperimentPlan) -> tuple[str, ...]:
    baseline = plan.baseline.model_dump(exclude={"label"})
    candidate = plan.candidate.model_dump(exclude={"label"})
    return tuple(sorted(key for key in baseline if baseline[key] != candidate[key]))


def trial_provenance(plan: ExperimentPlan, pair_index: int) -> dict[str, Any]:
    if not 0 <= pair_index < plan.pairs:
        raise ValueError("pair index outside declared plan")
    seed = plan.seed + pair_index
    fields = EvaluationPipelineConfig.model_fields
    pinned: dict[str, Any] = {name: True if name.endswith("_seed_pinned") else seed for name in fields if name.endswith(("_seed", "_seed_pinned"))}
    pipeline = EvaluationPipelineConfig(mode=plan.eval_mode, **pinned)
    nonce = canonical_digest({"plan": plan.model_dump(mode="json"), "pair": pair_index})[:16]
    return {
        "pair_index": pair_index,
        "order": ("baseline", "candidate") if pair_index % 2 == 0 else ("candidate", "baseline"),
        "seed": seed,
        "run_nonce": nonce,
        "holdout_ids": generate_holdout_ids(seed).to_dict(),
        "pipeline_config": pipeline.model_dump(mode="json"),
    }


def snapshot_fixture(snapshot: dict[str, Any]) -> Fixture:
    """Restore a normalized, already-frozen fixture, including governance objects."""
    fixture = Fixture.model_validate(snapshot)
    if fixture.governance is not None:
        fixture.governance = GovernanceFixture.model_validate(fixture.governance)
    if fixture.model_dump(mode="json") != snapshot:
        raise ValueError("fixture snapshot must be complete and normalized")
    return fixture


def verify_experiment(bundle: ExperimentBundle, *, check_analysis: bool = True, require_digest: bool = True) -> None:
    payload = bundle.model_dump(mode="json", exclude={"integrity_digest"})
    if require_digest and canonical_digest(payload) != bundle.integrity_digest:
        raise ValueError("experiment integrity digest mismatch")
    if canonical_digest(bundle.fixture_snapshot) != bundle.fixture_digest:
        raise ValueError("fixture digest mismatch")
    fixture = snapshot_fixture(bundle.fixture_snapshot)
    if not bundle.source_hashes or any(len(value) != 64 or any(c not in "0123456789abcdef" for c in value) for value in bundle.source_hashes.values()):
        raise ValueError("invalid source fingerprint manifest")
    if canonical_digest(bundle.source_hashes) != bundle.engine_fingerprint:
        raise ValueError("engine fingerprint mismatch")
    if bundle.changed_fields != changed_fields(bundle.plan):
        raise ValueError("declared changed fields mismatch")
    if len(bundle.trials) != bundle.plan.pairs:
        raise ValueError("experiment requires every declared pair exactly once")
    if tuple(trial.pair_index for trial in bundle.trials) != tuple(range(bundle.plan.pairs)):
        raise ValueError("missing, duplicate, or out-of-order pair IDs")
    for trial in bundle.trials:
        provenance = trial_provenance(bundle.plan, trial.pair_index)
        for field, expected in provenance.items():
            if getattr(trial, field) != expected:
                raise ValueError(f"pair {trial.pair_index}: invalid {field} provenance")
        for arm_name in ("baseline", "candidate"):
            arm = getattr(bundle.plan, arm_name)
            run = getattr(trial, arm_name)
            ids = [result.test_id for result in run.test_results]
            if len(ids) != len(set(ids)) or set(ids) != set(bundle.plan.test_ids):
                raise ValueError("arm results must match selected inspection IDs exactly")
            if run.fixture_name != fixture.metadata.name or run.provider != arm.provider:
                raise ValueError("arm fixture/provider provenance mismatch")
            for result in run.test_results:
                if result.spec is not None and result.spec.test_id != result.test_id:
                    raise ValueError("inspection specification identity mismatch")
                if result.variant_seed is not None and (result.variant_seed != trial.seed or not result.variant_seed_pinned):
                    raise ValueError("inspection variant seed must match pinned pair provenance")
    if check_analysis:
        from .analysis import _analyze

        if _analyze(bundle, bundle.plan.policy) != bundle.analysis:
            raise ValueError("retained analysis does not match native observations")


def freeze_experiment(bundle: ExperimentBundle) -> ExperimentBundle:
    """Validate structure, recompute analysis and checksum over the complete payload."""
    from .analysis import _analyze

    verify_experiment(bundle, check_analysis=False, require_digest=False)
    analyzed = bundle.model_copy(update={"analysis": _analyze(bundle, bundle.plan.policy), "integrity_digest": ""})
    return analyzed.model_copy(update={"integrity_digest": canonical_digest(analyzed.model_dump(mode="json", exclude={"integrity_digest"}))})


def _reject_unknown(value: Any, annotation: Any) -> None:
    """Native result models ignore extras by default; replay must reject them."""
    origin = get_origin(annotation)
    if origin in {Union, types.UnionType}:
        for member in get_args(annotation):
            if value is not None and member is not type(None):
                _reject_unknown(value, member)
        return
    if isinstance(annotation, type) and issubclass(annotation, BaseModel) and isinstance(value, dict):
        unknown = set(value) - set(annotation.model_fields)
        if unknown:
            raise ValueError("unknown fields in retained model")
        for name, field in annotation.model_fields.items():
            if name in value:
                _reject_unknown(value[name], field.annotation)
    elif origin in {list, tuple} and isinstance(value, list):
        args = get_args(annotation)
        for index, child in enumerate(value):
            _reject_unknown(child, args[0] if len(args) == 1 or args[-1] is Ellipsis else args[index % len(args)])
    elif origin is dict and isinstance(value, dict):
        args = get_args(annotation)
        if len(args) == 2:
            for child in value.values():
                _reject_unknown(child, args[1])


def _reject_constant(_: str) -> None:
    raise ValueError("nonfinite JSON constants are not accepted")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def load_experiment(path: str | Path) -> ExperimentBundle:
    """Strict UTF-8 replay: verify checksum, structure, provenance and analysis."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=_reject_constant, object_pairs_hook=_unique_object)
    _reject_unknown(raw, ExperimentBundle)
    bundle = ExperimentBundle.model_validate(raw)
    verify_experiment(bundle)
    return bundle
