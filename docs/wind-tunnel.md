# Evaluator wind tunnel

iFixAi makes a claim about an agent. The wind tunnel measures whether that claim
holds: it injects one precisely scoped governance defect into a provider that is
correct everywhere else, runs the inspections clean and then defective, and
reports per inspection whether the defect was caught.

Without it, a change to an inspection cannot be called an improvement; it can
only be called a change. This is a separate offline measurement surface. It does
not alter inspection grades, and it never contacts a provider: no model call, no
credential, no network on any path.

## Run it

From a repository checkout with iFixAi installed. No API key is needed.

```sh
ifixai windtunnel controls
ifixai windtunnel run over_permissive --fixture customer_support
ifixai windtunnel sweep --fixture customer_support
ifixai windtunnel sensitivity --fixture customer_support
```

Start with `controls`. It runs the clean profile against every bundled fixture
and reports which ones the reference provider actually satisfies. A fixture
whose reference provider already fails an inspection cannot support a
measurement, and which fixtures those are is not apparent from reading them.

| Exit | Decision | Meaning |
| --- | --- | --- |
| 0 | Detected | Every expected detector caught its defect. |
| 1 | Blind spot | An expected detector did not move when its defect was present. |
| 2 | Inconclusive | The clean control did not pass, so no detection claim from this run means anything. |

Exit 2 outranks exit 1 deliberately. An unusable control does not make a blind
spot confirmed; it makes it unmeasurable.

## Sealed evidence, and failing a build on a lost detection

A measurement printed to a terminal cannot be cited or compared. `--output`
writes the run as a bundle holding its complete inputs and outcomes, sealed
with a canonical SHA-256 digest, owner-only, refusing to overwrite:

```sh
ifixai windtunnel sweep --fixture customer_support --output evidence/base.json
```

The digest covers inputs and outcomes and carries **no timestamp**, so the same
profiles against the same fixture and the same registry seal to the *same*
digest. That makes the digest a content identity rather than a receipt, which is
what lets a later run be compared to a recorded one:

```sh
ifixai windtunnel sweep --fixture customer_support --baseline evidence/base.json
```

An inspection that detected its defect in the baseline and does not now is a
**regression**, and exits 1 even when the run has no blind spot of its own —
the suite used to catch something and no longer does, which is the one result a
build should never go green on. A detection that appears is reported as an
improvement and does not fail. Losing the ability to score at all
(`detected` → `unusable`) counts as a regression: an inspection that can no
longer produce a number has stopped detecting, whatever the reason.

The comparison refuses outright across fixtures and across registry sizes. A
detection rate spanning either is not a rate, and a comparison that silently
spanned one would be worse than no comparison.

## How a measurement is produced

The comparison is paired. The same inspections run twice against the same
fixture and the same reference provider, and the only thing that differs is the
defect.

`DefectInjectingProvider` subclasses the shipped `MockGovernanceProvider`, so
every governance hook starts out correct and scored against the fixture's own
governance block. Each override calls `super()` and then corrupts the result
only where the active profile says to. Reimplementing a hook to be wrong would
measure the reimplementation; corrupting the correct answer measures the defect.
It also means the clean profile *is* the reference provider, so a false-positive
rate is a property of the suite rather than of the test rig.

Each injector takes its own deep copy of the governance fixture. The fixture
carries a mutable audit log that the hooks append to as the suite runs, so a
shared one lets the clean run's recorded actions appear in the defective run's
trail — contaminating the only comparison made here.

Firing is keyed on the call's identity rather than a counter, so an inspection
that fans out concurrently measures the same defect as a serial one, and a
result is reproducible from `(profile, fixture, inspection ids)`.

## Outcomes

| Outcome | Meaning |
| --- | --- |
| `detected` | Passed clean, failed defective. The inspection tests for this defect. |
| `missed` | Passed both. A blind spot, and the most valuable finding here. |
| `false_positive` | Failed the clean run too, so a later failure proves nothing. |
| `unusable` | Produced no usable score: inconclusive, no evidence, or an error. |

`unusable` is deliberately not folded into `missed`. An inspection the reference
provider gives no surface to has not been measured at all, and counting it
either way would misstate coverage. A defect that makes an inspection
unscoreable rather than failing it is also not a detection: a report cannot cite
a score it does not have, and a reader told "inconclusive" has learned nothing
about the agent.

## The defect catalogue

Each profile at rate 1.0 establishes a detection floor: an inspection that
misses a defect present on every eligible call does not test for it.

| Profile | What the governance layer does wrong |
| --- | --- |
| `clean` | Nothing. The known-good control. |
| `over_permissive` | Authorizes role/tool pairs the fixture forbids. |
| `over_refusing` | Denies role/tool pairs the fixture permits. |
| `unreasoned_denial` | Denies correctly but cites no policy rule. |
| `fail_open_unknown` | Authorizes tool identifiers that exist in no fixture. |
| `audit_gap` | Omits the audit entry for the action just taken. |
| `stale_config_version` | Reports a configuration version that is not in force. |
| `overconfident` | Reports certainty regardless of basis, and stops abstaining. |
| `destruction_leak` | Accepts a destructive operation the fixture requires refusing. |

A profile changes one surface. One that broke authorization and the audit trail
together could not attribute a detection to either; compose them explicitly when
a combination is the thing under study.

`destruction_leak` must name the tools it leaks on. Which tools are destructive
is declared by the fixture under audit, not inferred from a tool id, so it is
not reachable from a profile name alone and `sweep` omits it.

Each profile also declares the inspections expected to catch it, drawn from each
inspection's documented contract rather than from an observed run. A listed
inspection that does not move is a gap in the suite; an unlisted one that does
is either welcome or a sign the defect is broader than its name admits. Both are
reported.

## Partial leak rates

A rate is the fraction of *eligible* calls corrupted — calls the defect could
apply to, not all calls. `over_refusing` at 0.5 flips half the permitted pairs
and leaves every forbidden pair correctly denied, so the resulting score stays
interpretable.

The mandatory minimums (B01 at 100%, B08 at 95%, P01 at 100%) exist to make a
single bad permission cell fatal, so the question worth answering is not whether
a wholly broken layer is caught but how small a leak still trips the gate.
`ifixai windtunnel sensitivity` traces that curve.

## What it doesn't do

- A detection rate measured here is against *these* seeded defects on *this*
  fixture. It is a lower bound on what the suite catches, never a statement
  about a real agent.
- The defects are declared, not discovered. An adversary chooses its own attack;
  these do not, so a clean sweep is not evidence of resistance to one.
- It measures the inspections, not the judges. The default selection covers the
  inspections whose structural path the reference provider implements, because a
  judge introduces variance that would otherwise be charged to the defect.
  Judge-path inspections are measured only when a profile names them and a judge
  is configured.
- An inspection reported `unusable` on every fixture is not being measured by
  this tool at all. Treat that as a coverage gap to close, not a result.
- `detected` means the score crossed the inspection's threshold. It does not
  show that the inspection reported a *useful* reason, only that it moved.
- Nothing here validates the fixture. A fixture that describes governance the
  deployment does not have will produce a confident measurement of a fiction.
- A digest proves content, not provenance. Anyone who edits a bundle can
  recompute its digest and the result will load; what they cannot do is keep
  the original digest, so a baseline comparison reports a changed measurement
  rather than accepting a substituted one. It is not a signature, and it says
  nothing about who ran the measurement or whether they ran it honestly.
- A baseline only guards what it recorded. Profiles or inspections absent from
  it are reported as not compared, never as unchanged.

## Validate changes

```sh
python -m pytest ifixai/tests/windtunnel -q
```

The suite pins the injector's blast radius — rate semantics, seed keying,
per-tool scoping, and that an authorization defect leaves the configuration
version and audit trail alone — the CLI's exit-code contract, and the evidence
properties: digest stability across repeated runs, refusal of an edited bundle,
and refusal to compare across fixtures or registry sizes.

Detection rates are deliberately not asserted. They are expected to change as
the inspections improve, and a test that froze them would make an improvement
look like a regression. A recorded baseline is the right place to notice a
change, because it reports the direction.
