# ADR-002: Audit retained agent trajectories against explicit business policy

Status: proposed for this feature PR.

## Context and problem

Individual inspection outcomes cannot reconstruct authority across a production
agent sequence. A worker may reuse an approval for changed arguments, receive an
overbroad child delegation, or exhaust a shared budget through multiple actors.
These failures depend on recorded relationships across steps. A generic telemetry
span name does not identify an approver, bound arguments or delegated authority.
The repository needs a reproducible way to inspect those relationships without
executing imported actions or making a model-based governance decision.

## Decision

Add an independent `ifixai.trajectory` package with strict, bounded, declarative
event and policy models, an iterative dependency graph evaluator, a supplied-callable
recorder and passive JSONL / explicitly instrumented OTLP JSON importers. Retain
one tenant and trace per capture. Expose audit, replay and alternate-policy analysis
through the SDK and a dedicated CLI group. Existing inspection registrations,
weights, grades, providers and schemas remain unchanged.

Evaluate six families: graph coverage, closed-world permissions, exact approval
binding/use/expiry/revocation, attenuating delegation chains, integer shared budgets
and recorded citation lineage. Every finding retains event witnesses. Preserve
missing evidence as INCONCLUSIVE rather than fabricating authority. A malformed
graph prevents authority evaluation. BLOCK takes precedence when evaluated facts
contain violations; CLEAR requires checked actions and complete enabled coverage.

Use literal exact/prefix resource scopes, integer units and canonical JSON digests.
Do not infer permission from timestamps, actor labels or arbitrary OTLP span names.
Require each OTLP semantic span to contain the canonical event and bind it to the
span ID, trace ID and timestamp. Retain dropped/inconsistent instrumentation as
coverage uncertainty. Collector identity and completeness remain declarations.

Freeze policy, capture and report together in a checksummed portable bundle.
Replay validates the normalized shape and checksum, then recomputes the report.
Alternate-policy analysis reads the same capture and records a finding-set delta;
it does not execute an intervention. Export owner-only HTML, JSON and Markdown
with no remote resources, raw arguments or model response text. Refuse overwrite.

## Alternatives considered

1. Add a synthetic inspection: smaller integration, but it would test supplied
   scenarios rather than audit a retained application trace. It cannot answer
   which actual recorded steps consumed an approval or a shared budget.
2. Interpret ordinary GenAI spans automatically: convenient, but missing business
   semantics would force guesses about authority and completeness. Reject this.
3. Add an inline enforcement gateway: can prevent selected actions, but requires
   application authorization integration and runtime availability guarantees.
   This passive feature has no such authority and cannot claim prevention.
4. Ask an LLM to judge trajectory authorization: flexible interpretation, but
   nondeterministic, potentially costly and unsuitable for exact grant accounting.
   Explicit policy is reproducible and has no provider dependence.
5. Sign bundles: useful for later collector provenance, but a signature would
   not prove completeness or factual truth. Leave authenticated collection to
   a separate integration with an explicit trust and key-management design.

## Consequences, limitations and rollback

Applications must emit the explicit instrumentation contract and choose policies.
The recorder observes synchronous supplied callables; arguments, actors, human
identity and costs are supplied semantic declarations. Failed calls make coverage
incomplete because side effects may precede an exception. Citation checks establish
observed lineage only. Hashes detect mutation, not authenticity. Reports describe
retained evidence, not unobserved activity or production certification.

The graph has bounded input and lazy ancestor queries, avoiding a full transitive
closure. Delegation depth is capped at 128; exceeding the cap is INCONCLUSIVE rather than truncated positive assurance. Cumulative ancestor traversal is capped at 2,000,000 steps and exhaustion halts authority evaluation with a coverage gap. Many repeated ancestor queries can still
cost more than flat traces; benchmarks report actual local time/memory and do not
claim network or production latency. Schema version 1 is explicit; changed report
semantics may require a compatible evaluator for old replays.

The change adds no runtime dependency, network operation, inspection grade change
or automatic publication. Roll back by removing the package, CLI registration,
public SDK exports and docs together. Previously retained bundles remain local
evidence and should not be deleted automatically.

## Acceptance and evidence

Require independent hand-derived policy oracles, adversarial graph/grant/citation
tests, recorder side-effect/failure checks, strict JSONL/OTLP parsing, bundle tamper
and rehash tests, immutable what-if workflow, HTML escaping, and no network/provider
resolution. Run synthetic local callables through native CLI audit and replay,
measure 1,000/10,000-event traces, and verify the exact feature patch and built
wheel independently. Run the repository's required lint/security/layout/fixture
gates and compare advisory typing diagnostics with unchanged main. Before public
submission, require non-implementer review of the exact candidate revision.

The feature is independent of the separately proposed paired-experiment feature;
neither unmerged PR is a runtime prerequisite. Number 002 preserves distinct
decision identifiers if both proposals are accepted.
