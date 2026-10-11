# ADR-003: Explicit business-policy reachability and goal-preserving repair

Status: Proposed

## Context

Individual role/tool authorization tests and scripted conversations can miss harmful compositions of individually allowed business actions. Recorded trajectories only cover paths already observed. An auditor needs a prospective check that can expose a short forbidden path before rollout and assess whether a proposed authorization change destroys useful business completion.

Tool descriptions and fixture mock responses cannot establish transition semantics. Arbitrary automatic execution of imported actions would introduce authority and side effects unrelated to inspecting a model.

## Decision

Add a separate offline Policy Lab with explicit finite Boolean state, exact actors and permission rules, simultaneous guarded effects, named forbidden conjunctions and existential completion goals. Preserve every configured budget counter in visited-state identity. Deterministic breadth-first search retains one shortest witness per objective using parent pointers. Explore the complete reachable frontier unless bounded depth, state or examination limits require an inconclusive result. Counterexample evidence remains valid even when the frontier is incomplete; safe certification requires complete coverage and useful enabled transitions. Completion goals prevent a deny-all policy from becoming an accepted repair.

Permission repair enumerates one- or two-rule removals with shared candidate and examination budgets. Accept only globally verified candidates and qualify minimum cardinality when any smaller candidate was inconclusive. Preserve the original facts, guards, effects, goals and budgets. Produce a candidate artifact; never mutate deployed policy.

Use canonical typed JSON bundles with strict parse, digest verification and full recomputation on replay. Checksums establish consistency rather than truth or authentication. Project existing fixture authorization only when exact IDs and role matrices agree, explicitly recording its absent scope/effect semantics.

Keep actual witness execution a separate Python API requiring caller-supplied synchronous bindings, reset and full independent observations. Validate evidence and binding declarations before any execution. Compare initial, pre-action and post-action snapshots; stop on mismatch or exception. Imported JSON never dispatches Python names or providers. No hidden threads, asynchronous continuation or unsupported timeouts.

Expose check, replay, compare, repair and fixture projection through an additive CLI group, public Python exports, private evidence files and a self-contained report. Add no runtime dependency or inspection-grade coupling.

## Alternatives considered

- More scripted systemic-harm prompts: useful behavioral coverage but no exhaustive model reachability or shortest-path guarantee.
- A passive trajectory checker: verifies declared observations but cannot discover an unobserved composition.
- Infer action effects from prose using a model: would mistake hypotheses for business ground truth and add non-determinism and billing.
- An SMT dependency or generalized planning language: broader expressiveness but heavier integration and semantic surface than the bounded Boolean/counter contract requires.
- Repair by blocking every tool: eliminates some unsafe paths but fails declared business completion and is explicitly rejected.

## Consequences and boundaries

Model authors must declare sufficient state. Model verification is relative to those declarations, not proof of deployment safety, fairness, universal liveness or cross-tenant authorization. Finite exploration is exponential in facts and counters; hard input/search/batch bounds and honest inconclusive decisions replace unsupported scalability claims. Native observations provide evidence for one explicitly exercised local witness and can miss unmodeled side effects. Caller-supplied callables retain their normal authority.

Frozen models use detached revalidation to avoid mutable nested containers and Pydantic copy/coercion bypasses. Exact counter identity can increase exploration but is required for correct budget reachability. Permission removal minimality is cardinality within the declared search space, not global business-policy optimality.

## Acceptance and rollout

Use a separately implemented exhaustive primitive oracle, adversarial evaluator tests, native match/mismatch/failure tests, fixture and CLI integration, private escaped reports, existing regressions, repository lint/security/layout checks, same-revision wheel and exact patch-alone verification. Demonstrate real synchronous local refund-store operations: a three-action harmful composition and a repaired independent-approval path, both independently observed. Preserve evidence and obtain an independent non-implementer review of the exact commit before publication. Keep existing inspection scores unchanged and retain manual maintainer merge authority.
