# CLI reference

Every `ifixai` command and `ifixai run` flag. Guided first run: [get-started.md](get-started.md).

## Commands

```bash
ifixai init                  # check env for provider keys, suggest a first run
ifixai run                   # run the default 10 inspections (Standard or Full mode)
ifixai run --full-run        # run all 61 inspections
ifixai run --fixture FILE    # custom fixture (YAML or JSON)
ifixai list tests            # all 61 inspections (32 core + 29 extended)
ifixai list fixtures         # registered named fixtures
ifixai validate [FILE]       # per-test layout, or a fixture against schema.json
ifixai compare A B           # diff two scorecard reports
ifixai windtunnel controls   # which fixtures the reference provider satisfies
ifixai windtunnel sweep      # measure which inspections catch each seeded defect
ifixai windtunnel sweep --output base.json      # seal the measurement
ifixai windtunnel sweep --baseline base.json    # exit 1 if a detection was lost
ifixai run -p openai -k "$OPENAI_API_KEY" -c DECEPTION   # example: one category
```

`trajectory`, `policy-lab` and `windtunnel` are offline command groups that need
no provider key; see [trajectory assurance](trajectory-assurance.md),
[Business Policy Lab](business-policy-lab.md) and the
[evaluator wind tunnel](wind-tunnel.md).

## `ifixai run` flags

### System under test (SUT)

| Flag | Default | Does |
|---|---|---|
| `--provider`, `-p` | none | `mock`, `openai`, `openrouter`, `orcarouter`, `requesty`, `cloudflare`, `vercel`, `anthropic`, `gemini`, `azure`, `bedrock`, `huggingface`, `http`, `langchain`. |
| `--api-key`, `-k` | none | SUT API key. Always passed explicitly, never read from the environment. |
| `--model`, `-m` | provider default | Model identifier override. |
| `--endpoint`, `-e` | none | Endpoint URL (required for `http` and `azure`; for `cloudflare` it replaces the URL built from `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_GATEWAY_ID`). |
| `--system-prompt`, `-s` | none | Custom system instructions sent before each inspection. |
| `--grounding` | `sut` | Governance context source: `sut` (baked-in), `fixture` (system prompt derived from fixture), `none`. |
| `--sut-temperature` | `0.0` | SUT sampling temperature. B22 needs `0` or `--sut-seed`. |
| `--sut-seed` | none | SUT sampling seed (recorded in the manifest either way). |

### Judge

| Flag | Default | Does |
|---|---|---|
| `--eval-mode` | auto | `deterministic`, `single`, `full`, or `self`. See [How a run is judged](#how-a-run-is-judged). |
| `--judge-provider` | none | Judge provider. Repeat >=2 times for a Full-mode ensemble. |
| `--judge-api-key`, `--judge-model` | none | Key(s) and model(s), paired with `--judge-provider`. |
| `--judge-budget` | `0` | Judge budget per run, in calls to the judge's provider: retries and fallback models count, and in Full mode each judge's call. `0` = a runaway guard of 10x the estimate, at least 2,000. The run may go 25% over the budget; when it needs a call past that ceiling it stops, keeps what finished and lists the rest as not run (budget). `--dry-run` and the run header show both numbers. See [When a run stops early](#when-a-run-stops-early). |

### Fixture and governance

| Flag | Default | Does |
|---|---|---|
| `--fixture`, `-f` | auto | Fixture name or YAML/JSON path. |
| `--governance` | none | `GovernanceFixture` YAML path; structural inspections score against your declared policies. See [fixture_authoring.md](fixture_authoring.md). |
| `--mode` | `standard` | `standard` or `full` (hand-built fixture + >=2 distinct judge providers). |

### Suite subset

With none of these flags, `ifixai run` runs the 10-inspection `essential` suite: B01, B08, P01, B12, B09, B27 (high impact) and B06, B16, B18, B20 (medium impact). It always carries the three mandatory minimums, so it still produces a grade. What each one checks: [README](../README.md#what-a-default-run-covers).

| Flag | Does |
|---|---|
| `--full-run` | Every inspection (all 61) instead of the default 10. Cannot be combined with the flags below. Overrides a `suite:` saved in `ifixai.yaml`. Unrelated to `--mode full`. |
| `--suite` | A named suite: `smoke` (3), `strategic` (8), `essential` (10, the default), `core` (32), `extended` (29), `all` (61), or a theme (`security`, `reliability`, `compliance`, `frontier`). `ifixai list suites` shows them. |
| `--strategic` | Top 8 strategic tests only. |
| `--test`, `-b` | Test(s) by ID, repeatable: `-b B01 -b B08`. |
| `--category`, `-c` | Category name(s), case-insensitive, repeatable; beats `--strategic`. Names: `FABRICATION`, `MANIPULATION`, `DECEPTION`, `UNPREDICTABILITY`, `OPACITY`, `SABOTAGE`, `SUBVERSION`, `CONCEALMENT`, `SANDBAGGING`, `INSUBORDINATION`, `USURPATION`, `SYSTEMIC_RISK`, `MISCALIBRATION`, `STAKEHOLDER_CONFLICT`, `PERCEPTION_GOVERNANCE`, `OVERSIGHT_ATROPHY`, `PERSISTENCE`, `IDENTITY_ATTESTATION`, `INFLUENCE`, `BALANCE_INTEGRITY`, `FRANKNESS_CORRECTNESS_LINK`, `GRADER_VALIDITY`, `BENCHMARK_CONTAMINATION`, `TRAINING_DISPOSITION_PROVENANCE`, `VULNERABLE_USER_CARE`. |

### Output and reporting

| Flag | Default | Does |
|---|---|---|
| `--output`, `-o` | `./ifixai-results/` | Report directory. |
| `--format` | `both` | `json`, `markdown`, or `both`. |
| `--name`, `--version` | provider name, `1.0` | System name and version label in reports. |
| `--min-score` | `0.85` | Exit code `2` if overall score is below (CI gate). A run flagged RUN INVALID exits `2` whatever its score. A scoped (`-b`) or strategic run that leaves out B01, B08 or P01 has no overall score: it exits `0` if every selected scored inspection passed, `2` if any failed, errored or was inconclusive, and `--min-score` doesn't apply. Any other run with no overall score exits `2`. A partial (stopped) run exits `1`, or `2` when a mandatory minimum that finished failed. |
| `--quiet`, `-q` | off | Suppress banner and summary; stdout still carries scores. |

### Execution and reliability

| Flag | Default | Does |
|---|---|---|
| `--timeout`, `-t` | `30` | Per-request timeout in seconds. |
| `--concurrency`, `-j` | `5` | Max in-flight LLM requests (1-20). Overrides `IFIXAI_CONCURRENCY`. |
| `--no-parallel` | off | Alias for `--concurrency 1`. |
| `--dry-run` | off | Print inspection and judge-call estimates, then exit. |
| `--no-promo` | off | Skip the [Pro-version offer](#pro-version-offer) and start the run immediately. `IFIXAI_NO_PROMO=1` skips it on every run. |
| `--reliability-out` | `runs` | Directory for `manifest.json`, one subdir per run. |
| `--run-nonce` | fresh | Replay-protection nonce (16 hex chars), recorded in the manifest. |
| `IFIXAI_JUDGE_FALLBACKS` (env) | packaged JSON | Path to the judge fallback-model chain. See [Judge fallback models](#judge-fallback-models). |
| `--holdout-seed`, `--b{12,14,28,29,30,32}-seed` | fresh random | Pin, or set matching `IFIXAI_*_SEED`, to replay a run. See [reproducibility.md](reproducibility.md). |

## Pro-version offer

The first interactive `ifixai run` on a machine opens with one question: whether you'd like to claim your free fast audit at <https://www.ifixai.ai/>. It's shown once, whatever you answer (Ctrl-C included); later runs start straight away. The marker is a `pro-offer-shown` file in `$XDG_CONFIG_HOME/ifixai` (else `~/.config/ifixai`): delete it to see the offer again.

Move with the arrow keys and press Enter:

- **No** (preselected, just press Enter): the run starts.
- **Yes**: the site opens in your browser, then you pick **Continue the current run** (preselected) or **Abandon the run**. Abandoning exits `0` before anything happens: no provider calls, no telemetry, no run directory.

Ctrl-C on either question aborts without starting the run. Where the arrow-key menu cannot run (`NO_COLOR` is set), the same options are listed with numbers to type instead.

To start immediately, pass `--no-promo`, or set `IFIXAI_NO_PROMO=1` to skip it on every run:

```bash
ifixai run --provider openai --no-promo
export IFIXAI_NO_PROMO=1
```

The offer is never shown when stdin or stdout is not a terminal (pipes, scripts, agent-driven runs), in CI, or on `--dry-run`, so automated runs are never blocked by it. `--quiet` does not skip it; use `--no-promo`. A runner that allocates a pseudo-terminal without setting `CI` looks like a person at a keyboard: pass `--no-promo` (or set `IFIXAI_NO_PROMO=1`) there.

## How a run is judged

The SUT (system under test) is the agent being graded via the SUT flags above; its key is never read from the environment. The judge grades the SUT's answers; a citable grade needs a judge from a second, different provider.

### Judge auto-pairing

With no `--judge-*` flags, iFixAi picks a judge from a different provider whose key it finds in the environment, never the SUT's own. With only one credential it refuses unless you pass `--eval-mode self`. Preference order: [testing-your-agent.md](testing-your-agent.md#provider-reference).

### Evaluation modes (`--eval-mode`)

| Mode | What it does |
|---|---|
| `deterministic` | Structural inspections only; no judge call. |
| `single` | One cross-provider judge (`--judge-provider` required). |
| `full` | Multi-judge ensemble (>=2 `--judge-provider`; Full mode only). |
| `self` | SUT grades itself; grade prints but is flagged, not citable. |

### Standard vs Full

Mode changes how a run is judged, not which inspections run: the same selection either way (10 by default, all 61 with `--full-run`). Standard: auto fixture (the bundled default is a **seeded-defect demo** — expect B01 and P01 to FAIL by design on a default run, and more on a `--full-run`; see [`ifixai/fixtures/default/README.md`](../ifixai/fixtures/default/README.md)), one auto-paired judge. Full (`--mode full`): requires `--fixture` (the default fixture is refused) and >=2 judge providers; majority vote, tie-break `fail > partial > pass`.

```bash
# Standard: export a second provider key and the judge auto-pairs.
ifixai run --provider http --endpoint http://localhost:8000/v1 --api-key "$YOUR_TOKEN"

# Full: hand-built fixture, two independent judges.
ifixai run --mode full \
  --provider http --endpoint http://localhost:8000/v1 --api-key "$YOUR_TOKEN" \
  --fixture ./my-fixture.yaml \
  --judge-provider anthropic --judge-api-key "$ANTHROPIC_API_KEY" \
  --judge-provider openai    --judge-api-key "$OPENAI_API_KEY"
```

Judge design rationale: [methodology.md](methodology.md#cross-provider-judge-default).

### When a run stops early

Three things stop a run before it finishes, and none reads as the agent failing:

- **The judge's key or credit runs out.** The judge's provider refuses the call: a rejected key (401 or 403), no credit (402), a spent key or workspace budget (OpenRouter's 403 "budget limit exceeded"), or a spent quota (a 429 that says so). Every later call would be refused too, so the run stops at once: the refused call is not retried, and no more judge calls or probes to the agent go out. A plain 429 rate limit is still retried, and a 403 that flags a probe's content (a moderated judge model) leaves that one probe ungraded while the run goes on.
- **The judge never gives a verdict.** Before its first verdict, a judge model the provider doesn't know (a 404, or OpenRouter's 400 "is not a valid model ID") stops the run at once, and five rejected calls in a row stop it too.
- **The run spends its judge budget.** It may go 25% over `--judge-budget` (or the default cap), counted in calls to the judge's provider, each retry included. Once those calls are made, the next call to the judge or the next message to the agent stops the run the same way. Any message to the agent counts as a probe the judge will grade, from any inspection, even one that has not asked the judge anything yet, so no probe goes out that could not be graded. Only what no judge reads still goes out until the run stops: calls to the agent's own hooks, and B26's burst of rate-limit probes. A judge call already out when the run stops was inside the ceiling, so it is left to answer, but not retried.

You get one line saying what happened and how to fix it: top up the judge's account or swap in another key with `--judge-api-key`, pick another `--judge-model`, or raise `--judge-budget`, then run again with `--resume <run id>` and the same flags. Inspections that finished keep their results. The rest are listed as not run, in the terminal and in the reports. A mandatory minimum (B01, B08, P01) that never ran reads NOT CHECKED, never a fail. A stopped run never passes as a whole. Its verdict reads INCONCLUSIVE (the run stopped early), the same in the terminal's `Passed` line, the scorecard and the reports, unless a mandatory minimum that finished failed, which no later inspection can undo: then it reads FAIL (the run stopped early). An inspection that errored turns INCONCLUSIVE into ERROR, never a FAIL into ERROR; it is still listed with the errors. Reports are still written. The run exits `2` when its verdict is FAIL, `1` otherwise.

### Judge fallback models

When the judge provider itself errors — an OpenRouter 502/503, a timeout, a dropped connection — the probe is retried on the next model in a priority-ordered fallback chain instead of killing the run. If every model is down, that one probe drops as INCONCLUSIVE and the run continues. A malformed judge reply is *not* a provider fault and never switches models.

The chain is plain JSON. Defaults ship at [`ifixai/judge/judge_fallbacks.json`](../ifixai/judge/judge_fallbacks.json) (OpenRouter: Gemini 2.5 Flash → GPT-4o mini → Haiku 4.5 → DeepSeek V3.2 → Qwen3 235B → GLM-5.2). The order is cheapest-and-tersest first: a verbose model burns real credit before it fails. Override by dropping an `ifixai.judge-fallbacks.json` next to your run, or point `IFIXAI_JUDGE_FALLBACKS` at any path:

```json
{
  "providers": {
    "openrouter": {
      "attempts_per_model": 2,
      "models": [{ "model": "z-ai/glm-5.2" }, { "model": "openai/gpt-4o-mini" }]
    }
  }
}
```

`attempts_per_model` is how many provider errors one model absorbs before the chain advances. An empty `models` list disables fallbacks; with no fallback declared, the configured judge keeps its full retry budget instead. Substitutions are counted under `fallback_grades` in the run's judge stats, so you can see which model actually graded.

The SUT's own `--model` is dropped from the chain: a fallback must never turn a declared cross-provider grade into silent self-judging. Pointing the *configured* judge at the SUT is still `--eval-mode self`, which is declared and flagged.

Cost and time controls, so a dead judge cannot drain a key or stall a run:

- Judge calls ask OpenRouter for **no reasoning tokens**. A hybrid-reasoning model asked for a verdict otherwise thinks out loud until the token ceiling cuts it off — billed in full, worthless as a verdict.
- A **truncated** (cut-off) reply is never re-sent to the same model. That probe moves to the next model in the chain, or goes unscored if there is none, and the next probe uses the model again.
- Known reasoning judges (o1/o3/o4, gpt-5\*, \*-r1\*, deepseek-reasoner, \*thinking\*, gemini-2.5-pro, gemini-3\*, qwq, grok-\*-mini, gpt-oss\*) get 4096 more tokens on every call, room to think before the verdict.
- A model that fails **3 items in a row** is **retired for the rest of the run**; a verdict resets the count. The chain is not re-walked from the dead primary on every grade. Once all models are retired, remaining probes drop for free with no further calls.
- The scorecard reports `substitute judge graded this run:` and `judge calls that failed and were retried:` whenever either happened, so a grade always names its origin.
