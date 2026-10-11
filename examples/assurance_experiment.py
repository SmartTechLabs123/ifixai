"""Run a real, entirely local paired experiment through the public CLI.

Usage: python examples/assurance_experiment.py --output /tmp/my-new-experiment

The endpoint and rubric evaluator are deliberately synthetic. This demonstrates
execution, pairing, reporting and replay; it is not evidence of model quality.
No credentials, model downloads or external services are used.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
from collections import Counter
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Support a source checkout as well as an editable installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ifixai.core.fixture_loader import load_fixture
from ifixai.inspections.b19_context_accuracy.runner import build_context_probes

DIMENSIONS = (
    "no_context_contradiction",
    "no_context_fabrication",
    "context_grounding_priority",
    "fact_recall_completeness",
    "no_unsupported_certainty",
)
GROUNDING_PROMPT = "For this demonstration, answer using only supplied session facts."


@contextmanager
def local_endpoint(*, response_suffix=""):
    """Owned loopback endpoint, using the shipped B19 probe facts as its oracle."""
    fixture = load_fixture("customer_support")
    answers = {
        probe.query_turn: probe.expected_fragment
        for probe in build_context_probes(fixture)
    }
    requests = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with lock:
                requests.append(payload)
            if self.path == "/retrieve":
                # Native provider capability discovery probes retrieval once.
                encoded = json.dumps({"sources": []}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
                return
            messages = payload["messages"]
            if payload.get("model") == "offline-fragment-judge":
                guidance = messages[0]["content"]
                expected = re.search(r"ground-truth fragment.*?is: '([^']+)'", guidance)
                # JudgeEvaluator sends the response in a separate envelope.
                response = messages[-1]["content"]
                passed = bool(expected and expected.group(1) in response)
                content = json.dumps({"dimensions": [
                    {"name": name, "passed": passed, "reasoning": "synthetic exact fragment oracle"}
                    for name in DIMENSIONS
                ]})
            elif payload.get("model") == "unavailable-sut":
                self.send_response(503)
                self.end_headers()
                return
            elif len([m for m in messages if m["role"] == "user"]) == 1:
                content = "Acknowledged."
            else:
                grounded = any(
                    GROUNDING_PROMPT in m["content"]
                    for m in messages if m["role"] == "system"
                )
                content = answers.get(messages[-1]["content"], "not stated") if grounded else "A default value applies."
            if payload.get("model") != "offline-fragment-judge":
                content += response_suffix
            encoded = json.dumps({"choices": [{
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def offline_plan(endpoint: str, *, control: str = "improvement", pairs: int = 2) -> dict:
    baseline = {"label": "baseline", "provider": "http", "model": "offline-sut", "endpoint": endpoint, "max_retries": 0}
    candidate = {**baseline, "label": "candidate", "system_prompt": GROUNDING_PROMPT}
    if control == "placebo":
        baseline["system_prompt"] = GROUNDING_PROMPT
    elif control == "regression":
        baseline["system_prompt"] = GROUNDING_PROMPT
        candidate.pop("system_prompt")
    elif control == "unscored":
        candidate["model"] = "unavailable-sut"
    return {
        "name": f"Synthetic B19 {control}",
        "intervention": "Illustrative context-grounding prompt, evaluated with an exact-fragment oracle",
        "fixture": "customer_support", "test_ids": ["B19"],
        "baseline": baseline, "candidate": candidate,
        "pairs": pairs, "seed": 42, "placebo": control == "placebo",
        "eval_mode": "single",
        "judge": {"provider": "http", "model": "offline-fragment-judge", "endpoint": endpoint},
        "policy": {"min_pairs": pairs, "required_test_ids": ["B19"]},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New directory; existing destinations are refused")
    parser.add_argument("--control", choices=["improvement", "placebo", "regression", "unscored"], default="improvement")
    args = parser.parse_args()
    args.output = args.output.absolute()
    if args.output.exists():
        parser.error("output directory must be new")
    plan_path = args.output.with_suffix(".plan.json")
    if plan_path.exists():
        parser.error("plan destination already exists")
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    with local_endpoint() as (endpoint, requests):
        plan_path.write_text(json.dumps(offline_plan(endpoint, control=args.control), indent=2), encoding="utf-8")
        command = [sys.executable, "-m", "ifixai.cli.main", "experiment", "run", str(plan_path), "--max-runs", "4", "--output", str(args.output)]
        completed = subprocess.run(command, check=False, cwd=Path(__file__).resolve().parents[1])
        # BLOCK or INCONCLUSIVE is an expected scientific outcome, not failure
        # of this example. Unexpected CLI errors remain visible and nonzero.
        if completed.returncode not in {1, 2}:
            raise RuntimeError(f"Synthetic example expected a non-PASS gate, exit={completed.returncode}")
        print("Synthetic HTTP requests:", dict(Counter(p.get("model", "") for p in requests)))
    replay = args.output.with_name(args.output.name + "-replay")
    completed = subprocess.run([sys.executable, "-m", "ifixai.cli.main", "experiment", "replay", str(args.output / "experiment.json"), "--output", str(replay)], check=False, cwd=Path(__file__).resolve().parents[1])
    if completed.returncode not in {1, 2}:
        raise RuntimeError(f"Synthetic replay expected a non-PASS gate, exit={completed.returncode}")
    print(f"Local synthetic evidence: {args.output}; offline replay: {replay}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
