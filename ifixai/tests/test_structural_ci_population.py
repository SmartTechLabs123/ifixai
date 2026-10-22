"""Inspections that score a subset must publish their CI over that subset."""

import importlib

import pytest

from ifixai.api import run_single
from ifixai.core.types import EvaluationMode, EvaluationPipelineConfig

_RUNNERS = {
    "C02": "c02_low_confidence_abstention",
    "C05": "c05_human_fallback_routing",
    "C11": "c11_operational_outcome_conformance",
    "X04": "x04_detection_performance_gate",
    "X11": "x11_pre_action_confirmation_gate",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("test_id", sorted(_RUNNERS))
async def test_ci_is_computed_over_the_scored_population(test_id):
    runner = importlib.import_module(f"ifixai.inspections.{_RUNNERS[test_id]}.runner")
    result = await run_single(
        test_id,
        provider="mock",
        fixture="default",
        pipeline_config=EvaluationPipelineConfig(mode=EvaluationMode.FULL),
    )
    scored = runner._scored_items(result.evidence)
    ci = result.confidence_interval
    assert 0 < len(scored) < len(result.evidence)
    assert ci.sample_size == len(scored)
    assert ci.lower <= result.score <= ci.upper
