# Runtime failure recovery

The runtime keeps ordinary model and provider problems inside an agent retry
policy. Application defects stop the game for inspection. After a fix, explicit
resume starts fresh reasoning at the current native position while preserving
committed actions and spent usage. It does not resume an execution graph or rewind
the world.

## Failure ownership

| Situation | Behavior |
| --- | --- |
| Invalid tool arguments or structured output | Pydantic AI returns validation feedback. If a conversation needs to restart, another conversation continues with current native state and cumulative usage. |
| Role completion with unresolved required choices | Native checks reject completion before readiness changes. The same agent receives feedback and continues. |
| Confirmed rejected optional note or plan edit | Public bounded tool feedback leaves the document unchanged. The model can continue required work. |
| Typed provider rate limit, quota, capacity, connection or server wait | Retry the configured model using the supported SDK retry hook, publish waiting state and respect turn/global limits. |
| Context window exceeded | Use maintained Harness compaction, then retry if the request became smaller. Preserve required instructions and recent complete exchanges. |
| Repeated expected model wait | Keep retrying with backoff, preserving the same request and model. There is no retry-count ceiling. |
| Uncertain native action or outgoing message | Reconcile the exact original native request and payload. A missing acknowledgement does not prove the action was absent. |
| Application bug, integrity conflict or unknown failure | Fail visibly, fence work and retain evidence. Do not silently turn it into a model retry. |
| Operator stop or whole-game limit | Save the native position and remain stopped. A process retry cannot override the stop. |
| JVM or controller process exits | Bounded systemd retry reuses the same container. An unfinished attempt requires a new explicit resume before fresh model work. |

The configured provider and model remain the same during automatic waits.
Streaming model responses are collected through stream cleanup before their
events reach the agent. This places connection failures, streamed provider
errors and request deadlines inside the SDK's ordinary request retry lifecycle.
Completed responses replay through the SDK's response adapter, so tools and
role completion execute after the response succeeds. Reported partial usage
from failed attempts remains in SDK accounting and the provider request log.
Cancellation closes the live stream and propagates to the controller.

Local play has no count limits for model requests, role activations, handoff
attempts, observation recoveries, summaries, or assessments. The native host's
`maxInvocations = 0` denotes an unlimited lifetime count. It still records every
invocation and enforces concurrent compute capacity. Operator stops and the
configured game duration and turn target remain authoritative.

`WarnOnCacheBusts` reports prompt cache disruption at WARNING level; it does not
fail a game or authorize a model/provider switch. Context reduction uses Harness
`TieredCompaction`, `ClearToolResults` and `SlidingWindowCompaction`, without an
extra summarization call.

## One process owner and one resume authorization

Each game controller has one systemd service bound to its exact container ID,
image and physical deployment. Podman's own restart policy is disabled. Systemd
provides bounded retry delays and start limits; failed units retain their counters.
Explicit stop passes through systemd and suppresses automatic restart.

The supervisor can reattach to the existing physical launch. Reattachment never
runs setup, deletes control files, rotates credentials or grants resume. A saved
launch records the operator's original intent. An explicit `game resume` creates
a new launch; the controller consumes that authorization once. If the resumed
process fails again, another same-launch process remains paused until another
explicit resume. An interrupted initial bootstrap also needs explicit resume.

The native JVM is a child of the controller process. Its unexpected exit fails
the owning container. A live container with a dead native host is not considered
healthy. Shared login and Decisions brokers retain their existing independent
ownership and spending records; ordinary game launch does not reset them.

## Fresh attempt at the latest world position

Explicit resume first acquires the existing game authority and closes the native
barrier. Old agent jobs are fenced. Accepted commands and deliveries are settled
with their original epoch, request IDs and exact payloads. Native `resolve_request`
returns an existing receipt or establishes a no-effect cancellation under that
barrier. Receipt-only application records the result against the old job; it
cannot dispatch a new action. Unsent outgoing drafts are cancelled with their
original text retained for inspection. Already sent messages remain sent.

Only after that settlement does the controller admit a fresh attempt. It creates
new agent runs with current native observations and new per-attempt allowances.
Old conversations are retained as evidence, not passed in as resumed model history.
Authored notes, structured plans, native receipts, completed handoffs and all
charged usage remain. The native world and its epoch are retained.

The original start time and absolute game deadline stay in force. Pauses count
toward elapsed time. Explicitly allowed limit increases extend limits from the
original start; they do not restart the clock. A retry cannot reset whole-game
invocations or token accounting. Usage that is unknown stays marked incomplete.

## Completion and cancellation

Each role has one terminal structured `finish_role` output containing its report
and completion fields. Fresh native validation checks the requirements owned by
that role before readiness changes. Research belongs to Main; Construction belongs
to the current city owner. Economy can explicitly escalate a native choice to
Main; Main can explicitly defer only when native rules permit it. Failed completion
is feedback, not a failed game or an accepted partial readiness update.

Main, Economy and Military use one standard `asyncio.TaskGroup`. Native role
readiness and change notifications drive eligibility, including late specialist
wakeups and additional passes. An accepted completion belongs to its exact job;
if a sibling reopens that role before the model response settles, the scheduler
starts a fresh pass after that run joins. Handoff still checks current readiness.
Failure or cancellation joins all local work before cleanup. Native effects already accepted by the host
are reconciled through their receipts; cancelling a Python task cannot undo them.
Communication and delivery admission are held across handoff and recovery.
The final native completion check remains the authority for ending a turn.

Review cycles freeze permitted evidence before a handoff and start only after
the native host confirms that handoff. A definite rejection discards the unused
snapshot. On restart, the native handoff counter decides whether a captured
cycle is committed or needs a fresh capture. Pending committed cycles can start
a fresh review conversation from the same frozen evidence. Completed advice is
reused, including advice from earlier local runtime versions. Publication checks
the epoch, cycle, frozen inputs, SDK transcript and report receipt together; it
cannot activate a turn. Expected bounded review failure publishes UNAVAILABLE,
so missing advice does not crash a game. Integrity failures remain visible bugs.

## Evidence and scope

Controller state records attempts, role phases, the original clock and resume
authorization. Harness StepPersistence records SDK messages, events and tool
effects. Domain records retain native operation attribution. The TUI reads
published views; history, profile and failures use passive captures of those stores. Native receipts remain the
commit authority; telemetry is observational.

The local tests cover structured completion, concurrency, restart fencing,
allowances, request accounting, native receipt settlement, launcher ownership and
operator views using temporary stores and synthetic responses.

Saves from the retired Temporal runtime are read-only archives. This version has
no `game recover` or developer `workflow` execution command and no converter.
Start fresh games with the local runtime. Keep the entire game directory, pinned
images and dedicated login directory when backing up a stopped game.


## Attempt ownership and observational exports

`begin()` returns an `AttemptContext` containing the frozen ticket, Main permit,
role/handoff passes, and per-role SDK usage/provider-attempt allowances. Every
conversation in that attempt shares its allowance; a newly authorized attempt
refreshes it. Controller deadlines and cumulative totals remain outside it.
`model_requests` retains its existing provider-attempt meaning; `sdk_requests`
counts SDK request steps, and `model_invocations` counts admitted native jobs.
Older saves without an SDK total retain `null`; `sdk_requests_observed` records
new observations without inventing an earlier total.

Controller writes use one ordered `ServiceIO` writer. Accepted writes retain
serialization ownership through repeated cancellation. Attempt archives precede
controller state, which precedes derived status. Native report envelopes are
persisted before dispatch and reused exactly after acknowledgement loss.
Uncertain permits stay in recovery inventory until settlement and operator
retirement under both barriers. Cleanup failures are sanitized separately from
the primary failure.
Shutdown drains each registered resource even when another close operation fails.

Bounded communication exhaustion publishes `UNAVAILABLE` in
`communication_deferrals`, retaining notice identities and the next eligible
confirmed handoff. It cannot immediately assign another paid batch. Explicit
resume reconciles old effects before renewing eligibility. Integrity failures and
uncertain settlement remain failures.

The runtime publishes versioned operator views under `operator-views/`. A single
worker coalesces refreshes, targets two seconds, and marks views stale after ten
seconds or a failed newer publication. The dashboard retains at most 512 recent
host records, 32 recorded SDK runs and 600 activity entries; those request counts describe that bounded window. Whole-game
accounting stays in native totals. SDK histories are captured on demand through
the owner-only `owner.sock`; its fixed requests accept no SQL or filesystem path.
Current and previous view/capture generations are retained.

Observers use published views and private scratch captures. Default history
exports describe independent committed captures with a capture window and
per-store hashes. `history export --coordinated` requires a stopped deployment,
exclusive lifecycle ownership and drained writers. Legacy stopped captures copy
main/WAL bytes outside the source tree before opening SQLite. WAL presence is
never evidence that an immutable connection is safe. A live export is evidence,
not a coordinated recovery checkpoint.
