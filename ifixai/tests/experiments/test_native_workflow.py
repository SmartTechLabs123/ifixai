"""Real native HTTP/inspection execution; no generated result is substituted."""

import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

import pytest
from click.testing import CliRunner

from ifixai import api
from ifixai.core.types import EvaluationPipelineConfig
from ifixai.experiments import ExperimentPlan, load_experiment, run_experiment

_EXAMPLE_PATH = Path(__file__).resolve().parents[3] / "examples" / "assurance_experiment.py"
_SPEC = importlib.util.spec_from_file_location("offline_assurance_example", _EXAMPLE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
example = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(example)


@pytest.mark.parametrize("control,delta,gate", [
    ("improvement", 1, "INCONCLUSIVE"),
    ("placebo", 0, "INCONCLUSIVE"),
    ("regression", -1, "BLOCK"),
    ("unscored", None, "INCONCLUSIVE"),
])
async def test_native_http_pairing_controls_and_unscored(control, delta, gate, monkeypatch, tmp_path):
    native_calls = []
    original = api.run_selected
    instances = []
    resolve = api._resolve_provider_with_governance

    def observed_provider(*args, **kwargs):
        provider = resolve(*args, **kwargs)
        instances.append(provider)
        return provider

    async def observed(*args, **kwargs):
        native_calls.append((args, kwargs))
        return await original(*args, **kwargs)

    monkeypatch.setattr(api, "run_selected", observed)
    monkeypatch.setattr(api, "_resolve_provider_with_governance", observed_provider)
    with example.local_endpoint() as (endpoint, requests):
        plan = ExperimentPlan.model_validate(example.offline_plan(endpoint, control=control))
        bundle = await run_experiment(plan, max_runs=4)
    assert len(native_calls) == 4
    assert len(instances) == len({id(instance) for instance in instances}) == 4
    assert [t.order for t in bundle.trials] == [("baseline", "candidate"), ("candidate", "baseline")]
    assert len({t.seed for t in bundle.trials}) == 2
    assert bundle.analysis.gate == gate
    assert bundle.analysis.exploratory
    inspection = bundle.analysis.inspections[0]
    assert inspection.mean_delta == delta
    assert inspection.eligible_pairs == (0 if delta is None else 2)
    for trial in bundle.trials:
        first, second = native_calls[trial.pair_index * 2:trial.pair_index * 2 + 2]
        for key in ("sut_seed", "run_nonce", "holdout_ids", "pipeline_config"):
            assert first[1][key] == second[1][key]
        pipeline = first[1]["pipeline_config"]
        for key in EvaluationPipelineConfig.model_fields:
            if key.endswith("_seed"):
                assert getattr(pipeline, key) == trial.seed
                assert getattr(pipeline, key + "_pinned") is True
        left, right = trial.baseline.test_results[0], trial.candidate.test_results[0]
        if delta is not None:
            assert [(e.test_case_id, e.prompt_sent, e.expected) for e in left.evidence] == [(e.test_case_id, e.prompt_sent, e.expected) for e in right.evidence]
            assert len(left.evidence) == 30
            assert len(right.evidence) == 30
            assert trial.baseline.overall_score is None
            assert trial.candidate.overall_score is None
    counts = Counter(p.get("model") for p in requests)
    if control != "unscored":
        assert counts == {"offline-sut": 240, "offline-fragment-judge": 120, None: 4}
    # A frozen bundle really replays after the server is shut down.
    path = tmp_path / "experiment.json"
    path.write_text(bundle.model_dump_json(), encoding="utf-8")
    replay = load_experiment(path)
    assert replay.analysis == bundle.analysis
    assert len(native_calls) == 4


async def test_run_budget_refusal_happens_before_any_network(monkeypatch):
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("over-budget experiment reached native runner")
    monkeypatch.setattr(api, "run_selected", forbidden)
    plan = ExperimentPlan.model_validate(example.offline_plan("http://127.0.0.1:1"))
    with pytest.raises(ValueError):
        await run_experiment(plan, max_runs=3)


async def test_runtime_credentials_echoed_by_native_http_are_redacted(monkeypatch):
    secret = "runtime-only-canary-SECRET-997173"
    monkeypatch.setenv("ASSURANCE_RUNTIME_TEST_KEY", secret)
    with example.local_endpoint(response_suffix=" " + secret) as (endpoint, _requests):
        raw = example.offline_plan(endpoint, pairs=1)
        for name in ("baseline", "candidate"):
            raw[name]["api_key_env"] = "ASSURANCE_RUNTIME_TEST_KEY"
        bundle = await run_experiment(ExperimentPlan.model_validate(raw), max_runs=2)
    assert secret not in bundle.model_dump_json()
    assert "[REDACTED]" in bundle.model_dump_json()
    assert bundle.redactions
    assert bundle.analysis.gate != "PASS"


async def test_runtime_key_embedded_in_declared_prompt_rejected_before_any_native_call(monkeypatch):
    secret = "runtime-only-input-canary-519143"
    monkeypatch.setenv("ASSURANCE_RUNTIME_TEST_KEY", secret)
    raw = example.offline_plan("http://127.0.0.1:1", pairs=1)
    raw["candidate"]["api_key_env"] = "ASSURANCE_RUNTIME_TEST_KEY"
    raw["candidate"]["system_prompt"] = "Unexpected credential leak " + secret
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("embedded credential reached physical runner")
    monkeypatch.setattr(api, "run_selected", forbidden)
    with pytest.raises(ValueError):
        await run_experiment(ExperimentPlan.model_validate(raw), max_runs=2)


def test_real_cli_run_replay_and_atomic_output(tmp_path):
    from ifixai.cli.main import ifixai_cli
    destination = tmp_path / "new-evidence"
    plan_path = tmp_path / "plan.json"
    runner = CliRunner()
    with example.local_endpoint() as (endpoint, requests):
        plan_path.write_text(json.dumps(example.offline_plan(endpoint)), encoding="utf-8")
        result = runner.invoke(ifixai_cli, ["experiment", "run", str(plan_path), "--max-runs", "4", "--output", str(destination)])
        assert result.exit_code == 2, (result.output, result.exception)
        assert requests
        assert (destination / "experiment.json").is_file()
        assert any(destination.glob("*.html"))
        before = len(requests)
        refused = runner.invoke(ifixai_cli, ["experiment", "run", str(plan_path), "--max-runs", "4", "--output", str(destination)])
        assert refused.exit_code != 0
        assert len(requests) == before
    replay = runner.invoke(ifixai_cli, ["experiment", "replay", str(destination / "experiment.json"), "--output", str(tmp_path / "replay")])
    assert replay.exit_code == 2, (replay.output, replay.exception)
    if sys.platform != "win32":
        assert (destination / "experiment.json").stat().st_mode & 0o077 == 0


def test_cli_dryrun_does_not_resolve_runtime_credentials_or_connect(tmp_path, monkeypatch):
    from ifixai.cli.main import ifixai_cli
    plan = example.offline_plan("http://127.0.0.1:1")
    plan["baseline"]["api_key_env"] = "ASSURANCE_TEST_MISSING_KEY"
    monkeypatch.delenv("ASSURANCE_TEST_MISSING_KEY", raising=False)
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    result = CliRunner().invoke(ifixai_cli, ["experiment", "run", str(path), "--dry-run"])
    assert result.exit_code == 0, (result.output, result.exception)
