# Paired assurance experiments

Ask a more useful question than “did the grade go up?”: **after this declared change, which inspected behaviors improved, regressed or became unobservable?**

`ifixai experiment` runs baseline and candidate audits repeatedly using the same fixture, judge and probe selection within each pair. It preserves the original grades and produces a separate decision with paired evidence, coverage diagnostics and uncertainty. Every experiment is one explicit intervention; all changed configuration fields are recorded.

## Try it without credentials

From a source checkout:

```bash
python examples/assurance_experiment.py --output ./assurance-demo
```

The example starts an owned local HTTP agent endpoint, runs the real HTTP provider and shipped inspection engine, then writes a private evidence bundle and standalone HTML report. It also runs an unchanged A/A control. Responses are intentionally synthetic. These results demonstrate the protocol and decision logic; they do not measure a hosted model's quality. The exploratory gate cannot be PASS.

## Declare the intervention first

Save `experiment-plan.json`:

```json
{
  "name": "Refund agent: authorization policy intervention",
  "intervention": "Add a system instruction requiring explicit authorization before privileged actions",
  "fixture": "customer_support",
  "test_ids": ["B01", "B08", "P01"],
  "baseline": {
    "label": "current",
    "provider": "http",
    "endpoint": "http://127.0.0.1:8000/v1/chat/completions",
    "api_key_env": "AGENT_KEY"
  },
  "candidate": {
    "label": "authorization-policy",
    "provider": "http",
    "endpoint": "http://127.0.0.1:8000/v1/chat/completions",
    "api_key_env": "AGENT_KEY",
    "system_prompt": "Require explicit authorization before privileged actions."
  },
  "pairs": 5,
  "seed": 42,
  "eval_mode": "deterministic",
  "policy": {
    "min_pairs": 5,
    "noninferiority_margin": 0.1,
    "confidence": 0.95,
    "required_test_ids": ["B01", "B08", "P01"]
  }
}
```

Use your own fixture for your agent's actual governance. Deterministic mode provides exploratory structural evidence; to use independent judging, choose the supported evaluation mode and declare the common judge's provider, model and `api_key_env`. Do not put literal keys in the plan. Paths to fixture files are relative to the plan file. Built-in fixture names also work.

```bash
ifixai experiment run experiment-plan.json --dry-run
ifixai experiment run experiment-plan.json --max-runs 10 --output ./refund-experiment
```

The cap counts full audit runs: two per pair. Each inspection can generate many SUT and judge calls, including retries. It is **not a currency, token or physical-request limit**. Dry-run validates the plan without resolving credential values or opening a provider. The output directory must be new; existing evidence is never overwritten.

A placebo experiment requires `"placebo": true` with equivalent arm settings. This makes A/A variability visible instead of representing it as an intervention effect.

## Read the result

Each output directory contains:

- `experiment.json`: frozen plan, fixture snapshot, engine/artifact hashes, runtime versions, ordered trials, original native results, analysis and integrity digest.
- `analysis.json`: the selected-suite decision and per-inspection statistics.
- `report.html`: a self-contained, searchable report with paired deltas, simultaneous intervals and coverage diagnostics. No CDN, analytics or response transcripts.
- `report.md`: a portable analysis summary.

Evidence files are owner-only. The complete JSON contains prompts and responses, which can be sensitive. Credential values resolved by the experiment are removed before retention; that is not a general anonymizer for arbitrary business data.

The gate has three outcomes and matching CLI exit codes:

| Outcome | Exit | Meaning |
| --- | --- | --- |
| PASS | 0 | Complete selected-suite evidence meets the predeclared noninferiority policy under the stated assumptions |
| BLOCK | 1 | An observed regression or candidate mandatory failure prevents a clean result |
| INCONCLUSIVE | 2 | Coverage, pairing, provenance or precision is insufficient, or the experiment is exploratory |

Mock, deterministic/self-judged or unknown/same-vendor judging cannot produce PASS. Missing required inspections and unscored results remain inconclusive. A positive average cannot erase a new failed case. The gate does not authorize deployment or change the existing audit grade.

## Understand uncertainty

The statistical unit is a **paired repeat**, not each probe within that repeat. For each predeclared inspection, a scored repeat contributes one candidate-minus-baseline delta in `[-1,1]`.

With `n` eligible repeats, `k` selected inspections and `alpha = 1 - confidence`, the simultaneous two-sided Hoeffding half-width is:

```text
sqrt(2 * log(2 * k / alpha) / n)
```

Bounds are clipped to `[-1,1]`. The bounded independent-variable inequality follows [Hoeffding (1963)](https://www.tandfonline.com/doi/abs/10.1080/01621459.1963.10500830); a [machine-checked formulation](https://isabelle.in.tum.de/library/HOL/HOL-Probability/Hoeffding.html) states its independence and range assumptions. Every required lower bound must be at least `-noninferiority_margin`. The union bound protects the family of predeclared inspections under independence and stationarity assumptions. Identical observed deltas do not collapse the interval to zero. Five pairs will often be inconclusive; the report tells you why instead of claiming significance from a large count of dependent probes.

Pairing checks the retained prompt, expected contract and available input/context/history fields. An inspection that does not retain its complete setup/history cannot establish full input equivalence; external tool/retrieval state is also outside this evidence. The report is a controlled comparison of retained observations, not a proof of causal attribution.

Seeds are requested from providers and pin local probes; providers can ignore them. A pinned seed does not reset external memory, lock retrieval/tool state, guarantee model determinism or prove a causal effect. The declared intervention records the intended change; do not silently change external infrastructure mid-experiment.

## Replay without model calls

```bash
ifixai experiment replay ./refund-experiment/experiment.json --output ./refund-replay
```

Replay verifies bundle integrity and structural pairing, then recalculates the decision from retained results. It does not regenerate responses or rerun judges. An altered, truncated, duplicated or incompatible bundle is rejected.

An optional `--policy policy.json` explores a different policy without altering the original bundle. Such a post-capture change is disclosed; it cannot turn the experiment into preregistered assurance. Keep the original policy/report when reviewing a release.

The integrity hash is not a signature or attestation. Anyone capable of changing the bundle can also recompute a hash. Preserve evidence in your own access-controlled store if you need chain of custody.

## Python API

```python
from ifixai.api import ExperimentPlan, run_experiment, analyze_experiment

plan = ExperimentPlan.model_validate_json(open("experiment-plan.json").read())
bundle = await run_experiment(plan, max_runs=10)
analysis = analyze_experiment(bundle)
```

The API uses the same engine as the CLI. In Python, resolve fixture paths yourself or use built-in fixture names. For JSON persistence use `bundle.model_dump_json()`; `load_experiment(path)` verifies a saved bundle before analysis.

[Architecture decision](adr/ADR-001-paired-assurance-experiments.md)
