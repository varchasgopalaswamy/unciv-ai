# Gameplay policy and Code Mode

The application uses the independent Main, Economy and Military role flow.
Main handles player choices and delegates role work; Economy and Military retain
their ordinary action permissions and ownership checks. The council runtime,
executive grants, adviser tools/reports and council policy options have been
removed. Historical result artifacts and frozen experimental sources remain as
read-only evidence; they cannot select an active council policy.

`agents.gameplay_policy` is frozen with sessions, role requests and controller
attempts. Missing settings retain legacy role behavior. New experiments select
the shared outcome and opportunity assessment mechanisms explicitly:

```toml
[agents.gameplay_policy]
version = 1
architecture = "repaired_roles"
```

No active game or saved request is migrated. Native legality and player visibility
remain authoritative; social promises are advisory, and no fixed build order or
compulsory movement is added. Religion, espionage and mods remain outside this work.

## Observation delivery

`PreparedRoleContext` serializes role and communication startup, including the
bounded details prepared by `startup_context`. The gateway derives inspection
credit from that exact saved input, rather than all observations it cached during
preparation. The run-scoped `PlayerObservations` capability materializes full,
delta, unchanged and compatible cross-query detail responses with the existing
reducers. It also materializes overview and coordination updates. Reducer state
survives within-run history compaction but is never reused by another run.

Prepared delivery credit is frozen privately with the startup artifact. The local
controller clears presentation credit before a new SDK run. A lifecycle hook
acknowledges it after a model response only when the exact opening survived the
preceding context transformations. An opening removed or summarized by compaction
grants no inspection credit; the agent must inspect current facts. The private credit record alone never grants action authority.

A missing baseline admits at most four read-only recoveries per run. Original
responses and recovery receipts remain private in gateway journals and SDK tool
metadata. Recovery never replays an action. An unavailable observation following
a committed action preserves the receipt and requires reconsideration.

`invalidatedDetails` revokes inspection evidence while retaining the private
historical bytes needed to decode subsequent native deltas. Retention alone
does not deliver facts or restore authority. Only a new native response can
materialize that query; the gateway independently checks current evidence.

## Construction and outcomes

`construction_intent` offers `keep_current`, `append_new`, `start_new`,
`prioritize_existing` and `remove_existing`. Existing entries use city-bound queue
references containing the native queue revision hash, index and name. Duplicate
Workers therefore remain distinct. Argument validation rejects references absent
from this run's current delivered queue; native revision checks fence subsequent
external changes.

`QUEUE_CONSTRUCTION` retains append behavior. `START_CONSTRUCTION` calls the
engine's atomic validated `tryStartConstruction`, which reuses the human city
screen's add-to-top behavior. Production is preserved according to native rules.
Native receipts include the before and after queues. Compact feedback describes
unmet intent; it never undoes or repeats an effect.

Version 1 terminal tools accept optional structured `effectClaims`. Each claimed
operation and command kind must match a confirmed receipt from that run. Free
prose still needs human review; the application does not infer truth from prose.
Harness `SystemReminders` and `DetectLoops(action='nudge')` give advisory feedback.
Cross-turn inactivity assessment uses the existing progress history, not loop
detector memory from another run.

## Code Mode execution

Code Mode is an execution choice within the same role architecture. Set
`agents.execution_policy.code_mode.enabled = true` in a new experimental session.
The Harness exposes permitted function tools through `run_code`; the model writes
Python executed by Monty. Terminal role/report tools remain native SDK outputs.
Code has no filesystem, environment or network access except the explicitly
supplied, owner-bound tools. Native effects still pass through the original role,
visibility, epoch, inspection and receipt checks.

Observations are materialized before tools return data to Monty. The private
failure fence prevents caught sandbox exceptions from bypassing retired authority
or uncertain native receipts. Full nested call results remain in SDK metadata and
receipts; committed effects are included in bounded feedback even if a snippet
fails later or prints nothing. Errors require reconsideration, not automatic
replay. Ordinary deferred economy and peace waits keep their original identities
and release inference capacity while awaiting work.

The initial Code Mode investigation changes only the Code Mode flag from the
retained direct-execution settings. Output limits, caching and compaction start
with the same values as the baseline. Tool traces guide any later revisions;
policy and image hashes distinguish them. The credential-free scripted fixture
also uses the actual generated Code Mode catalogue and Monty execution, rather
than bypassing the sandbox.

Code Mode advertises pure result helpers alongside its generated catalogue. `result_data`
selects a native envelope by default, accepts an explicit frozen-review envelope,
and refuses ambiguous, rejected or partially executed native results. `result_feedback` preserves the
complete envelope; `required_result_field` distinguishes missing fields from
legitimate empty values. `result_entity` selects the documented `data.entity`
object of an entity-detail response. Their versioned implementation runs inside
each Monty REPL and is reinstalled after a reset. It is shared with host wrappers
for parity checks; helper execution creates no SDK tool calls. These helpers
perform no native reads or writes, receive no native operation link or inspection
credit, and bypass observation recovery.
Use their advertised keyword arguments. Convenience tools such as
`inspect_bundle` already return their own flattened result structure.

The generated catalogue preserves argument names, required fields and typed
bundle queries/results. Presentation adapters flatten conditional object fields
that the pinned renderer would omit, including `construction_details(target=...)`.
Python keyword arguments such as the native `with` field use an explicit
`arguments={...}` dictionary. Conditional constraints still use the original
canonical validator at dispatch; rendering does not relax native inputs.

Frozen-review query arguments are validated before any bundle projection.
Malformed caller input becomes retry feedback; internal data, evidence and store
validation failures remain fatal. Code Mode guidance groups independently needed
reads, retains source labels, reuses current data and checks receipts before
dependent actions. It imposes no minimum call count or strategic trajectory.

## Short execution windows and intent handoffs

The version-two candidate policy enables optional `run_code(focused=true, ...)`
for live player roles. This remains full Code Mode: native observations and
actions go through Monty, with structured terminal tools outside the sandbox.
There is no hybrid execution mode. Direct execution remains the default.

A focused program enters a FIFO player-scoped scheduling gate at its first
gateway operation. Its default limits are ten seconds and six underlying gateway
operations, with a thirty-second queue timeout. Bundle reads and batch commands
count individually; pure local helpers do not count. Other role operations use
the same gate. The window ends after a native batch has been acknowledged, on
expiry, before a deferred wait, or when the program exits. No database lock is
held across the program, inference or adviser waits. Windows are private to one
player, epoch, activation, job, SDK run and program; restart does not restore them.

The gate reduces interference by other roles in this process. External changes
can still invalidate observations, so every original freshness, ownership and
native legality check remains active. Expiry stops fresh tail operations while
already admitted effects settle against their original receipts. Unknown effects
fence further admissions until trusted receipt settlement. Feedback attributes
staleness to an own-role mutation only when exact receipt revisions form a linked
chain; otherwise the cause stays unknown. Neither diagnosis authorizes a replay.

Strategy decisions can carry a `followThrough` record with role/task references,
the next step, blocking facts, reassessment triggers, route assumptions and
investment alternatives including waiting. City plans can refer to those stable
strategy decision IDs. These are authored intentions, not ownership or completion
claims. Startup also presents a bounded advisory index of delivered civilian
routes and dated rejected/unexecuted action attempts. Omission counts remain
explicit; historical entries never grant fresh inspection credit.

With `intent_handoff_review=true`, a new current-turn specialist failure offers
Main a final reassessment before handoff. Receipt IDs prevent repeated prompts
for the same attempt, with bounded attention bookkeeping per activation. Main
can revise the plan, wait, hold or accept risk and finish. No action is required,
and the host never repeats an attempted action automatically. Older policies do
not gain this additional pass. Existing policy serializations and fingerprints
retain their legacy shape when these additive settings are absent.

## Local monitoring overhead

Operator activity views cache bounded rendered snapshots using notifications
around public SDK StepStore writes. Pending, cancelled or uncertain writes
invalidate the cache; notifications are not proof of a committed action. A store
without notifications uses the uncached path. Atomic publication, ownership
binding and the existing two-second publication ceiling remain in place; reused
activity retains its original data timestamp.

Strict decision contracts and union validators are compiled once per immutable
schema key in bounded caches. Keys include role, catalogue/grants, report schema,
presentation mode and an optional policy discriminator. Mutable handlers, observations and authority are
never cached. `operator-views/performance.json` records bounded local timings for
watch reads, snapshot restore/render, publication commits and event-loop lag,
plus cache counters. These measurements require no telemetry upload or model call.
