"""An errored judge sample must not turn a demonstrated FAIL majority unscorable.

S02, P13, P22 and P27 read each grade three times through the shared `evaluate_with_consensus`
(their private copies are gone); each runner module's own reference to it is checked.
"""

import asyncio

import pytest

from ifixai.core.types import EvaluationMethod, JudgeErrorKind, PipelineResult
from ifixai.inspections.p13_hidden_side_objective_persistence import runner as p13
from ifixai.inspections.p22_goal_edit_acceptance import runner as p22
from ifixai.inspections.p27_privilege_creep import runner as p27
from ifixai.inspections.s02_configurer_stakeholder_conflict import runner as s02

_ERRORED = PipelineResult(
    passed=False,
    evaluation_result="extraction_error",
    evaluation_method=EvaluationMethod.JUDGE,
    extraction_error=JudgeErrorKind.EXTRACTION,
)


def _verdict(passed: bool) -> PipelineResult:
    return PipelineResult(
        passed=passed,
        evaluation_result="pass" if passed else "fail",
        evaluation_method=EvaluationMethod.JUDGE,
    )


def _run(consensus, samples):
    return asyncio.run(consensus(_Judge(samples), "response", None, None, "context", {}))["result"]


class _Judge:
    def __init__(self, samples):
        self._samples = list(samples)

    def is_ensemble_judge(self):
        return False

    async def evaluate(self, **_kwargs):
        return self._samples.pop(0)


_CONSENSUS = [s02.evaluate_with_consensus, p13.evaluate_with_consensus,
              p22.evaluate_with_consensus, p27.evaluate_with_consensus]


@pytest.mark.parametrize("consensus", _CONSENSUS, ids=["S02", "P13", "P22", "P27"])
def test_errored_first_sample_does_not_unscore_a_fail_majority(consensus):
    result = _run(consensus, [_ERRORED, _verdict(False), _verdict(False)])
    assert result.passed is False
    assert result.extraction_error is None


@pytest.mark.parametrize("consensus", _CONSENSUS, ids=["S02", "P13", "P22", "P27"])
def test_all_errored_samples_stay_unmeasurable(consensus):
    result = _run(consensus, [_ERRORED, _ERRORED, _ERRORED])
    assert result.passed is False
    assert result.extraction_error is JudgeErrorKind.EXTRACTION
