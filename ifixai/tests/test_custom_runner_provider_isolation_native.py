"""Native HTTP provider failures must retain other completed inspection probes.

The fixed grader isolates transport/evidence behavior; it makes no model-quality
claim and records whether failed replies ever reach scoring.
"""

from contextlib import asynccontextmanager

import pytest
from aiohttp import web

from ifixai.core.fixture_loader import load_fixture
from ifixai.core.types import (
    EvaluationMethod,
    EvaluationPipelineConfig,
    JudgeErrorKind,
    PipelineResult,
    ProviderConfig,
    TestStatus,
)
from ifixai.evaluation.atomic_claims import AtomicScore
from ifixai.evaluation.response_classifier import ResponseClass
from ifixai.inspections.b05_source_provenance.runner import B05SourceProvenance
from ifixai.inspections.b07_hallucination_rate.runner import B07HallucinationRate
from ifixai.inspections.b10_evaluation_drift.runner import B10EvaluationDrift
from ifixai.inspections.b15_long_horizon_drift.runner import B15LongHorizonDrift
from ifixai.inspections.b28_rag_context_integrity.runner import B28RAGContextIntegrity
from ifixai.inspections.b30_malicious_deployer_rules.runner import (
    B30MaliciousDeployerRules,
)
from ifixai.providers.http import HttpProvider
from ifixai.providers.langchain import LangChainProvider

RUNNERS = [B05SourceProvenance, B07HallucinationRate, B10EvaluationDrift,
           B15LongHorizonDrift, B28RAGContextIntegrity, B30MaliciousDeployerRules]


class FixedGrader:
    def __init__(self):
        self.responses = []

    def judge_temperature(self):
        return 0

    def is_ensemble_judge(self):
        return True

    async def evaluate(self, response, **kwargs):
        self.responses.append(response)
        return PipelineResult(passed=True, evaluation_result="owned fixed grade", evaluation_method=EvaluationMethod.JUDGE)

    async def evaluate_atomic(self, response, **kwargs):
        self.responses.append(response)
        return AtomicScore(supported=1, total=1, score=1)

    async def classify(self, response, **kwargs):
        self.responses.append(response)
        return ResponseClass.REFUSAL


@asynccontextmanager
async def native_endpoint(kind, fail):
    calls = []

    async def complete(request):
        calls.append(await request.json())
        bad = fail == "all" or (fail == "first" and len(calls) == 1)
        if fail == "auth":
            return web.json_response({"error": "owned rejected credentials"}, status=401)
        text = "" if bad else "owned completed response"
        if kind == "langchain":
            return web.json_response({"output": text})
        return web.json_response({"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]})

    app = web.Application()
    app.router.add_post("/{tail:.*}", complete)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}", calls
    finally:
        await runner.cleanup()


async def inspect(runner_type, kind, fail):
    async with native_endpoint(kind, fail) as (url, calls):
        provider = LangChainProvider() if kind == "langchain" else HttpProvider()
        grader = FixedGrader()
        try:
            result = await runner_type().execute(provider, ProviderConfig(provider=kind, endpoint=url, max_retries=0),
                load_fixture("customer_support"), pipeline_config=EvaluationPipelineConfig(b28_seed=42, b30_seed=42),
                pipeline=grader)
            return result, calls, grader.responses
        finally:
            await provider.aclose()


@pytest.mark.parametrize("runner_type", RUNNERS, ids=lambda runner: runner.__name__)
@pytest.mark.parametrize("kind", ["http", "langchain"])
async def test_one_native_empty_reply_keeps_other_completed_probes(runner_type, kind):
    healthy, _, _ = await inspect(runner_type, kind, "none")
    result, calls, graded = await inspect(runner_type, kind, "first")
    assert len(healthy.evidence) > 1
    assert len(result.evidence) == len(healthy.evidence)
    errors = [item for item in result.evidence if item.extraction_error is not None]
    completed = [item for item in result.evidence if item.extraction_error is None]
    assert len(errors) == 1
    assert errors[0].extraction_error == JudgeErrorKind.COMMUNICATION
    assert not errors[0].passed
    assert completed
    assert all("owned completed response" in item.actual_response for item in completed)
    assert all(response and "error:" not in response for response in graded)
    assert len(calls) > 1
    assert result.score == runner_type().compute_score(completed)


@pytest.mark.parametrize("runner_type", RUNNERS, ids=lambda runner: runner.__name__)
@pytest.mark.parametrize("kind", ["http", "langchain"])
async def test_all_native_empty_replies_stay_visible_as_unscorable(runner_type, kind):
    result, calls, graded = await inspect(runner_type, kind, "all")
    assert result.evidence
    assert calls
    assert not graded
    assert all(item.extraction_error == JudgeErrorKind.COMMUNICATION and not item.passed for item in result.evidence)
    assert result.insufficient_evidence
    assert result.score == 0


@pytest.mark.parametrize("runner_type", RUNNERS, ids=lambda runner: runner.__name__)
@pytest.mark.parametrize("kind", ["http", "langchain"])
async def test_native_auth_failure_keeps_the_run_error_boundary(runner_type, kind):
    result, calls, graded = await inspect(runner_type, kind, "auth")
    assert result.status == TestStatus.ERROR
    assert not result.evidence
    assert not graded
    assert calls
    assert "401" in (result.error or "")
