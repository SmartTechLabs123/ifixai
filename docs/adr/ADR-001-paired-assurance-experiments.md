# ADR-001: Paired assurance experiments

- **Status**: proposed
- **Date**: 2026-10-11
- **Deciders**:
- **Tags**: experiments, reproducibility, evidence

## Context

The existing `compare` command compares two completed scorecards. It cannot control the fixture, random probe choices, judge, run order or repeat count that produced them. A higher aggregate score can hide lost coverage or a new failure. Teams need a repeatable way to investigate a declared agent change and retain the evidence behind that decision.

## Decision

Add an independent experiment domain with strict plan, trial, bundle and analysis models. Execute paired baseline/candidate audits through the existing public API, serially, with fresh provider instances. Freeze the common fixture, inspection artifacts and engine fingerprint before the first run. Pin every analytic family seed, holdout ID and run nonce within each pair, vary seeds between repeats and balance AB/BA order.

Retain full native scorecards. Analyze only scored, comparable evidence. Match case/step/method identities and prompt/expected contracts; expose changed trajectories instead of treating them as the same probe. Count repeats as independent sampling units, not individual probes. Use simultaneous bounded Hoeffding intervals with a declared noninferiority margin. The decision is separate from the existing grade. Observed regressions and mandatory failures block; missing evidence, inadequate precision or exploratory judging remain inconclusive.

Save an owner-only bundle with a content digest and self-contained reports. Offline replay validates and recomputes from retained results without resolving providers. A digest detects accidental alteration relative to the saved bundle; it is neither a signature nor proof of the provider's behavior. Post-capture policy changes are disclosed and cannot create preregistered assurance.

## Consequences

### Positive

- A proposed intervention becomes a reproducible experiment with evidence and explicit uncertainty.
- Coverage loss and regressions remain visible; target gains cannot cancel guardrail failures.
- Existing inspection implementations, scorecards and grade mathematics are reused unchanged.
- No new dependency, hosted service or remote report asset is required.

### Negative

- Two audits per pair can be expensive. The explicit run cap is not a token or currency cap.
- Conservative intervals commonly remain inconclusive at small repeat counts.
- Full private bundles can contain sensitive prompts and responses. Treat them as audit data.
- Fresh clients and balanced order reduce some confounding but cannot reset an external agent or prove stationarity, independence, deterministic sampling or causality.

### Neutral

- A PASS concerns the selected-suite policy, not certification or authorization to deploy.
- Serial execution avoids overlapping the current mutable inspection registry; this does not implement the separate registry-isolation proposal.

## Links

- [User guide](../experiments.md)
- [Existing comparison](../../ifixai/reporting/comparison.py)
