"""A real endpoint on a bundled fixture runs without iFixAi's simulated governance;
`--governance` reaches the run; library callers keep the fixture's own block."""

import http.server
import json
import re
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from ifixai.api import run_selected, run_single
from ifixai.cli.main import ifixai_cli
from ifixai.core.fixture_loader import load_fixture
from ifixai.providers.governance_fixture import GovernanceFixture

DEFAULT_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "default" / "fixture.yaml"
EXAMPLES = DEFAULT_FIXTURE.parents[1] / "examples"
STRUCTURAL = ["P08", "M06", "M07"]


class _Agent(http.server.BaseHTTPRequestHandler):
    """A plain chat agent: no tools, no audit trail. Also answers as a LangServe app (POST .../invoke)."""

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers["Content-Length"]))
        reply = "I can't do that."
        answer = {"output": reply} if self.path.endswith("/invoke") else {
            "choices": [{"finish_reason": "stop", "message": {"content": reply}}]}
        body = json.dumps(answer).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def agent() -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Agent)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)


def _run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, endpoint: str, *extra: str, provider: str = "http"
) -> tuple[str, dict[str, dict]]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IFIXAI_TELEMETRY", "0")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    out = tmp_path / "reports"
    args = [
        "run", "--provider", provider, "--endpoint", endpoint, "--auth-method", "none",
        "--eval-mode", "single", "--judge-provider", "mock", "--no-telemetry", "--no-parallel",
        "--format", "json", "--output", str(out), "--reliability-out", str(tmp_path / "reliability"),
        *[a for tid in STRUCTURAL for a in ("--test", tid)], *extra,
    ]
    result = CliRunner().invoke(ifixai_cli, args)
    reports = list(out.glob("*.json"))
    assert len(reports) == 1, result.output
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    return result.output, {t["test_id"]: t for t in report["test_results"]}


@pytest.mark.parametrize(("provider", "extra"), [
    ("http", ()), ("http", ("-f", "default")), ("langchain", ("--api-key", "k")),
    ("http", ("-f", "customer_support")), ("http", ("-f", str(EXAMPLES / "customer_support.yaml"))),
])
def test_real_endpoint_on_a_bundled_fixture_gets_no_simulated_governance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent: str, provider: str, extra: tuple[str, ...]
) -> None:
    output, results = _run(tmp_path, monkeypatch, agent, *extra, provider=provider)
    assert {tid: t["status"] for tid, t in results.items()} == dict.fromkeys(STRUCTURAL, "inconclusive"), output
    assert "Pass --governance" in output


def test_real_endpoint_on_own_fixture_keeps_its_governance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent: str
) -> None:
    # The same block, copied out of the package, is the user's own declared policy.
    own = tmp_path / "own.yaml"
    own.write_text((EXAMPLES / "customer_support.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    output, results = _run(tmp_path, monkeypatch, agent, "-f", str(own))
    assert {tid: t["status"] for tid, t in results.items()} == dict.fromkeys(STRUCTURAL, "pass"), output
    assert "Governance: composed from fixture (source=explicit" in output
    assert "Pass --governance" not in output


def test_governance_flag_reaches_the_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent: str) -> None:
    governance = yaml.safe_load(DEFAULT_FIXTURE.read_text(encoding="utf-8"))["governance"]
    governance["version"] = "flag-policy-v7"
    # Double the default block's 4 seed audit records so B25's records=8 can only come from this file.
    seeds = governance["seed_audit_records"]
    governance["seed_audit_records"] = seeds + [{**s, "timestamp": f"2026-02-0{i + 1}T00:00:00Z"} for i, s in enumerate(seeds)]
    policy = tmp_path / "policy.yaml"
    policy.write_text(yaml.safe_dump(governance), encoding="utf-8")
    output, results = _run(tmp_path, monkeypatch, agent, "--governance", str(policy), "--test", "B25")
    assert {tid: results[tid]["status"] for tid in STRUCTURAL} == dict.fromkeys(STRUCTURAL, "pass"), output
    assert set(re.findall(r"records=\d+", json.dumps(results["B25"]["evidence"]))) == {"records=8"}
    assert "version=flag-policy-v7" in output
    assert "Pass --governance" not in output


async def test_library_caller_keeps_the_fixture_block(agent: str) -> None:
    run = await run_selected(set(STRUCTURAL), provider="http", endpoint=agent, auth_method="none", fixture="default")
    assert {t.test_id: t.status.value for t in run.test_results} == dict.fromkeys(STRUCTURAL, "pass")


@pytest.mark.parametrize(("decision", "status"), [({}, "pass"), ({"governance": None}, "inconclusive")])
async def test_run_single_takes_the_governance_decision(agent: str, decision: dict, status: str) -> None:
    result = await run_single("P08", provider="http", endpoint=agent, auth_method="none", fixture="default", **decision)
    assert result.status.value == status


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ("hello: world\n", "declares nothing to simulate"),
        ("", "declares nothing to simulate"),
        ("tools: []\n", "declares nothing to simulate"),
        ("version: v1\n", "declares nothing to simulate"),
        ("seed_audit_records: []\n", "declares nothing to simulate"),
        ('default_response: "ok"\n', "declares nothing to simulate"),
        ("policies: {}\naudit_log:\n  records: []\nper_test: {}\n", "declares nothing to simulate"),
        ("responses:\n  hi: there\nversion: v2\n", "declares nothing to simulate"),
        ("version: [unclosed\n  - : :\n", "while parsing a flow sequence"),
        ("seed_audit_records: oops\n", "file: seed_audit_records: Input should be a valid tuple"),
        ("tools:\n  - name: lookup\n", "file: tools.0.tool_id: Field required"),
        ("policies:\n  session:\n    cross_user_isolated: maybe\n",
         "file: policies.session.cross_user_isolated: Input should be a valid boolean"),
    ],
)
def test_unusable_governance_file_stops_before_any_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent: str, content: str, reason: str
) -> None:
    policy = tmp_path / "policy.yaml"
    policy.write_text(content, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IFIXAI_TELEMETRY", "0")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    result = CliRunner().invoke(ifixai_cli, [
        "run", "--provider", "http", "--endpoint", agent, "--auth-method", "none", "--eval-mode", "single",
        "--judge-provider", "mock", "--no-telemetry", "--test", "P08", "--governance", str(policy),
    ])
    # A config error, like every other one in `run`: 2 would read as a failed gate in CI.
    assert result.exit_code == 1, result.output
    errors = [line for line in result.output.splitlines() if line.startswith("Error:")]
    assert len(errors) == 1, result.output
    assert errors[0].startswith(f"Error: --governance {policy} is not a usable governance file: ")
    assert reason in errors[0]
    assert "Connected to" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


@pytest.fixture
def empty_block_fixture(tmp_path: Path) -> Path:
    """The default fixture with a `governance:` block that declares nothing to simulate."""
    data = yaml.safe_load(DEFAULT_FIXTURE.read_text(encoding="utf-8"))
    data["governance"] = {"version": "1.0.0"}
    path = tmp_path / "empty-governance.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.mark.parametrize(("provider", "extra"), [("http", ()), ("langchain", ("--api-key", "k"))])
def test_a_real_agent_skips_an_empty_governance_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent: str, empty_block_fixture: Path,
    provider: str, extra: tuple[str, ...],
) -> None:
    output, results = _run(tmp_path, monkeypatch, agent, "-f", str(empty_block_fixture), *extra, provider=provider)
    assert {tid: t["status"] for tid, t in results.items()} == dict.fromkeys(STRUCTURAL, "inconclusive"), output
    assert [line for line in output.splitlines() if "declares nothing to simulate" in line] == [
        "Governance: the fixture's governance: block declares nothing to simulate -- not composing it onto your agent."
    ]
    assert "composed from fixture" not in output


def test_mock_still_composes_an_empty_governance_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent: str, empty_block_fixture: Path
) -> None:
    # The mock still gets the empty block composed on, so it fails B02.
    output, results = _run(tmp_path, monkeypatch, agent, "-f", str(empty_block_fixture), "--test", "B02", provider="mock")
    assert results["B02"]["status"] == "fail", output
    assert "Governance: composed from fixture (source=explicit, version=1.0.0)." in output
    assert "declares nothing to simulate" not in output


@pytest.mark.parametrize(("provider", "extra", "warned"), [
    ("http", (), True), ("langchain", ("--api-key", "k"), True), ("mock", (), False),
])
def test_grounding_fixture_warns_on_a_real_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent: str, provider: str, extra: tuple[str, ...], warned: bool
) -> None:
    output, _ = _run(tmp_path, monkeypatch, agent, "--grounding", "fixture", *extra, provider=provider)
    assert ("(double-governing)" in output) is warned, output


def test_library_caller_keeps_an_empty_governance_block(empty_block_fixture: Path) -> None:
    fixture = load_fixture(empty_block_fixture)
    assert (fixture.governance, fixture.governance_source) == (GovernanceFixture(version="1.0.0"), "explicit")
