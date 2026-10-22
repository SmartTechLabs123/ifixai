"""A run stops at once when the judge's key or credit runs out, and when it spends its judge budget
plus the 25% it may go over. After the stop nothing more goes to the judge or the agent, what
finished keeps its result, the rest is reported as not run, and the user reads one plain line
that says how to fix it. Never a FAIL, a raw provider error or a stack trace."""

import asyncio
import http.server
import io
import json
import logging
import re
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path

import pytest
from aiohttp import web
from click.testing import CliRunner
from rich.console import Console

from ifixai.cli import orchestrator, ui
from ifixai.cli.main import ifixai_cli
from ifixai.core import runner as core_runner
from ifixai.core.concurrency import ConcurrencyGovernor
from ifixai.core.fixture_loader import load_fixture
from ifixai.core.runner import build_partial_result, run_selected
from ifixai.core.types import (
    AnalyticRubric,
    ChatMessage,
    EvaluationCriteria,
    EvaluationMode,
    EvaluationPipelineConfig,
    EvidenceItem,
    ExpectedClaim,
    InspectionCategory,
    InspectionSpec,
    ProviderConfig,
    RubricDimension,
    RubricVerdict,
    TestResult,
    TestStatus,
)
from ifixai.evaluation import analytic_judge
from ifixai.evaluation.analytic_judge import (
    AnalyticRubricJudge,
    EnsembleAnalyticRubricJudge,
    JudgeCommunicationError,
)
from ifixai.evaluation.atomic_claims import (
    score_atomic_claims,
    score_atomic_claims_with_ground_truth,
)
from ifixai.evaluation.pipeline import CURRENT_INSPECTION, EvaluationPipeline
from ifixai.evaluation.run_stop import (
    RUN_STOP,
    RunStop,
    RunStopped,
    StopKind,
    budget_ceiling,
    drop_stopped_task_reports,
    judge_free,
    stopping,
)
from ifixai.harness.base import BaseTest
from ifixai.harness.registry import INSPECTION_REGISTRY, SPEC_BY_ID, create_inspection
from ifixai.inspections.b26_rate_limiting.runner import B26RateLimiting
from ifixai.judge.config import JudgeConfig, JudgeProviderSpec
from ifixai.judge.evaluator import EnsembleJudgeEvaluator, JudgeEvaluator, judge_refusal
from ifixai.providers.base import (
    ChatProvider,
    ProviderAuthError,
    ProviderError,
    ProviderOverloadedError,
    ProviderRateLimitError,
    ProviderResponseError,
    detect_capabilities,
)
from ifixai.providers.resolver import resolve_provider
from ifixai.reporting.scorecard import (
    _format_run_minimums_status,
    generate_json_report,
    generate_markdown_report,
    generate_summary_report,
)

RUBRIC = AnalyticRubric(
    test_id="V09",
    outcome_type="refuse",
    dimensions=[
        RubricDimension(name="warned_reader", description="d", weight=0.5, mandatory=True),
        RubricDimension(name="kept_scope", description="d", weight=0.5),
    ],
)
CRITERIA = EvaluationCriteria(expected_outcome="refuse")
CLAIM = ExpectedClaim(claim="The sky is green.", supported=False, reason="It is blue.")
VERDICT = json.dumps({
    "dimensions": [{"name": d.name, "passed": True, "reasoning": "r"} for d in RUBRIC.dimensions],
    "overall_reasoning": "r",
})
NO_CREDIT = {"error": {"message": "Insufficient credits. Add more credits.", "code": 402}}
SPENT_QUOTA = {"error": {"message": "You exceeded your current quota, please check your plan.",
                         "type": "insufficient_quota", "code": "insufficient_quota"}}
DEAD_KEY = {"error": {"message": "User not found.", "code": 401}}
# OpenRouter's answer when a key, workspace or guardrail budget is spent: no key or credit word.
ORG_BUDGET = {"error": {"message": "Org member budget limit exceeded (weekly limit). Contact your org admin.",
                        "code": 403, "metadata": {"limit_source": "openrouter_key_limit"}}}
FORBIDDEN = {"error": {"message": "Forbidden", "code": 403}}
# OpenRouter's answer for a model slug it doesn't know.
NOT_A_MODEL = {"error": {"message": "judge is not a valid model ID", "code": 400}}
RATE_LIMITED = {"error": {"message": "Rate limit reached for requests", "code": 429}}
# What a plain stop line never shows: a raw provider body, an SDK status line, an exception
# class, or advice to turn the stop off.
RAW = re.compile(r"\{'error'|\{\"error\"|Error code: \d{3}|\b[A-Z]\w*(?:Error|Exception)\b|IFIXAI_JUDGE_FAIL_FAST")


def completion(content: str) -> dict:
    return {"choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}]}


class AiohttpJudge:
    """A judge on the test's own loop: refuses the first request with `refusal` at once and
    answers every later one with a verdict after `hold` seconds. Logs each request's prompt."""

    def __init__(self, refusal: tuple[int, dict] | None = None, hold: float = 0.0) -> None:
        self.refusal = refusal
        self.hold = hold
        self.prompts: list[str] = []
        self.url = ""

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.prompts.append(body["messages"][-1]["content"])
        if self.refusal is not None and len(self.prompts) == 1:
            return web.json_response(self.refusal[1], status=self.refusal[0])
        await asyncio.sleep(self.hold)
        return web.json_response(completion(VERDICT), headers={"retry-after-ms": "1"})


Serve = Callable[[AiohttpJudge], Awaitable[str]]


@pytest.fixture
async def serve() -> AsyncIterator[Serve]:
    runners: list[web.AppRunner] = []

    async def start(judge: AiohttpJudge) -> str:
        app = web.Application()
        app.router.add_post("/chat/completions", judge.handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        runners.append(runner)
        judge.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        return judge.url

    yield start
    for runner in runners:
        await runner.cleanup()


def stopped(kind: StopKind = StopKind.BUDGET) -> RunStop:
    stop = RunStop()
    stop.stop(RunStopped(kind, "what happened", "Fix it", "budget"))
    return stop


# (a) A 429 that says the account is spent is never retried; a plain rate limit still is.


@pytest.mark.parametrize(("body", "sends"), [(SPENT_QUOTA, 1), (RATE_LIMITED, 3)])
async def test_a_spent_quota_is_never_retried_and_a_rate_limit_still_is(
    serve: Serve, body: dict, sends: int
) -> None:
    pytest.importorskip("openai")

    class Throttled(AiohttpJudge):
        async def handle(self, request: web.Request) -> web.Response:
            self.prompts.append("x")
            return web.json_response(body, status=429, headers={"retry-after-ms": "1"})

    judge = Throttled()
    url = await serve(judge)
    provider = resolve_provider("openai")
    config = ProviderConfig(provider="openai", api_key="k", model="m", endpoint=url, max_retries=2)
    try:
        with pytest.raises(ProviderRateLimitError):
            await provider.send_message([ChatMessage(role="user", content="hi")], config)
    finally:
        await provider.aclose()

    assert len(judge.prompts) == sends


# (b) The stop line is one plain sentence that says how to fix it.


@pytest.mark.parametrize(
    ("error", "says"),
    [
        (ProviderAuthError(provider="openai", details=f"Error code: 401 - {DEAD_KEY}"), "refused its API key"),
        (ProviderResponseError(provider="openai", details=f"HTTP 402: Error code: 402 - {NO_CREDIT}"), "ran out of credit"),
        (ProviderRateLimitError(provider="openai", details=f"Error code: 429 - {SPENT_QUOTA}"), "ran out of credit or quota"),
        (ProviderResponseError(provider="openrouter", details="HTTP 403: Error code: 403 - this API key is disabled"), "refused its API key"),
        # A 402 refuses payment whatever its body says.
        (ProviderResponseError(provider="openrouter", details="HTTP 402: Error code: 402 - {'error': {'message': 'Payment needed'}}"), "ran out of credit"),
        # The status read the same way the refusal itself was, however the provider words it.
        (ProviderResponseError(provider="openrouter", details="status_code=403: unauthorized"), "refused its API key (openrouter, HTTP 403)"),
        (ProviderResponseError(provider="gemini", details="HTTP/1.1 402 Payment Required"), "ran out of credit or quota (gemini, HTTP 402)"),
        (ProviderRateLimitError(provider="openai", details="status code: 429 insufficient_quota"), "ran out of credit or quota (openai, HTTP 429)"),
        # Any 403 the provider didn't send for the content: a spent budget reads as credit, the
        # rest (a bare "Forbidden", a blocked region) as the key.
        (ProviderResponseError(provider="openrouter", details=f"HTTP 403: Error code: 403 - {ORG_BUDGET}"), "ran out of credit or quota (openrouter, HTTP 403)"),
        (ProviderResponseError(provider="openrouter", details="HTTP 403: Error code: 403 - Forbidden"), "refused its API key (openrouter, HTTP 403)"),
        (ProviderResponseError(provider="openai", details="Error code: 403 - {'error': {'code': 'unsupported_country_region_territory'}}"), "refused its API key (openai, HTTP 403)"),
    ],
)
def test_a_refused_key_or_credit_reads_as_one_plain_line_with_the_fix(
    error: ProviderError, says: str
) -> None:
    wrapped = JudgeCommunicationError("Judge provider rejected the request")
    wrapped.__cause__ = error

    stop = judge_refusal(wrapped)

    assert stop is not None and stop.kind is StopKind.JUDGE_REFUSED
    assert says in stop.what
    assert "top up" in stop.fix.lower() and "--judge-api-key" in stop.fix
    assert not RAW.search(f"{stop.what}. {stop.fix}")


@pytest.mark.parametrize(
    "error",
    [
        ProviderOverloadedError(provider="openai", details="HTTP 503: overloaded"),
        ProviderRateLimitError(provider="openai", details=f"Error code: 429 - {RATE_LIMITED}"),
        ProviderResponseError(provider="openai", details="Error code: 400 - prompt is too long"),
        ProviderResponseError(provider="openai", details="HTTP 400: request id 9c3a402e"),
        # A 403 for the probe's content (OpenRouter moderation, whose body says "No credits were
        # charged"): only that request fails.
        ProviderResponseError(
            provider="openrouter",
            details="HTTP 403: Error code: 403 - {'error': {'code': 403, 'message': 'openai/gpt-4.1-mini "
            "requires moderation on OpenAI. Your input was flagged for \"violence\". No credits were charged.'}}",
        ),
    ],
)
def test_a_call_that_can_still_succeed_never_stops_the_run(error: ProviderError) -> None:
    assert judge_refusal(error) is None


# (c) After a refusal, nothing more goes to the judge or the agent.


class CountingAgent(ChatProvider):
    def __init__(self) -> None:
        self.sends = 0

    async def send_message(self, messages: list[ChatMessage], config: ProviderConfig) -> str:
        self.sends += 1
        return "I won't do that."


class TwoTurnsEach(BaseTest):
    """A stand-in inspection: six probes at once, each asks the agent and has the reply graded,
    twice."""

    def __init__(self) -> None:
        super().__init__(InspectionSpec(
            test_id="V09", name="stand-in", category=InspectionCategory.DECEPTION,
            description="d", threshold=0.5, weight=0.1, scoring_method="pass rate",
            min_evidence_items=1,
        ))

    async def probe(self, provider: ChatProvider, config: ProviderConfig, index: int) -> list[EvidenceItem]:
        items = []
        for turn in range(2):
            reply = await provider.send_message([ChatMessage(role="user", content=f"p{index}-{turn}")], config)
            result = await self._pipeline.evaluate(response=reply, criteria=CRITERIA, rubric=RUBRIC)
            items.append(EvidenceItem(test_case_id=f"{index}-{turn}", passed=result.passed,
                                      evaluation_result=result.evaluation_result))
        return items

    async def run(self, provider: object, config: object, fixture: object) -> list[EvidenceItem]:
        probes = await asyncio.gather(*[self.probe(provider, config, i) for i in range(6)])  # type: ignore[arg-type]
        return [item for items in probes for item in items]


@pytest.fixture
async def judged() -> AsyncIterator[Callable[[str], EvaluationPipeline]]:
    """Pipelines over a local judge; their SDK clients are closed after the test."""
    made: list[JudgeEvaluator] = []

    def pipeline(url: str) -> EvaluationPipeline:
        evaluator = JudgeEvaluator(JudgeConfig(provider="openai", model="judge", api_key="k", endpoint=url))
        made.append(evaluator)
        config = EvaluationPipelineConfig(mode=EvaluationMode.SINGLE, judge_max_calls=0)
        return EvaluationPipeline(config, AnalyticRubricJudge(evaluator))

    yield pipeline
    for evaluator in made:
        await evaluator.aclose()


@pytest.mark.parametrize("refusal", [(402, NO_CREDIT), (429, SPENT_QUOTA), (401, DEAD_KEY)])
async def test_after_a_refusal_no_call_goes_to_the_judge_or_the_agent(
    serve: Serve,
    judged: Callable[[str], EvaluationPipeline],
    monkeypatch: pytest.MonkeyPatch,
    refusal: tuple[int, dict],
    caplog: pytest.LogCaptureFixture,
) -> None:
    pytest.importorskip("openai")
    monkeypatch.setenv("IFIXAI_JUDGE_FAIL_FAST", "1")
    caplog.set_level(logging.WARNING)
    # The refusal lands at once, while the other five grades are on their way out.
    judge = AiohttpJudge(refusal=refusal, hold=0.3)
    pipeline = judged(await serve(judge))
    agent = CountingAgent()

    with stopping(pipeline.run_stop), pytest.raises(RunStopped) as stop:
        await TwoTurnsEach().execute(agent, ProviderConfig(provider="custom"), None, pipeline=pipeline)  # type: ignore[arg-type]
    reached_judge = len(judge.prompts)
    await asyncio.sleep(0.5)

    assert stop.value.kind is StopKind.JUDGE_REFUSED
    # Every probe's first turn went out before the refusal; no second turn ever did.
    assert agent.sends == 6
    # A grade the refusal caught on its way out is cut there, never sent after the stop.
    assert 1 <= reached_judge <= 6
    assert len(judge.prompts) == reached_judge
    assert pipeline.run_stop.kind is StopKind.JUDGE_REFUSED
    # The stop line says it once; no log line repeats the refusal.
    assert not [r.getMessage() for r in caplog.records if "non-retryable" in r.getMessage()]


@pytest.mark.parametrize("expected", [None, [CLAIM]])
async def test_a_refusal_inside_a_claim_score_stops_the_run_too(
    serve: Serve,
    judged: Callable[[str], EvaluationPipeline],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    expected: list[ExpectedClaim] | None,
) -> None:
    """Claim scoring folds a failed call into its score and retries it, so only the judge's own
    send sees the refusal: it stops the run there, before a retry or a log line that would show
    the user the provider's raw error."""
    pytest.importorskip("openai")
    monkeypatch.setenv("IFIXAI_JUDGE_FAIL_FAST", "1")
    caplog.set_level(logging.WARNING)
    judge = AiohttpJudge(refusal=(402, NO_CREDIT))
    pipeline = judged(await serve(judge))
    token = RUN_STOP.set(pipeline.run_stop)
    try:
        with pytest.raises(RunStopped):
            await pipeline.evaluate_atomic(
                response="The sky is green.", sources="s", mode="grounding", expected_claims=expected
            )
    finally:
        RUN_STOP.reset(token)

    assert len(judge.prompts) == 1
    assert not [r.getMessage() for r in caplog.records if "credit" in r.getMessage().lower()]


async def test_an_ensemble_judge_that_refuses_stops_the_run_even_when_the_other_answers(
    serve: Serve, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ensemble scores claims with the judge that answered; the one that refused still stops
    the run once its call returns."""
    pytest.importorskip("openai")
    monkeypatch.setenv("IFIXAI_JUDGE_FAIL_FAST", "1")
    verdicts = [{"claim": CLAIM.claim, "response_correct": False, "reason": "r"}]

    class ByModel(AiohttpJudge):
        async def handle(self, request: web.Request) -> web.Response:
            model = (await request.json())["model"]
            self.prompts.append(model)
            if model == "m1":
                return web.json_response(NO_CREDIT, status=402)
            return web.json_response(completion(json.dumps(verdicts)))

    judge = ByModel()
    ensemble = EnsembleJudgeEvaluator(JudgeConfig(endpoint=await serve(judge), providers=[
        JudgeProviderSpec(provider="openai", model="m1", api_key="a"),
        JudgeProviderSpec(provider="openai", model="m2", api_key="b"),
    ]))
    config = EvaluationPipelineConfig(mode=EvaluationMode.FULL, judge_max_calls=0)
    pipeline = EvaluationPipeline(config, EnsembleAnalyticRubricJudge(ensemble))
    token = RUN_STOP.set(pipeline.run_stop)
    try:
        with pytest.raises(RunStopped) as stop:
            await pipeline.evaluate_atomic(
                response="The sky is green.", sources="", mode="grounding", expected_claims=[CLAIM]
            )
    finally:
        RUN_STOP.reset(token)
        await ensemble.aclose()

    assert stop.value.kind is StopKind.JUDGE_REFUSED
    assert sorted(judge.prompts) == ["m1", "m2"]


async def test_a_run_told_to_keep_going_keeps_going_after_a_refusal(
    serve: Serve, judged: Callable[[str], EvaluationPipeline], monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("openai")
    monkeypatch.setenv("IFIXAI_JUDGE_FAIL_FAST", "0")
    judge = AiohttpJudge(refusal=(402, NO_CREDIT))
    agent = CountingAgent()
    pipeline = judged(await serve(judge))

    with stopping(pipeline.run_stop):
        result = await TwoTurnsEach().execute(agent, ProviderConfig(provider="custom"), None, pipeline=pipeline)  # type: ignore[arg-type]

    assert result.status is TestStatus.PASS
    assert agent.sends == 12


class HeldInClient(ChatProvider):
    """A call that passed the stop check, then waits inside its HTTP client before it is sent."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.sent = 0

    async def send_message(self, messages: list[ChatMessage], config: ProviderConfig) -> str:
        await self.release.wait()
        self.sent += 1
        return VERDICT


# On a budget stop a judge grade already counted is inside the ceiling, so it still finishes.
@pytest.mark.parametrize(
    ("caller", "kind", "finishes"),
    [
        ("agent", StopKind.JUDGE_REFUSED, False),
        ("agent", StopKind.BUDGET, False),
        ("judge", StopKind.JUDGE_REFUSED, False),
        ("judge", StopKind.BUDGET, True),
    ],
)
async def test_a_call_held_in_its_client_when_the_run_stops_never_leaves(
    caller: str, kind: StopKind, finishes: bool
) -> None:
    held = HeldInClient()
    judge = JudgeEvaluator(JudgeConfig(provider="mock", model="m"))
    judge._provider = held
    send = held.send_message if caller == "agent" else judge.send_message
    stop = RunStop()
    token = RUN_STOP.set(stop)
    try:
        call = asyncio.create_task(send([], ProviderConfig(provider="custom")))
    finally:
        RUN_STOP.reset(token)
    await asyncio.sleep(0.01)

    stop.stop(RunStopped(kind, "what happened", "Fix it", "n"))
    held.release.set()
    [outcome] = await asyncio.gather(call, return_exceptions=True)

    if finishes:
        assert (outcome, held.sent) == (VERDICT, 1)
    else:
        assert isinstance(outcome, RunStopped) and outcome.kind is kind
        assert held.sent == 0


# At shutdown the run cancels what is still out, such as a grade a budget stop let finish. A stop
# raised in place of that cancellation would be reported as an error after the stop line.
@pytest.mark.parametrize("budget_stop", [False, True])
async def test_a_cancellation_the_stop_never_made_stays_a_cancellation(budget_stop: bool) -> None:
    held = HeldInClient()
    judge = JudgeEvaluator(JudgeConfig(provider="mock", model="m"))
    judge._provider = held
    stop = RunStop()
    token = RUN_STOP.set(stop)
    try:
        call = asyncio.create_task(judge.send_message([], ProviderConfig(provider="custom")))
    finally:
        RUN_STOP.reset(token)
    await asyncio.sleep(0.01)
    if budget_stop:
        stop.stop(RunStopped(StopKind.BUDGET, "what happened", "Fix it", "budget"))

    call.cancel()
    await asyncio.gather(call, return_exceptions=True)

    assert call.cancelled()
    assert held.sent == 0


# (d) The soft budget: up to 25% over, then the run stops.


@pytest.mark.parametrize(("budget", "ceiling"), [(0, 0), (1, 2), (20, 25), (40, 50), (41, 52), (100, 125)])
def test_a_run_may_go_a_quarter_over_its_budget_rounded_up(budget: int, ceiling: int) -> None:
    assert budget_ceiling(budget) == ceiling


class VerdictProvider(ChatProvider):
    """The judge's provider: a passing verdict on every call, after `fails_first` failed tries
    per grade (`junk`, an unreadable reply, or `overloaded`, a 503). Counts every call it gets."""

    def __init__(self, fails_first: str = "") -> None:
        self.fails_first = fails_first
        self.calls = 0

    async def send_message(self, messages: list[ChatMessage], config: ProviderConfig) -> str:
        self.calls += 1
        await asyncio.sleep(0)
        if self.fails_first and self.calls % 2:
            if self.fails_first == "overloaded":
                raise ProviderOverloadedError(provider="openai", details="HTTP 503: overloaded")
            return "not a verdict"
        return VERDICT


def over(provider: VerdictProvider, model: str = "m") -> JudgeEvaluator:
    evaluator = JudgeEvaluator(JudgeConfig(provider="mock", model=model))
    evaluator._provider = provider
    return evaluator


def budget_pipeline(budget: int, fails_first: str = "") -> tuple[EvaluationPipeline, VerdictProvider]:
    provider = VerdictProvider(fails_first)
    config = EvaluationPipelineConfig(mode=EvaluationMode.SINGLE, judge_max_calls=0, judge_budget=budget)
    return EvaluationPipeline(config, AnalyticRubricJudge(over(provider))), provider


async def grade(pipeline: EvaluationPipeline, inspection: str = "V09") -> bool:
    """One grade for `inspection`, inside the run's stop as a run makes it."""
    token = CURRENT_INSPECTION.set(inspection)
    try:
        with stopping(pipeline.run_stop):
            return (await pipeline.evaluate(response="r", criteria=CRITERIA, rubric=RUBRIC)).passed
    finally:
        CURRENT_INSPECTION.reset(token)


async def send_for(
    agent: ChatProvider, pipeline: EvaluationPipeline, inspection: str, hook: bool = False, graded: bool = True
) -> object:
    """One message to the agent from `inspection` inside the run: a probe the judge grades, one
    sent inside `judge_free` (`graded` False), or with `hook` one hook call."""
    token = CURRENT_INSPECTION.set(inspection)
    config = ProviderConfig(provider="custom")
    try:
        with stopping(pipeline.run_stop), nullcontext() if graded else judge_free():
            if hook:
                return await agent.invoke_tool("t", "r", {}, config)
            return await agent.send_message([], config)
    finally:
        CURRENT_INSPECTION.reset(token)


async def test_the_run_stops_when_it_needs_a_judge_call_past_the_ceiling() -> None:
    pipeline, judge = budget_pipeline(4)
    agent = CountingAgent()

    graded = [await grade(pipeline) for _ in range(5)]
    # The ceiling's calls are spent, but nothing has asked for another: the run goes on, and
    # what no judge grades still reaches the agent, from the inspection the judge graded too: a
    # message sent judge-free, and a hook call.
    assert pipeline.run_stop.kind is None
    assert await send_for(agent, pipeline, "V09", graded=False) == "I won't do that."
    assert await send_for(resolve_provider("mock"), pipeline, "V09", hook=True) is not None
    # A probe the judge would grade would go out to stay ungraded, even from an inspection that
    # never asked the judge anything.
    with pytest.raises(RunStopped) as stop:
        await send_for(agent, pipeline, "B03")
    with pytest.raises(RunStopped):
        await grade(pipeline)

    assert graded == [True] * 5
    assert (judge.calls, agent.sends) == (5, 1)
    assert stop.value.kind is StopKind.BUDGET
    line = f"{stop.value.what}. {stop.value.fix}"
    assert line == (
        "the run used 5 judge calls, its budget of 4 plus the 25% it may go over. Raise the "
        "budget with --judge-budget"
    )
    assert not RAW.search(line)
    assert pipeline.cap_stats() == {
        "judge_call_cap": 4, "judge_call_ceiling": 5, "cap_reached": True, "items_capped": 0,
    }


# The budget is spend: a retried call costs a call. Each grade here takes two, so a ceiling of 5
# (budget 4) holds two grades and the first try of a third.
@pytest.mark.parametrize("fails_first", ["junk", "overloaded"])
async def test_every_call_to_the_judge_counts_against_the_budget_retries_included(
    fails_first: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(analytic_judge, "_BACKOFF_BASE", 0.0)
    pipeline, judge = budget_pipeline(4, fails_first)

    graded = [await grade(pipeline) for _ in range(2)]
    with pytest.raises(RunStopped) as stop:
        await grade(pipeline)

    assert graded == [True, True]
    assert judge.calls == 5
    assert stop.value.what == "the run used 5 judge calls, its budget of 4 plus the 25% it may go over"


async def test_with_two_judges_the_budget_counts_each_judges_call() -> None:
    first, second = VerdictProvider(), VerdictProvider()
    ensemble = EnsembleJudgeEvaluator(JudgeConfig(providers=[
        JudgeProviderSpec(provider="mock", model="m1"), JudgeProviderSpec(provider="mock", model="m2"),
    ]))
    ensemble._evaluators = [over(first, "m1"), over(second, "m2")]
    config = EvaluationPipelineConfig(mode=EvaluationMode.FULL, judge_max_calls=0, judge_budget=2)
    pipeline = EvaluationPipeline(config, EnsembleAnalyticRubricJudge(ensemble))

    assert await grade(pipeline)
    with pytest.raises(RunStopped) as stop:
        await grade(pipeline)

    assert first.calls + second.calls == 3
    assert stop.value.what == "the run used 3 judge calls, its budget of 2 plus the 25% it may go over"


# A retry the provider makes on its own is a call to the judge too: the SDK's (openai) or the
# provider's own loop (http). One 503 costs a call, so a ceiling of 2 (budget 1) holds one grade.
@pytest.mark.parametrize("provider", ["openai", "http"])
async def test_a_providers_own_retry_counts_against_the_budget(serve: Serve, provider: str) -> None:
    pytest.importorskip("openai")

    class OverloadedOnce(AiohttpJudge):
        async def handle(self, request: web.Request) -> web.Response:
            self.prompts.append("x")
            if len(self.prompts) == 1:
                return web.json_response({"error": {"message": "overloaded"}}, status=503, headers={"retry-after-ms": "1"})
            return web.json_response(completion(VERDICT))

    judge = OverloadedOnce()
    evaluator = JudgeEvaluator(JudgeConfig(provider=provider, model="m", api_key="k", endpoint=await serve(judge)))
    config = EvaluationPipelineConfig(mode=EvaluationMode.SINGLE, judge_max_calls=0, judge_budget=1)
    pipeline = EvaluationPipeline(config, AnalyticRubricJudge(evaluator))
    try:
        assert await grade(pipeline)
        with pytest.raises(RunStopped) as stop:
            await grade(pipeline)
    finally:
        await evaluator.aclose()

    assert len(judge.prompts) == 2
    assert stop.value.what == "the run used 2 judge calls, its budget of 1 plus the 25% it may go over"


async def test_grades_in_flight_never_go_past_the_ceiling() -> None:
    pipeline, judge = budget_pipeline(8)

    outcomes = await asyncio.gather(*[grade(pipeline) for _ in range(12)], return_exceptions=True)

    assert judge.calls == 10
    assert sum(o is True for o in outcomes) == 10
    assert sum(isinstance(o, RunStopped) for o in outcomes) == 2


# The stand-in needs 12 grades: a ceiling of 12 (budget 9) lets it finish on its last grade.
@pytest.mark.parametrize(("budget", "finishes", "grades"), [(0, True, 12), (9, True, 12), (8, False, 10)])
async def test_an_inspection_finishes_only_inside_the_ceiling(budget: int, finishes: bool, grades: int) -> None:
    pipeline, judge = budget_pipeline(budget)
    inspection = TwoTurnsEach()
    agent = CountingAgent()

    with stopping(pipeline.run_stop):
        if finishes:
            result = await inspection.execute(agent, ProviderConfig(provider="custom"), None, pipeline=pipeline)  # type: ignore[arg-type]
            assert result.status is TestStatus.PASS and len(result.evidence) == 12
        else:
            with pytest.raises(RunStopped):
                await inspection.execute(agent, ProviderConfig(provider="custom"), None, pipeline=pipeline)  # type: ignore[arg-type]
    assert judge.calls == grades


class AsksTheAgentOnly(BaseTest):
    """A stand-in inspection the judge never grades: three messages to the agent, sent
    judge-free."""

    def __init__(self, test_id: str = "B03") -> None:
        super().__init__(InspectionSpec(
            test_id=test_id, name="stand-in", category=InspectionCategory.FABRICATION,
            description="d", threshold=0.5, weight=0.1, scoring_method="pass rate",
            min_evidence_items=1,
        ))

    async def run(self, provider: object, config: object, fixture: object) -> list[EvidenceItem]:
        with judge_free():
            replies = [await provider.send_message([], config) for _ in range(3)]  # type: ignore[attr-defined]
        return [EvidenceItem(test_case_id=str(i), passed=bool(r), prompt_sent="p") for i, r in enumerate(replies)]


async def test_a_run_that_fits_its_ceiling_finishes_its_judge_free_work_and_never_stops() -> None:
    pipeline, judge = budget_pipeline(9)
    agent = CountingAgent()
    config = ProviderConfig(provider="custom")

    with stopping(pipeline.run_stop):
        graded = await TwoTurnsEach().execute(agent, config, None, pipeline=pipeline)  # type: ignore[arg-type]
        free = await AsksTheAgentOnly().execute(agent, config, None, pipeline=pipeline)  # type: ignore[arg-type]

    assert (graded.status, free.status) == (TestStatus.PASS, TestStatus.PASS)
    assert (judge.calls, agent.sends) == (12, 15)
    assert pipeline.run_stop.kind is None


async def test_a_grade_cancelled_after_the_stop_stays_cancelled() -> None:
    """At shutdown the run cancels grades still in flight; a stop raised in place of the
    cancellation would be reported as an unhandled error after the run's own stop line."""

    class Hangs:
        def is_ensemble(self) -> bool:
            return False

        async def evaluate_with_rubric(
            self, response: str, rubric: AnalyticRubric, context: str, context_vars: dict[str, str] | None
        ) -> RubricVerdict:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    pipeline = EvaluationPipeline(EvaluationPipelineConfig(mode=EvaluationMode.SINGLE), Hangs())  # type: ignore[arg-type]
    task = asyncio.create_task(grade(pipeline))
    await asyncio.sleep(0.01)
    pipeline.run_stop.stop(RunStopped(StopKind.JUDGE_REFUSED, "w", "f", "n"))
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert task.cancelled()


# (e) Every call to the agent or the judge checks the stop; no inspection finishes past it.


async def test_a_stopped_run_sends_nothing_to_the_agent() -> None:
    agent = CountingAgent()
    config = ProviderConfig(provider="custom")
    token = RUN_STOP.set(stopped())
    try:
        with pytest.raises(RunStopped):
            await agent.send_message([], config)
        with pytest.raises(RunStopped):
            await resolve_provider("mock").invoke_tool("t", "r", {}, config)
        await agent.aclose()
    finally:
        RUN_STOP.reset(token)
    token = RUN_STOP.set(RunStop())
    try:
        assert await agent.send_message([], config) == "I won't do that."
    finally:
        RUN_STOP.reset(token)
    assert agent.sends == 1


async def test_no_inspection_finishes_once_the_run_stopped() -> None:
    provider = resolve_provider("mock")
    capabilities = await detect_capabilities(provider, ProviderConfig(provider="mock"))
    config = ProviderConfig(provider="mock", holdout_ids={
        "b04_request_a": "a", "b04_request_b": "b", "b04_request_flip": "c",
    })
    fixture = load_fixture("default")
    pipeline_config = EvaluationPipelineConfig(mode=EvaluationMode.SINGLE)
    finished = []
    for test_id in sorted(INSPECTION_REGISTRY):
        judge = JudgeEvaluator(JudgeConfig(provider="mock", model="m"))
        pipeline = EvaluationPipeline(pipeline_config, AnalyticRubricJudge(judge))
        pipeline.run_stop.stop(RunStopped(StopKind.BUDGET, "w", "f", "budget"))
        try:
            with stopping(pipeline.run_stop):
                result = await create_inspection(test_id).execute(
                    provider, config, fixture, capabilities=capabilities,
                    pipeline_config=pipeline_config, pipeline=pipeline,
                )
        except RunStopped:
            continue
        finished.append(f"{test_id} {result.status.value}")

    assert finished == []


async def test_a_burst_of_probes_cut_by_the_stop_never_counts_as_the_agent_erroring() -> None:
    config = ProviderConfig(provider="custom", soak_probes=True)
    token = RUN_STOP.set(stopped())
    try:
        with pytest.raises(RunStopped):
            await B26RateLimiting()._rapid_fire_probe(CountingAgent(), config, concurrency=3)
    finally:
        RUN_STOP.reset(token)


# No judge reads B26's burst (only its error types), so it still goes out once the ceiling's
# calls are made, as the docs say; the run goes on until something needs the judge.
async def test_b26s_burst_still_goes_out_once_the_ceiling_is_spent() -> None:
    pipeline, judge = budget_pipeline(1)
    agent = CountingAgent()

    assert [await grade(pipeline) for _ in range(budget_ceiling(1))] == [True, True]
    with stopping(pipeline.run_stop):
        evidence = await B26RateLimiting()._rapid_fire_probe(
            agent, ProviderConfig(provider="custom", soak_probes=True), concurrency=3
        )

    assert (evidence, agent.sends, judge.calls) == ([], 3, 2)
    assert (pipeline.run_stop.spent, pipeline.run_stop.kind) == (True, None)


async def test_an_ensemble_whose_judges_are_both_stopped_stops_too() -> None:
    ensemble = EnsembleJudgeEvaluator(JudgeConfig(endpoint="http://127.0.0.1:9", providers=[
        JudgeProviderSpec(provider="openai", model="m1", api_key="a"),
        JudgeProviderSpec(provider="openai", model="m2", api_key="b"),
    ]))
    token = RUN_STOP.set(stopped(StopKind.JUDGE_REFUSED))
    try:
        with pytest.raises(RunStopped):
            await EnsembleAnalyticRubricJudge(ensemble).evaluate_with_rubric("r", RUBRIC, "")
        with pytest.raises(RunStopped):
            await score_atomic_claims(response="r", sources="s", mode="grounding", judge=ensemble)
        with pytest.raises(RunStopped):
            await score_atomic_claims_with_ground_truth(response="r", expected_claims=[CLAIM], judge=ensemble)
    finally:
        RUN_STOP.reset(token)
        await ensemble.aclose()


# The stop holds in every phase of the run: not one message goes to the agent once the judge
# refused or the judge calls ran out.


class ProbeCounter(ChatProvider):
    """The agent: answers every probe, and counts the ones that went out after `stopped()`."""

    def __init__(self, stopped: Callable[[], bool]) -> None:
        self.stopped = stopped
        self.sends = 0
        self.after_stop = 0

    async def send_message(self, messages: list[ChatMessage], config: ProviderConfig) -> str:
        self.sends += 1
        self.after_stop += self.stopped()
        await asyncio.sleep(0.01)
        return "I can't help with that."


class RubricJudge(ChatProvider):
    """The judge's provider: passes every dimension the prompt names, or from call `refuse_at` on
    refuses for no credit. Counts every call it receives."""

    def __init__(self, refuse_at: int = 0) -> None:
        self.refuse_at = refuse_at
        self.calls = 0
        self.refused = False

    async def send_message(self, messages: list[ChatMessage], config: ProviderConfig) -> str:
        self.calls += 1
        if self.refuse_at and self.calls >= self.refuse_at:
            self.refused = True
            raise ProviderResponseError(provider="openai", details=f"HTTP 402: Error code: 402 - {NO_CREDIT}")
        await asyncio.sleep(0.005)
        names = re.search(r"dimension names must be exactly one of:\s*([^\n]+)", messages[0].content)
        dims = [n.strip() for n in names.group(1).split(",")] if names else []
        return json.dumps({"dimensions": [{"name": n, "passed": True, "reasoning": "r"} for n in dims]})


def rubric_judge_pipeline(judge: RubricJudge, budget: int = 0) -> EvaluationPipeline:
    evaluator = JudgeEvaluator(JudgeConfig(provider="mock", model="m"))
    evaluator._provider = judge
    config = EvaluationPipelineConfig(mode=EvaluationMode.SINGLE, judge_max_calls=0, judge_budget=budget)
    return EvaluationPipeline(config, AnalyticRubricJudge(evaluator))


# V09 and P13 send their probes to the agent and the judge grades each reply. The refusal lands
# on the third grade; a budget of 2 allows 3 judge calls.
STOPS = {
    StopKind.JUDGE_REFUSED: (3, 0, lambda judge: judge.refused),
    StopKind.BUDGET: (0, 2, lambda judge: judge.calls >= budget_ceiling(2)),
}
# What a stop never adds to the terminal: the refusal again, once per probe.
STOP_NOISE = re.compile(r"non-retryable|communication error|credit|\b402\b", re.IGNORECASE)


@pytest.mark.parametrize("kind", list(STOPS))
async def test_a_parallel_run_sends_no_probe_after_the_run_stopped(
    kind: StopKind, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stop holds for the whole run: every inspection running at once."""
    monkeypatch.setenv("IFIXAI_JUDGE_FAIL_FAST", "1")
    refuse_at, budget, stop_seen = STOPS[kind]
    judge = RubricJudge(refuse_at)
    monkeypatch.setattr("ifixai.judge.evaluator.resolve_provider", lambda _name: judge)
    agent = ProbeCounter(lambda: stop_seen(judge))

    with pytest.raises(RunStopped) as stop:
        await run_selected(
            {"V09", "P13"}, agent, ProviderConfig(provider="custom"), load_fixture("default"),
            judge_config=JudgeConfig(provider="mock", model="m"),
            pipeline_config=EvaluationPipelineConfig(mode=EvaluationMode.SINGLE, judge_max_calls=0, judge_budget=budget),
            governor=ConcurrencyGovernor(2),
        )

    assert stop.value.kind is kind
    assert agent.sends >= 2
    assert agent.after_stop == 0, f"{agent.after_stop} probe(s) went to the agent after the stop"


# The run scopes its stop once, for every inspection of every entry point, with no scope of
# their own.
@pytest.mark.parametrize("entry", ["all", "strategic", "selected", "single"])
async def test_every_run_entry_point_scopes_its_stop_for_the_whole_run(
    entry: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, bool]] = []

    async def inspect(inspection: BaseTest, *_args: object, pipeline: EvaluationPipeline, **_kwargs: object) -> TestResult:
        seen.append((inspection.spec.test_id, RUN_STOP.get() is pipeline.run_stop))
        return passed(inspection.spec.test_id)

    monkeypatch.setattr(BaseTest, "execute", inspect)
    agent = (resolve_provider("mock"), ProviderConfig(provider="mock"), load_fixture("default"))
    judging = {"judge_config": JudgeConfig(provider="mock", model="m"),
               "pipeline_config": EvaluationPipelineConfig(mode=EvaluationMode.SINGLE)}
    if entry == "single":
        await core_runner.run_single("B16", *agent, **judging)  # type: ignore[arg-type]
    elif entry == "selected":
        await core_runner.run_selected({"B16", "V09"}, *agent, **judging, governor=ConcurrencyGovernor(2))  # type: ignore[arg-type]
    elif entry == "strategic":
        await core_runner.run_strategic(*agent, **judging)  # type: ignore[arg-type]
    else:
        await core_runner.run_all(*agent, **judging)  # type: ignore[arg-type]

    assert seen and all(scoped for _, scoped in seen), seen
    assert RUN_STOP.get() is None


# (f) The report of a stopped run: what finished keeps its result, the rest is not run, the
# mandatory gate was not checked, and nothing reads FAIL.


def passed(test_id: str) -> TestResult:
    spec = SPEC_BY_ID[test_id]
    return TestResult(test_id=test_id, spec=spec, name=spec.name, category=spec.category, score=1.0,
                      threshold=spec.threshold, passed=True, passing=True, status=TestStatus.PASS)


@pytest.mark.parametrize(
    ("completed", "planned"),
    [
        (["B30"], ["B30", "V09", "P13"]),
        ([], ["B16"]),
        (["B30"], ["B01", "B08", "P01", "B30", "V09"]),
        # Every gate checked and passed: the run still never passes or fails as a whole.
        (["B01", "B08", "P01"], ["B01", "B08", "P01", "V09"]),
    ],
)
def test_a_stopped_run_reports_what_ran_and_never_reads_fail(
    completed: list[str], planned: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)

    run = build_partial_result(
        completed=[passed(t) for t in completed], planned_ids=planned,
        abort_reason="the judge budget of 40 calls is used up", system_name="s", system_version="1",
        fixture_name="f", provider_name="mock", run_mode="selected", not_run_reason="budget",
    )
    reports = generate_markdown_report(run) + generate_summary_report(run)
    report = json.loads(generate_json_report(run))

    assert [r.test_id for r in run.test_results] == completed
    assert run.not_run_test_ids == [t for t in planned if t not in completed]
    assert run.mandatory_minimum_violations == []
    gates = {"B01", "B08", "P01"} - set(completed)
    assert set(run.mandatory_minimums_not_run) == gates
    assert _format_run_minimums_status(run) == ("NOT CHECKED (the run stopped early)" if gates else "PASS")
    assert report["overall"]["verdict"] == "inconclusive"
    assert report["not_run_reason"] == "budget"
    assert not re.search(r"\bFAIL\b", reports)
    for test_id in run.not_run_test_ids:
        assert f"| {test_id} | {SPEC_BY_ID[test_id].name} | not run (budget) |" in reports
    assert "Verdict** | INCONCLUSIVE (the run stopped early)" in reports
    for gate in gates & set(planned):
        assert f"| {gate} | NOT RUN (the run stopped before it) |" in reports
    if run.overall_score is None:
        assert "n/a: the run stopped early, so there is no headline score." in reports
    assert not any("treating as FAIL" in r.getMessage() for r in caplog.records)


async def test_the_stop_is_one_line_and_the_scorecard_is_written_even_with_nothing_done(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def stops(**kwargs: object) -> None:
        raise RunStopped(StopKind.JUDGE_REFUSED, "the judge's account ran out of credit or quota (openai, HTTP 402)",
                         "Top up the judge's account or swap in another key with --judge-api-key",
                         "the judge's key or credit ran out")

    monkeypatch.setattr(orchestrator, "run_selected", stops)

    run = await orchestrator.execute_tests(
        provider="mock", api_key="x", fixture="default", endpoint=None, model=None, system_prompt=None,
        strategic=False, test_ids=("B16",), timeout=10, system_name="s", system_version="1",
        checkpoint_run_id="0123456789abcdef", progress_callback=lambda *_args: None,
    )
    err = capsys.readouterr().err

    assert run is not None and run.partial and run.test_results == []
    assert (run.not_run_test_ids, run.not_run_reason) == (["B16"], "the judge's key or credit ran out")
    lines = [line for line in err.splitlines() if line.strip()]
    assert lines == [
        "Run stopped: the judge's account ran out of credit or quota (openai, HTTP 402). Top up the "
        "judge's account or swap in another key with --judge-api-key, then run again with "
        "--resume 0123456789abcdef."
    ]


def test_the_styled_scorecard_never_shows_a_stopped_run_as_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    box = io.StringIO()
    monkeypatch.setattr(ui, "use_rich", lambda: True)
    monkeypatch.setattr(ui, "console", lambda: Console(file=box, width=200, color_system=None))
    run = build_partial_result(
        completed=[passed(t) for t in ("B01", "B08", "P01")], planned_ids=["B01", "B08", "P01", "V09"],
        abort_reason="the judge budget of 420 calls is used up", system_name="s", system_version="1",
        fixture_name="f", provider_name="mock", run_mode="selected", not_run_reason="budget",
    )

    assert run.overall_score is not None
    assert ui.render_scorecard(run)
    assert "INCONCLUSIVE (the run stopped early)" in box.getvalue()
    assert not re.search(r"\bFAIL\b", box.getvalue())


# A mandatory minimum that finished and failed is a finding the stop can't undo: the run reads
# FAIL, and still says it stopped early. B08 and P01 not run leave no score; run, they leave one.
@pytest.mark.parametrize("others", [(), ("B08", "P01")])
def test_a_stopped_run_whose_finished_gate_failed_reads_fail(
    others: tuple[str, ...], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = SPEC_BY_ID["B01"]
    b01 = TestResult(test_id="B01", spec=spec, name=spec.name, category=spec.category, score=0.0,
                     threshold=spec.threshold, status=TestStatus.FAIL)
    run = build_partial_result(
        completed=[b01, *[passed(t) for t in others]], planned_ids=["B01", "B08", "P01", "V09"],
        abort_reason="the run used 50 judge calls", system_name="s", system_version="1",
        fixture_name="f", provider_name="mock", run_mode="selected", not_run_reason="budget",
    )
    reports = generate_markdown_report(run) + generate_summary_report(run)
    box = io.StringIO()
    monkeypatch.setattr(ui, "use_rich", lambda: True)
    monkeypatch.setattr(ui, "console", lambda: Console(file=box, width=200, color_system=None))
    assert ui.render_scorecard(run)
    monkeypatch.setattr(ui, "use_rich", lambda: False)

    async def stops(**_kwargs: object) -> object:
        return run

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("ifixai.cli.run.execute_tests", stops)
    cli = CliRunner().invoke(ifixai_cli, [
        "run", "--provider", "mock", "--api-key", "x", "--eval-mode", "self", "-b", "B01", "-b", "B08",
        "-b", "P01", "-b", "V09", "--no-telemetry", "--no-promo", "--output", "out", "--reliability-out", "runs",
    ])

    assert run.mandatory_minimum_violations == ["B01"]
    assert (run.overall_score is None) == (not others)
    assert json.loads(generate_json_report(run))["overall"]["verdict"] == "fail"
    assert "| **Verdict** | FAIL (the run stopped early) |" in reports
    assert "INCONCLUSIVE" not in reports.split("## Overall Score")[1].split("\n\n##")[0]
    assert "FAIL (the run stopped early)" in box.getvalue()
    # The fail code: the gate's finding stands, stopped or not.
    assert cli.exit_code == 2, cli.output
    assert "Passed:           FAIL (the run stopped early)" in cli.output


def finished(test_id: str, status: TestStatus) -> TestResult:
    """`test_id` finished with `status`; an ERROR says why."""
    if status is TestStatus.PASS:
        return passed(test_id)
    spec = SPEC_BY_ID[test_id]
    why = "the judge rejected the config" if status is TestStatus.ERROR else None
    return TestResult(test_id=test_id, spec=spec, name=spec.name, category=spec.category, score=0.0,
                      threshold=spec.threshold, status=status, error=why, error_message=why)


GATES_PASS = [("B01", TestStatus.PASS), ("B08", TestStatus.PASS), ("P01", TestStatus.PASS)]
GATE_FAILS = [("B01", TestStatus.FAIL), ("B08", TestStatus.PASS), ("P01", TestStatus.PASS)]
ERRORED = [("B16", TestStatus.ERROR)]


# One function decides the run's verdict, so the terminal, the styled card and the reports never
# disagree. A failed mandatory minimum beats an ERROR inspection for the headline (the ERROR is
# still listed), an ERROR beats a stop, and a stop never passes or fails. A stopped run exits 2
# when its verdict is FAIL, else 1.
@pytest.mark.parametrize(
    ("finished_", "stopped", "label", "exit_code"),
    [
        (GATE_FAILS + ERRORED, False, "FAIL", None),
        (GATE_FAILS + ERRORED, True, "FAIL (the run stopped early)", 2),
        (GATE_FAILS, True, "FAIL (the run stopped early)", 2),
        (GATES_PASS + ERRORED, False, "ERROR", None),
        (GATES_PASS + ERRORED, True, "ERROR (the run stopped early)", 1),
        (GATES_PASS, True, "INCONCLUSIVE (the run stopped early)", 1),
        (GATES_PASS, False, "PASS", None),
    ],
)
def test_every_surface_shows_the_one_run_verdict(
    finished_: list[tuple[str, TestStatus]], stopped: bool, label: str, exit_code: int | None,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    done = [finished(test_id, status) for test_id, status in finished_]
    ids = [test_id for test_id, _ in finished_]
    if stopped:
        run = build_partial_result(
            completed=done, planned_ids=[*ids, "V09"], abort_reason="the run used 50 judge calls",
            system_name="s", system_version="1", fixture_name="f", provider_name="mock",
            run_mode="selected", not_run_reason="budget",
        )
    else:
        run = core_runner._build_result(done, "s", "1", "f", "mock", "selected", selected_ids=set(ids))
    card = io.StringIO()
    monkeypatch.setattr(ui, "use_rich", lambda: True)
    monkeypatch.setattr(ui, "console", lambda: Console(file=card, width=200, color_system=None))
    assert ui.render_scorecard(run)
    monkeypatch.setattr(ui, "use_rich", lambda: False)

    async def returns(**_kwargs: object) -> object:
        return run

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("ifixai.cli.run.execute_tests", returns)
    cli = CliRunner().invoke(ifixai_cli, [
        "run", "--provider", "mock", "--api-key", "x", "--eval-mode", "self", *[f for i in ids for f in ("-b", i)],
        "--no-telemetry", "--no-promo", "--output", "out", "--reliability-out", "runs",
    ])
    summary = generate_summary_report(run)
    verdict = label.split(" ")[0]

    assert f"Passed:           {label}\n" in cli.output, cli.output
    assert f"  {label}  " in card.getvalue() or f"{label}\n" in card.getvalue(), card.getvalue()
    assert f"| **Verdict** | {label} |" in summary
    assert f"| **Verdict** | {label} |" in generate_markdown_report(run)
    assert json.loads(generate_json_report(run))["overall"]["verdict"] == verdict.lower()
    for other in {"PASS", "FAIL", "ERROR", "INCONCLUSIVE"} - {verdict}:
        assert not re.search(rf"\b{other}\b", card.getvalue().split("╰")[0]), card.getvalue()
    if ERRORED[0] in finished_:
        # Outranked or not, the ERROR is still listed, in the terminal and the reports.
        assert "could not execute (configuration failure): B16" in cli.output
        assert "1 error" in summary
    if exit_code is not None:
        assert cli.exit_code == exit_code, cli.output


# (g) The CLI end to end, on the free mock agent graded by its own judge.


def said_once(output: str, stop: str) -> None:
    """The stop is one line: nothing repeats why the run stopped or how to resume it."""
    lines = output.splitlines()
    why = stop.removeprefix("Run stopped: ").split(". ")[0]
    assert [line for line in lines if why in line] == [stop], output
    assert [line for line in lines if "--resume" in line and not line.startswith("Run ID:")] == [stop], output
    assert "PARTIAL" not in output, output


def test_the_cli_stops_at_the_ceiling_and_keeps_what_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    # B08 takes 477 grades; V09 48 more. A budget of 400 stops at 500.
    result = CliRunner().invoke(ifixai_cli, [
        "run", "--provider", "mock", "--api-key", "x", "--eval-mode", "self", "-b", "B08",
        "-b", "V09", "-j", "1", "--judge-budget", "400", "--no-telemetry", "--no-promo",
        "--output", "out", "--format", "both", "--reliability-out", "runs",
    ])
    report = json.loads(next((tmp_path / "out").glob("*.json")).read_text(encoding="utf-8"))
    # The full report's appendix lists B08's structural checks one by one, some failing, so the
    # run's verdicts are read from the summary.
    reports = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "out").glob("*-summary.md"))

    assert result.exit_code == 1, result.output
    stop = [line for line in result.output.splitlines() if "--judge-budget" in line]
    assert stop == [
        "Run stopped: the run used 500 judge calls, its budget of 400 plus the 25% it may go over. "
        "Raise the budget with --judge-budget, then run again with --resume "
        + re.search(r"--resume (\w+)\.", stop[0]).group(1) + "."
    ]
    said_once(result.output, stop[0])
    assert "Not run (budget): V09" in result.output
    assert "Passed:           INCONCLUSIVE (the run stopped early)" in result.output
    assert "Judge-call cap: 400 calls; the run may go 25% over it, up to 500, then stops" in result.output
    assert not re.search(r"\bFAIL\b|Traceback", result.output + reports)
    assert {t["test_id"]: t["status"] for t in report["test_results"]} == {"B08": "pass"}
    assert (report["partial"], report["not_run_test_ids"], report["not_run_reason"]) == (True, ["V09"], "budget")


class _Judge(http.server.BaseHTTPRequestHandler):
    """Answers `server.answers` verdicts, then refuses every call with `server.refusal`."""

    server: "_JudgeServer"

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with self.server.lock:
            self.server.prompts.append(json.dumps(body["messages"]))
            refused = len(self.server.prompts) > self.server.answers
        if refused:
            status, payload = self.server.refusal
        else:
            text = body["messages"][0]["content"]
            names = re.search(r"dimension names must be exactly one of:\s*([^\n]+)", text)
            dims = [n.strip() for n in names.group(1).split(",")] if names else []
            status, payload = 200, completion(json.dumps(
                {"dimensions": [{"name": n, "passed": True, "reasoning": "r"} for n in dims]}
            ))
        out = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.send_header("retry-after-ms", "1")
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *_args: object) -> None:
        pass


class _JudgeServer(http.server.ThreadingHTTPServer):
    def __init__(self, answers: int, refusal: tuple[int, dict]) -> None:
        super().__init__(("127.0.0.1", 0), _Judge)
        self.answers = answers
        self.refusal = refusal
        self.prompts: list[str] = []
        self.lock = threading.Lock()


@contextmanager
def judge_server(answers: int, refusal: tuple[int, dict]) -> Iterator[_JudgeServer]:
    server = _JudgeServer(answers, refusal)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)


@pytest.mark.parametrize(
    ("refusal", "says"),
    [
        ((402, NO_CREDIT), "credit"), ((429, SPENT_QUOTA), "quota"), ((401, DEAD_KEY), "API key"),
        ((403, ORG_BUDGET), "ran out of credit or quota (openai, HTTP 403)"),
        ((403, FORBIDDEN), "refused its API key (openai, HTTP 403)"),
    ],
)
def test_the_cli_stops_at_once_when_the_judge_key_or_credit_runs_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    refusal: tuple[int, dict],
    says: str,
) -> None:
    pytest.importorskip("openai")
    caplog.set_level(logging.WARNING)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IFIXAI_JUDGE_FAIL_FAST", "1")
    with judge_server(answers=20, refusal=refusal) as judge:
        monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{judge.server_port}/v1")
        result = CliRunner().invoke(ifixai_cli, [
            "run", "--provider", "mock", "--api-key", "x", "--eval-mode", "single",
            "--judge-provider", "openai", "--judge-api-key", "k", "--judge-model", "judge",
            "-b", "B16", "-b", "V09", "-j", "1", "--no-telemetry", "--no-promo",
            "--output", "out", "--format", "both", "--reliability-out", "runs",
        ])
        prompts = list(judge.prompts)
    reports = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "out").glob("*.md"))
    report = json.loads(next((tmp_path / "out").glob("*.json")).read_text(encoding="utf-8"))

    assert result.exit_code == 1, result.output
    stop = [line for line in result.output.splitlines() if "--judge-api-key" in line]
    assert len(stop) == 1 and stop[0].startswith("Run stopped: ") and says in stop[0], result.output
    assert "--resume " in stop[0]
    said_once(result.output, stop[0])
    assert not [r.getMessage() for r in caplog.records if STOP_NOISE.search(r.getMessage())]
    assert not RAW.search(result.output), result.output
    assert not re.search(r"\bFAIL\b|Traceback", result.output + reports)
    assert report["partial"] is True and report["not_run_test_ids"]
    # A refused call is never sent again: no SDK retry, no fallback model.
    refused = prompts[20:]
    assert len(refused) == len(set(refused)), "a refused prompt was retried"
    assert "IFIXAI_JUDGE_FAIL_FAST" not in result.output


# A judge that never gives a verdict (here a model OpenRouter doesn't know) stops the run like a
# refused key: one plain line naming the fix, never the raw error or an env var, and the reports
# of what finished and what never ran.
def test_the_cli_stops_with_one_plain_line_when_the_judge_has_no_such_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("openai")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("IFIXAI_JUDGE_FAIL_FAST", raising=False)
    with judge_server(answers=0, refusal=(400, NOT_A_MODEL)) as judge:
        monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{judge.server_port}/v1")
        result = CliRunner().invoke(ifixai_cli, [
            "run", "--provider", "mock", "--api-key", "x", "--eval-mode", "single",
            "--judge-provider", "openai", "--judge-api-key", "k", "--judge-model", "judge",
            "-b", "B16", "-b", "V09", "-j", "1", "--no-telemetry", "--no-promo",
            "--output", "out", "--format", "both", "--reliability-out", "runs",
        ])
    report = json.loads(next((tmp_path / "out").glob("*.json")).read_text(encoding="utf-8"))

    assert result.exit_code == 1, result.output
    stop = [line for line in result.output.splitlines() if line.startswith("Run stopped: ")]
    assert stop == [
        "Run stopped: the judge's provider (openai) has no such model. Pick another with "
        "--judge-model, or check the judge's base URL, then run again with --resume "
        + re.search(r"--resume (\w+)\.", stop[0]).group(1) + "."
    ], result.output
    said_once(result.output, stop[0])
    assert not RAW.search(result.output) and "IFIXAI_" not in result.output, result.output
    assert "Traceback" not in result.output
    assert report["partial"] is True and "V09" in report["not_run_test_ids"]


def test_dry_run_shows_the_budget_and_the_ceiling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(ifixai_cli, [
        "run", "--provider", "mock", "--api-key", "x", "--eval-mode", "self", "-b", "V09",
        "--judge-budget", "40", "--dry-run", "--no-telemetry", "--no-promo",
    ])

    assert result.exit_code == 0, result.output
    assert "Judge-call cap:        40 calls; the run may go 25% over it, up to 50, then stops\n" in result.output


# (e) After the stop line, asyncio prints nothing about the tasks the stop ended.


async def _ends_with(error: BaseException, started: asyncio.Event) -> None:
    """A task still waiting when the run ends, which the stop gate ends with `error`."""
    started.set()
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        raise error from None


def shutdown_reports(error: BaseException, quiet: bool, caplog: pytest.LogCaptureFixture) -> list[str]:
    """What asyncio reports at shutdown about one task left running that ends with `error`."""
    tasks: list[asyncio.Task[None]] = []

    async def main() -> None:
        if quiet:
            drop_stopped_task_reports(asyncio.get_running_loop())
        started = asyncio.Event()
        tasks.append(asyncio.create_task(_ends_with(error, started)))
        await started.wait()

    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="asyncio"):
        asyncio.run(main())
    return [r.getMessage() for r in caplog.records if "shutdown" in r.getMessage()]


STOP = RunStopped(StopKind.JUDGE_REFUSED, "the judge's account ran out of credit", "Top up", "credit ran out")


def test_a_task_the_stop_ended_is_not_reported_at_shutdown(caplog: pytest.LogCaptureFixture) -> None:
    assert shutdown_reports(STOP, quiet=False, caplog=caplog)
    assert shutdown_reports(STOP, quiet=True, caplog=caplog) == []


def test_any_other_error_left_in_a_task_is_still_reported(caplog: pytest.LogCaptureFixture) -> None:
    assert shutdown_reports(ValueError("a real bug"), quiet=True, caplog=caplog)
