"""Independent numerical oracle; fabricated results are NOT runtime evidence.

Cross-vendor labels below are explicitly fabricated to exercise PASS eligibility.
The native loopback tests independently ensure synthetic runs cannot receive PASS.
"""

import copy
import json
import math

import pytest

from ifixai.core.fixture_loader import load_fixture
from ifixai.core.types import EvidenceItem, TestResult, TestRunResult, TestStatus
from ifixai.experiments import (
    ExperimentArm,
    ExperimentBundle,
    ExperimentPlan,
    ExperimentPolicy,
    analyze_experiment,
    load_experiment,
    simultaneous_bounds,
)
from ifixai.experiments.integrity import (
    canonical_digest,
    freeze_experiment,
    trial_provenance,
)
from ifixai.experiments.models import ExperimentTrial


def fabricated_oracle_bundle(*, pairs=20, scores=(0.0, 1.0), cases=1, test_ids=("B19",), margin=0.2):
    """Construct honest test-only records; no claim these models actually ran."""
    fixture = load_fixture("customer_support")
    plan = ExperimentPlan(
        name="FABRICATED cross-vendor mathematical oracle",
        intervention="TEST ONLY declared model intervention",
        fixture="customer_support", test_ids=test_ids,
        baseline=ExperimentArm(label="control", provider="openai", model="test-only-a"),
        candidate=ExperimentArm(label="intervention", provider="openai", model="test-only-b"),
        pairs=pairs, eval_mode="single",
        judge={"provider": "anthropic", "model": "test-only-judge"},
        policy=ExperimentPolicy(min_pairs=1, required_test_ids=test_ids, noninferiority_margin=margin),
    )

    def run(score):
        passed = score >= 0.5
        return TestRunResult(
            provider="openai", fixture_name=fixture.metadata.name,
            judge_relation="cross-vendor", run_mode="selected",
            test_results=[TestResult(
                test_id=test_id, score=score, passed=passed, passing=passed,
                status=TestStatus.PASS if passed else TestStatus.FAIL,
                evidence=[EvidenceItem(
                    test_case_id=f"case-{i}", prompt_sent="identical controlled prompt",
                    expected="identical expected behavior", actual_response="FABRICATED",
                    passed=passed,
                ) for i in range(cases)],
            ) for test_id in test_ids],
        )

    snapshot = fixture.model_dump(mode="json")
    hashes = {"test-only/source": "a" * 64}
    bundle = ExperimentBundle(
        plan=plan, fixture_snapshot=snapshot, fixture_digest=canonical_digest(snapshot),
        source_hashes=hashes, engine_fingerprint=canonical_digest(hashes),
        runtime_versions={"test": "fabricated"}, changed_fields=("model",),
        trials=tuple(ExperimentTrial(
            **trial_provenance(plan, i),
            baseline=run(scores[0]), candidate=run(scores[1]),
        ) for i in range(pairs)),
    )
    return freeze_experiment(bundle)


def replace_native_result(bundle, change):
    data = bundle.model_dump(mode="json")
    for trial in data["trials"]:
        change(trial["candidate"], trial["baseline"])
    return freeze_experiment(ExperimentBundle.model_validate(data))


@pytest.mark.parametrize("pairs,test_ids", [(5, ("B19",)), (20, ("B19", "B22")), (200, ("B19",))])
def test_simultaneous_interval_uses_trial_count_independent_of_case_count(pairs, test_ids):
    small = fabricated_oracle_bundle(pairs=pairs, test_ids=test_ids)
    large = fabricated_oracle_bundle(pairs=pairs, test_ids=test_ids, cases=30)
    expected_width = math.sqrt(2 * math.log(2 * len(test_ids) / 0.05) / pairs)
    for report in (small.analysis, large.analysis):
        for inspection in report.inspections:
            assert inspection.eligible_pairs == pairs
            assert inspection.mean_delta == 1.0
            assert inspection.lower_bound == pytest.approx(max(-1, 1 - expected_width))
            assert inspection.upper_bound == 1.0
    assert small.analysis.inspections[0].lower_bound == large.analysis.inspections[0].lower_bound


def test_fabricated_cross_vendor_positive_control_and_aa_small_n_uncertainty():
    assert fabricated_oracle_bundle().analysis.gate == "PASS"
    assert fabricated_oracle_bundle(scores=(1, 1)).analysis.gate == "INCONCLUSIVE"
    enough = fabricated_oracle_bundle(pairs=200, scores=(1, 1))
    assert enough.analysis.gate == "PASS"
    assert enough.analysis.inspections[0].mean_delta == 0


@pytest.mark.parametrize("deltas,k,confidence", [
    ((-1., 1.) * 30, 3, .95),
    ((-.2, .3, .7, .1, -.5) * 15, 7, .8),
    ((0.,) * 1000, 1, .99),
    ((-1.,), 2, .95),
])
def test_mixed_delta_bounds_match_independent_mathematical_oracle(deltas, k, confidence):
    mean = sum(deltas) / len(deltas)
    width = math.sqrt((2 / len(deltas)) * math.log((2 * k) / (1 - confidence)))
    assert simultaneous_bounds(deltas, k, confidence) == pytest.approx((mean, max(-1, mean - width), min(1, mean + width)))


@pytest.mark.parametrize("deltas,k,confidence", [
    ((float("nan"),), 1, .95), ((float("inf"),), 1, .95),
    ((1.01,), 1, .95), ((0.,), 0, .95),
    ((0.,), 1, 1), ((0.,), 1, 0),
])
def test_bounds_reject_invalid_statistical_inputs(deltas, k, confidence):
    with pytest.raises(ValueError):
        simultaneous_bounds(deltas, k, confidence)


def test_direction_reversed_and_case_regression_block():
    reversed_bundle = fabricated_oracle_bundle(scores=(1, 0))
    assert reversed_bundle.analysis.gate == "BLOCK"
    assert reversed_bundle.analysis.inspections[0].mean_delta == -1
    assert reversed_bundle.analysis.inspections[0].broken
    # Aggregate equality cannot hide a passed case becoming failed.
    equal = fabricated_oracle_bundle(scores=(1, 1))
    changed = replace_native_result(equal, lambda c, _b: c["test_results"][0]["evidence"][0].update(passed=False))
    assert analyze_experiment(changed).gate == "BLOCK"


@pytest.mark.parametrize("mutation", [
    lambda c, _b: c.update(partial=True),
    lambda c, _b: c.update(resumed_run_id="old", reused_result_count=1),
    lambda c, _b: c["test_results"][0].update(insufficient_evidence=True),
    lambda c, _b: c["test_results"][0].update(status="inconclusive"),
    lambda c, _b: c["test_results"][0].update(evidence=[]),
    lambda c, _b: c["test_results"][0]["evidence"][0].update(is_diagnostic=True),
    lambda c, _b: c["test_results"][0]["evidence"][0].update(extraction_error="communication"),
    lambda c, _b: c["test_results"][0]["evidence"][0].update(prompt_sent="different case"),
    lambda c, _b: c["test_results"][0]["evidence"][0].update(expected="different oracle"),
])
def test_missing_unscored_divergent_and_mixed_provenance_never_improve(mutation):
    changed = replace_native_result(fabricated_oracle_bundle(), mutation)
    report = analyze_experiment(changed)
    assert report.gate != "PASS"
    inspection = report.inspections[0]
    assert inspection.eligible_pairs == 0
    assert inspection.mean_delta is None
    assert not inspection.fixed


@pytest.mark.parametrize("relation", ["", "self", "same-provider"])
def test_unknown_self_and_same_vendor_judging_never_pass(relation):
    changed = replace_native_result(fabricated_oracle_bundle(), lambda c, b: (c.update(judge_relation=relation), b.update(judge_relation=relation)))
    assert analyze_experiment(changed).gate == "INCONCLUSIVE"


def test_required_coverage_and_min_pairs_fail_closed():
    bundle = fabricated_oracle_bundle()
    absent = ExperimentPolicy(required_test_ids=("B01", "B19"), min_pairs=1, noninferiority_margin=.2)
    assert analyze_experiment(bundle, absent).gate == "INCONCLUSIVE"
    few = ExperimentPolicy(required_test_ids=("B19",), min_pairs=21, noninferiority_margin=.2)
    assert analyze_experiment(bundle, few).gate == "INCONCLUSIVE"


def test_declared_mandatory_failure_cannot_be_hidden_by_missing_native_spec():
    # Deliberately omitted native spec and redundant gate flags: registry
    # semantics still make B01 a mandatory inspection.
    bundle = fabricated_oracle_bundle(pairs=200, test_ids=("B01",), scores=(0, 0))
    assert bundle.analysis.gate == "BLOCK"


def test_identical_last_prompt_cannot_hide_changed_context_trajectory():
    bundle = fabricated_oracle_bundle()
    def changed_context(candidate, baseline):
        baseline["test_results"][0]["evidence"][0]["details"] = {"context_provided": "The tool has high risk"}
        candidate["test_results"][0]["evidence"][0]["details"] = {"context_provided": "The tool has low risk"}
    changed = replace_native_result(bundle, changed_context)
    assert changed.analysis.gate == "INCONCLUSIVE"
    assert changed.analysis.inspections[0].eligible_pairs == 0
    assert changed.analysis.inspections[0].divergent


def test_duplicate_evidence_identity_never_inflates_coverage():
    bundle = fabricated_oracle_bundle()
    def duplicate(candidate, _baseline):
        records = candidate["test_results"][0]["evidence"]
        records.append(copy.deepcopy(records[0]))
    changed = replace_native_result(bundle, duplicate)
    assert changed.analysis.gate == "INCONCLUSIVE"
    assert changed.analysis.inspections[0].eligible_pairs == 0
    assert changed.analysis.inspections[0].divergent


def resigned(data):
    data = copy.deepcopy(data)
    data.pop("integrity_digest", None)
    data["integrity_digest"] = canonical_digest(data)
    return data


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(schema_version="future"),
    lambda d: d["trials"].pop(),
    lambda d: d["trials"][1].update(pair_index=0),
    lambda d: d["trials"][0].update(pair_index=999),
    lambda d: d["trials"][0].update(seed=99999),
    lambda d: d["trials"][0].update(order=["candidate", "baseline"]),
    lambda d: d["trials"][0].update(run_nonce="f" * 16),
    lambda d: d["trials"][0]["pipeline_config"].update(b12_seed_pinned=False),
    lambda d: d["trials"][0]["holdout_ids"].clear(),
    lambda d: d["fixture_snapshot"]["metadata"].update(name="different fixture"),
    lambda d: d["trials"][0]["candidate"]["test_results"].clear(),
    lambda d: d["trials"][0]["candidate"]["test_results"].append(d["trials"][0]["candidate"]["test_results"][0]),
])
def test_replay_rejects_resigned_structural_tampering(tmp_path, mutate):
    data = fabricated_oracle_bundle().model_dump(mode="json")
    mutate(data)
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(resigned(data)), encoding="utf-8")
    with pytest.raises((ValueError, TypeError)):
        load_experiment(path)


def test_replay_detects_payload_tamper_and_needs_no_provider(tmp_path, monkeypatch):
    bundle = fabricated_oracle_bundle()
    path = tmp_path / "bundle.json"
    path.write_text(bundle.model_dump_json(), encoding="utf-8")
    def forbidden(*_args, **_kwargs):
        raise AssertionError("replay resolved a provider")
    monkeypatch.setattr("ifixai.providers.resolver.resolve_provider", forbidden)
    replay = load_experiment(path)
    assert analyze_experiment(replay) == bundle.analysis
    data = json.loads(path.read_text(encoding="utf-8"))
    data["trials"][0]["candidate"]["test_results"][0]["score"] = .75
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        load_experiment(path)


@pytest.mark.parametrize("mutation", [
    lambda d: d["trials"][0]["candidate"].update(unrecognized_native_field=True),
    lambda d: d["trials"][0]["candidate"]["test_results"][0]["evidence"][0].update(unrecognized_evidence_field=True),
    lambda d: d["source_hashes"].update({"test-only/source": "broken"}),
    lambda d: d.update(engine_fingerprint="b" * 64),
    lambda d: d.update(changed_fields=["label"]),
    lambda d: d["trials"][0]["candidate"].update(fixture_name="wrong fixture"),
    lambda d: d["trials"][0]["candidate"].update(provider="anthropic"),
])
def test_replay_rejects_unknown_native_fields_and_provenance(tmp_path, mutation):
    data = fabricated_oracle_bundle().model_dump(mode="json")
    mutation(data)
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(resigned(data)), encoding="utf-8")
    with pytest.raises(ValueError):
        load_experiment(path)


def test_replay_rejects_duplicate_json_keys_and_nonfinite(tmp_path):
    path = tmp_path / "invalid.json"
    for raw in ('{"schema_version":"1","schema_version":"1"}', '{"bad":NaN}'):
        path.write_text(raw, encoding="utf-8")
        with pytest.raises(ValueError):
            load_experiment(path)
