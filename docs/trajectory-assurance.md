# Recorded trajectory assurance

iFixAi can audit a retained sequence of agent actions against a business policy,
then freeze and replay the findings without a model or service connection. Use it
to investigate an approval used twice, a worker acting outside delegated scope,
a shared budget exhausted across actors, or an output citing the wrong source.
Each finding links to the recorded events that support it. This is a separate
workflow from inspections and scorecards; it does not change existing grades.

The auditor is **passive**. It evaluates already-recorded actions and never
executes a tool named in imported data. `TraceRecorder.invoke` executes only the
Python callable supplied by your application, records its observed outcome and
propagates its exceptions. It does not enforce policy or reverse side effects.

## Observe an application action

```python
from ifixai.trajectory import (
    ActorPolicy, AuditPolicy, BudgetRule, PermissionRule, TraceRecorder,
    create_audit_bundle, write_capture_jsonl,
)
from ifixai.reporting.trajectory import write_trajectory_reports

store = {"refunded_units": 0}
def refund(amount):
    store["refunded_units"] += amount
    return store["refunded_units"]

policy = AuditPolicy(
    name="local refund policy", tenant_id="shop",
    trusted_collectors=("local-recorder",),
    actors={"worker": ActorPolicy(roles=("operator",))},
    permissions=(PermissionRule(role="operator", action="refund",
                                resource_scope="orders:*"),),
    budgets=(BudgetRule(scope="trace", limit_units=100),),
    require_citations=False,
)
recorder = TraceRecorder("shop", "local-recorder")
recorder.invoke(refund, 25, actor_id="worker", action="refund",
                resource="orders:42", arguments={"amount": 25}, cost_units=25)
capture = recorder.seal()
write_capture_jsonl(capture, "capture.jsonl")  # native captures only; new file
bundle = create_audit_bundle(capture, policy)
assert bundle.report.decision == "CLEAR"
write_trajectory_reports(bundle, "refund-evidence")  # must be a new directory
```

This example mutates a local dictionary, not money. The application supplies the
semantic arguments and integer cost; the recorder does not prove that they match
the callable or billing. Export to JSONL accepts native captures without import warnings; it cannot erase incomplete imported coverage. Raw argument dictionaries and response text are not
serialized. Treat actor identifiers, resource names and labels as retained data.
The recorder serializes event emission under a lock, refuses sealing while a
call is pending, and marks a recording incomplete after a failed call. Automatic
parent links represent local program order, not distributed physical causality.
Synchronous callables are supported; instrument asynchronous execution explicitly.

The larger [runnable example](../examples/trajectory_assurance.py) constructs
compliant and adversarial local refund trajectories with approvals, delegations,
sources and outputs, then exercises canonical JSONL and instrumented OTLP.

## CLI and portable evidence

Save `policy.model_dump_json(indent=2)` as `policy.json`. All paths below are local.

```bash
ifixai trajectory audit policy.json capture.jsonl --output evidence
ifixai trajectory replay evidence/bundle.json --output replay-evidence
ifixai trajectory audit policy.json spans.json --format otlp --trace-id <32-hex-trace-id> --output otlp-evidence
ifixai trajectory what-if evidence/bundle.json alternate-policy.json --output scenario-evidence
```

Every command creates a new owner-only directory with `bundle.json`, `report.json`,
`report.html` and `report.md`. Existing destinations are refused. HTML uses no
remote resources and includes a searchable timeline, recorded parent links,
violation witnesses, family coverage and budget usage. Open `report.html` locally.
`what-if` also writes `comparison.json`, identifying resolved, new and persistent
findings by `(rule, code, event_id)`. It evaluates the same retained facts under
another policy; it does not execute a remediation or demonstrate its effectiveness.
The original bundle is read only.

| Exit | Decision | Meaning |
| --- | --- | --- |
| 0 | CLEAR | No observed violation or coverage gap in the evaluated policy and retained capture |
| 1 | BLOCK | At least one observed policy violation; evidence gaps remain visible |
| 2 | INCONCLUSIVE | No observed violation, but missing evidence or unevaluated required coverage prevents a conclusion |

Malformed input, replay integrity failure and output-directory errors are command
errors (exit 1) without a report decision. CLI errors do not echo raw imported
payloads. A trace with no checked completed action cannot be CLEAR. A configured
approval family without an applicable completed action, required citations with
no output, or configured budgets without completed actions remain inconclusive.
Disabled or unused families are shown as `NOT_EVALUATED`; this is not a pass.

Replay input is bounded to 256 MiB. The bundle stores the complete normalized policy, capture, deterministic report
and SHA-256 checksum. Replay validates the checksum and recomputes the report;
editing a report and rehashing it does not bypass recomputation. A checksum proves
neither collector authenticity nor capture truth. Replays require a compatible
schema/evaluator; a changed evaluation is refused rather than silently accepted.

## Policy and evidence semantics

| Family | Evaluated invariant |
| --- | --- |
| Graph | Unique IDs; contiguous sequence; same tenant/trace; monotonic time; preceding parents; start/end, count and connected lineage |
| Permission | Completed actions require a known actor and a direct role grant or valid delegated authority |
| Approval | Every matching rule requires the bound subject, action, exact resource and argument digest; authorized approver, causal precedence, expiry, revocation and shared use count |
| Delegation | Known issuer/recipient; issuer permission; parent recipient owns child issuance; action and scope attenuation; causal lineage; every ancestor remains unexpired and unrevoked |
| Budget | Charge every completed observed action once, even unauthorized actions; exact integer trace and actor pools |
| Citation | Output source IDs, digests, known source actors and preceding causal lineage |

Resources on actions and approvals are exact. Policy/delegation scopes permit an
exact resource or one terminal `*` prefix wildcard (`orders:*`). Matching is literal,
not regex or shell execution. Multiple matching approval rules all apply: an issuer
must satisfy each rule's approver roles and human requirements. Expiry equality is
expired. Completed attempts consume a referenced approval use even if the binding
is invalid. Invalid recorded revocation blocks the report and conservatively denies
subsequent use; it never confers authority.

Delegation chains deeper than 128 grants are coverage gaps and cannot produce CLEAR. The cumulative ancestry traversal limit is 2,000,000 steps; exhaustion halts authority evaluation with a coverage gap while retaining the completed-action budget ledger. Failed or unknown actions are coverage gaps because a failure can follow a side
effect. Their unknown costs cannot be assumed zero. Missing referenced grants and
sources are missing evidence; the auditor does not invent their content. An invalid
graph prevents positive authority evaluation. Observed violations take precedence
over non-graph evidence gaps. Tenant mismatch prevents policy evaluation.

Citation lineage does **not** establish semantic support, truth or licensing.
Human identity, collector trust, completion and cost fields are caller declarations.
Collect and secure them in your application; this tool is not a signed collector,
runtime authorization service or deployment certificate.

## Canonical JSONL schema

The first nonblank line is a strict capture header; each remaining nonblank line
is a `TraceEvent`. Unknown fields, duplicate JSON keys, nonfinite constants and
invalid UTF-8 are rejected. Input is bounded to 64 MiB and 100,000 events. Canonical
rows are never silently sorted. Event IDs are bounded identifiers; trace IDs are
32 hex characters. Timestamps and amounts are nonnegative strict integers bounded
to signed 64-bit range, rejecting booleans and floats. Exact models and defaults
are exposed in `ifixai.trajectory.models`.

```json
{"schema_version":"1","trace_id":"1234567890abcdef1234567890abcdef","tenant_id":"shop","collector_id":"local-recorder","capture_complete":true,"source_format":"jsonl"}
```

Every event has `event_id`, `trace_id`, `tenant_id`, `sequence`, `time_ns`, `parents`
and a `payload` discriminated by `kind`. Supported kinds are `start`, `approval`,
`revocation`, `delegation`, `action`, `source`, `output` and `end`. Seal with an `end`
whose `declared_events` includes itself and whose `complete` declares coverage.
Use `canonical_digest` / `arguments_digest` for normalized JSON SHA-256 digests.

## Explicit OTLP JSON instrumentation

The importer accepts an OTLP `ExportTraceServiceRequest` JSON envelope with
`resourceSpans[].scopeSpans[].spans[]`. Ordinary GenAI span names do not encode
the business-policy authority needed for this audit. **Every retained semantic
span must carry an `ifixai.event` string attribute containing its full canonical
TraceEvent JSON.** A normal uninstrumented export is incomplete evidence.

Resource attributes are `ifixai.tenant_id` and `ifixai.collector_id` as `stringValue`,
and `ifixai.capture_complete` as `boolValue`. Bind the embedded event's ID to the
span's nonzero 16-hex `spanId`, its trace to nonzero 32-hex `traceId`, and its time
to `endTimeUnixNano` (or `startTimeUnixNano` if end is absent). IDs accept hex case
and normalize lowercase. Timestamps accept decimal strings or exact integers.
If there are explicit parents, `parentSpanId` must identify a retained canonical
parent; additional parents remain in `event.parents`.

```json
{"key":"ifixai.event","value":{"stringValue":"{...full canonical event JSON...}"}}
```

Unknown ordinary OTLP envelope fields are ignored. Unknown reserved `ifixai.*`
attributes, missing/inconsistent resource metadata, uninstrumented spans, missing
canonical parent bindings, or reported dropped attributes/events/links create
coverage warnings. Duplicate reserved attributes and ambiguous typed values are
rejected. Span kind/status enums, when supplied, must use numeric values; missing start/end timestamp metadata is a coverage warning. Multi-trace exports require explicit trace selection; unselected traces
are intentionally excluded. OTLP events are normalized by their explicit sequence,
then the same graph audit checks duplicates, gaps and time. Timestamps alone never
prove approval or delegation authority.

The envelope follows the official [OTLP JSON encoding specification](https://opentelemetry.io/docs/specs/otlp/#otlpjson-protobuf-encoding).
The `ifixai.*` instrumentation contract is specific to this feature and is not
an OpenTelemetry semantic convention or an automatically installed integration.

## Verification

Run `python -m pytest ifixai/tests/trajectory -q` for independent policy oracles,
strict import/replay, adversarial grants, recorder outcomes and offline workflows.
The example and benchmark use synthetic local facts, not production deployment
evidence. Full repository checks remain `ruff check ifixai`, `bandit -r ifixai -ll`,
`ifixai validate` and validation of every bundled example fixture. No API key,
provider call or new runtime dependency is required by this workflow.
