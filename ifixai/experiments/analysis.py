"""Repeat-level, conservative paired inference; never rewrites native grades."""
from __future__ import annotations

import math
from collections import defaultdict
from typing import TYPE_CHECKING

from ifixai.core.types import (
    EvaluationMode,
    EvidenceItem,
    TestResult,
    TestRunResult,
    TestStatus,
)

from .models import ExperimentAnalysis, ExperimentPolicy, InspectionAnalysis

if TYPE_CHECKING:
    from .models import ExperimentBundle

ASSUMPTIONS = (
    "Simultaneous Hoeffding bounds assume independent, stationary repeats; evidence items are not independent samples.",
    "Seeds request reproducibility; model, provider, environment and judge drift remain uncontrolled.",
    "Selected-suite noninferiority is not deployment authorization, certification, or causal proof.",
    "Source hashes and a self-contained checksum establish integrity, not authenticity or truth.",
    "Replay reanalyzes retained observations and does not re-execute providers.",
)


def simultaneous_bounds(deltas: tuple[float, ...], inspections: int, confidence: float) -> tuple[float, float, float] | None:
    """Bound the mean of repeat-level deltas in [-1, 1], union bound over k."""
    if not deltas:
        return None
    if inspections < 1 or not 0 < confidence < 1 or any(not math.isfinite(d) or not -1 <= d <= 1 for d in deltas):
        raise ValueError("invalid simultaneous-bound inputs")
    mean = math.fsum(deltas) / len(deltas)
    width = math.sqrt(2 * math.log(2 * inspections / (1 - confidence)) / len(deltas))
    return mean, max(-1.0, mean - width), min(1.0, mean + width)


def _identity(item: EvidenceItem) -> tuple[str, int | None, str, str]:
    return (item.test_case_id, item.step_number, item.inspection_method.value, item.evaluation_method.value)


def _input_signature(item: EvidenceItem) -> tuple[object, ...]:
    # Native inspections sometimes retain setup turns only in details (B19's
    # context_provided), not prompt_sent. Compare these as controlled inputs,
    # excluding outcome/judge fields. Opaque trajectory records fail closed if
    # they differ: they cannot establish the same case was exercised.
    keys = {"context_provided", "query", "prompt", "prompts", "input", "inputs",
            "messages", "turns", "conversation", "trajectory", "initial_context",
            "user_id", "role", "tool_id", "source_id", "plan", "probe_id",
            "probe_category", "scenario", "variant", "seed", "reference", "references"}
    controlled = {key: value for key, value in item.details.items()
                  if key in keys or key.startswith("expected_")
                  or key.endswith(("_prompt", "_input", "_context", "_turns", "_messages"))}
    from .integrity import canonical_digest

    return (item.prompt_sent, item.expected, item.expected_behavior, canonical_digest(controlled))


def _measurements(result: TestResult) -> tuple[dict[tuple[str, int | None, str, str], EvidenceItem], bool]:
    records: dict[tuple[str, int | None, str, str], EvidenceItem] = {}
    duplicate = False
    for item in result.evidence:
        if item.is_diagnostic:
            continue
        identity = _identity(item)
        if not item.test_case_id or identity in records:
            duplicate = True
        records[identity] = item
    return records, duplicate


def _eligible(run: TestRunResult, result: TestResult) -> bool:
    return (
        not run.partial and not run.resumed_run_id and not run.reused_result_count
        and not run.not_run_test_ids and not run.abort_reason
        and result.status in {TestStatus.PASS, TestStatus.FAIL}
        and not result.insufficient_evidence and not result.error and not result.error_message
        and math.isfinite(result.score) and 0 <= result.score <= 1
    )


def analyze_experiment(bundle: ExperimentBundle, policy: ExperimentPolicy | None = None) -> ExperimentAnalysis:
    """Verify and reanalyze a retained bundle without provider resolution or network."""
    from .integrity import verify_experiment

    verify_experiment(bundle, check_analysis=False)
    return _analyze(bundle, policy or bundle.plan.policy)


def _analyze(bundle: ExperimentBundle, policy: ExperimentPolicy) -> ExperimentAnalysis:
    blocked: list[str] = []
    uncertain: list[str] = []
    reports: list[InspectionAnalysis] = []
    plan = bundle.plan
    exploratory = plan.eval_mode in {EvaluationMode.DETERMINISTIC, EvaluationMode.SELF} or any(
        arm.provider in {"mock", "bridge"} for arm in (plan.baseline, plan.candidate)
    ) or plan.judge is None or plan.judge.provider in {"mock", "bridge"}
    if exploratory:
        uncertain.append("Deterministic, self-judged, or mock experiments are exploratory and cannot receive PASS.")
    if any(run.self_judged or run.judge_relation != "cross-vendor" for trial in bundle.trials for run in (trial.baseline, trial.candidate)):
        exploratory = True
        uncertain.append("Every arm requires a recorded cross-vendor judge relation for PASS; same-vendor or unknown independence is exploratory.")
    post_capture_policy = policy != plan.policy
    if post_capture_policy:
        uncertain.append("Decision policy changed after capture; retrospective reanalysis cannot promote the experiment to PASS.")
    missing_required = sorted(set(policy.required_test_ids) - set(plan.test_ids))
    if missing_required:
        uncertain.append("Required inspections absent: " + ", ".join(missing_required))
    from ifixai.scoring.mandatory_minimums import MANDATORY_MINIMUMS

    for trial in bundle.trials:
        run = trial.candidate
        failed = set(run.mandatory_minimum_violations)
        failed.update(test_id for test_id, status in run.mandatory_minimum_status.items() if status == TestStatus.FAIL)
        failed.update(result.test_id for result in run.test_results if
                      result.test_id in MANDATORY_MINIMUMS and
                      (result.status == TestStatus.FAIL or
                       (_eligible(run, result) and result.score < MANDATORY_MINIMUMS[result.test_id])))
        if failed:
            blocked.append(f"Pair {trial.pair_index}: candidate mandatory failure: {', '.join(sorted(failed))}")
    for test_id in plan.test_ids:
        deltas: list[float] = []
        cases: dict[str, list[str]] = defaultdict(list)
        complete = True
        for trial in bundle.trials:
            baseline = next(result for result in trial.baseline.test_results if result.test_id == test_id)
            candidate = next(result for result in trial.candidate.test_results if result.test_id == test_id)
            left, left_duplicate = _measurements(baseline)
            right, right_duplicate = _measurements(candidate)
            eligible = _eligible(trial.baseline, baseline) and _eligible(trial.candidate, candidate)
            aligned = bool(left) and bool(right) and not left_duplicate and not right_duplicate
            if left_duplicate or right_duplicate:
                cases["divergent"].append(f"{trial.pair_index}:duplicate-evidence-identity")
            if not left or not right:
                cases["unscored"].append(f"{trial.pair_index}:empty-measurement-set")
            for key in sorted(left.keys() | right.keys(), key=str):
                case_id = f"{trial.pair_index}:{key[0]}:{key[1]}:{key[2]}:{key[3]}"
                if left_duplicate or right_duplicate:
                    cases["divergent"].append(case_id)
                    continue
                if key not in left or key not in right:
                    cases["missing"].append(case_id)
                    aligned = False
                    continue
                before, after = left[key], right[key]
                if _input_signature(before) != _input_signature(after):
                    cases["divergent"].append(case_id)
                    aligned = False
                elif before.extraction_error or after.extraction_error or not eligible:
                    cases["unscored"].append(case_id)
                    aligned = False
                else:
                    outcome = "persistent_pass" if before.passed and after.passed else "broken" if before.passed else "fixed" if after.passed else "persistent_fail"
                    cases[outcome].append(case_id)
                    if outcome == "broken":
                        blocked.append(f"{test_id}: observed pass-to-fail case in pair {trial.pair_index} ({key[0]}).")
            if eligible and aligned:
                delta = candidate.score - baseline.score
                deltas.append(delta)
                if delta < -policy.noninferiority_margin:
                    blocked.append(f"{test_id}: observed scored regression exceeds margin in pair {trial.pair_index}.")
            else:
                complete = False
        if not complete:
            uncertain.append(f"{test_id}: incomplete, unscored, duplicate, or divergent paired evidence.")
        if len(deltas) < policy.min_pairs:
            uncertain.append(f"{test_id}: {len(deltas)} eligible pairs below required {policy.min_pairs}.")
        bounds = simultaneous_bounds(tuple(deltas), len(plan.test_ids), policy.confidence)
        if bounds is None or bounds[1] < -policy.noninferiority_margin:
            uncertain.append(f"{test_id}: simultaneous lower bound does not establish selected-suite noninferiority.")
        reports.append(InspectionAnalysis(
            test_id=test_id, eligible_pairs=len(deltas), paired_deltas=tuple(deltas),
            mean_delta=bounds[0] if bounds else None, lower_bound=bounds[1] if bounds else None,
            upper_bound=bounds[2] if bounds else None, coverage_complete=complete,
            **{name: tuple(cases[name]) for name in ("fixed", "broken", "persistent_pass", "persistent_fail", "missing", "unscored", "divergent")},
        ))
    return ExperimentAnalysis(
        gate="BLOCK" if blocked else "INCONCLUSIVE" if uncertain else "PASS",
        reasons=tuple(dict.fromkeys(blocked + uncertain)), inspections=tuple(reports),
        assumptions=ASSUMPTIONS, exploratory=exploratory,
        planned_pairs=plan.pairs, completed_pairs=len(bundle.trials),
        applied_policy=policy, post_capture_policy=post_capture_policy,
    )
