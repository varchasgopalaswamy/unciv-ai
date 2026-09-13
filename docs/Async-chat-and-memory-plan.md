# Asynchronous chat, structured memory and emergency recovery

Status: first implementation delivered September 12, 2026. See
[launch and recovery instructions](Chat-memory-and-recovery.md). This document
records the agreed design; the broader player API and operating-system isolation
remain incomplete.

Build a civilization service that receives events, maintains structured memory,
dispatches model jobs against snapshots, and validates their proposals before
execution.

Normal recovery preserves confirmed work. An explicit operator-only emergency
rollback restores **all players and the game to a consistent checkpoint at the
start of a numbered game round**.

The first release includes public/private chat, social commitments, structured
memory, integration with existing gameplay commands, and a human CLI. The next
[economic trade slice](Asynchronous-trade.md) now implements host-owned binding
proposals and asynchronous execution; a browser interface remains later work.

## 1. Responsive runtime and communication

- Replace the single active-conversation model with a durable event inbox and a
  serialized state handler. Events include messages, observations, commitment
  changes, turn ownership, job results and command receipts.
- Dispatch model jobs asynchronously. No mailbox handler waits for model
  completion, a network response or another player. Completions return as events.
- Each job receives an immutable snapshot, its purpose, permitted proposal types
  and relevant dependency versions.
- Jobs propose actions, messages and memory updates. They receive no direct
  gameplay mutation credentials and cannot modify canonical memory.
- Model-facing proposals contain gameplay choices and intent, not transport
  bookkeeping. The service generates and durably stores request IDs, manages
  credentials, attaches snapshot/dependency versions, and reconciles retries.
  Models still select entity IDs (cities, units, technologies, proposals) and
  explicitly acknowledge gameplay consequences they have actually observed.
- Default to two concurrent jobs per civilization—one gameplay/planning job and
  one communication job—with four jobs match-wide. Make both limits configurable.
- Batch communication arrivals for one second. Private messages and explicit
  public mentions trigger work; other public messages remain available for the
  next relevant activation.
- Application code maintains the authenticated HTTPS event subscription and
  reconnect cursor. Quiet periods cause no model calls.
- Preserve model/reasoning settings and enforce job timeouts and invocation
  budgets. Queue work when capacity is occupied; communication never implicitly
  pauses gameplay.

Public chat includes everyone in the match from the start. Private chat initially
supports one-to-one conversations. Chat creates no diplomatic contact and reveals
no map state.

Provide interfaces to list channels, read history, send messages, subscribe to
permitted events and manage social commitments. Add CLI send, inbox, follow and
commitment commands. Use authenticated identities, short identifiers and
audience-filtered cursors that reveal no unrelated private activity.

## 2. Structured memory and commitments

Use a private SQLite database per civilization for events, immutable record
revisions, jobs, proposed changes and outgoing requests. Generate Markdown views
from committed records; Git is not the runtime transaction system.

Maintain four distinct information types:

| Type | Meaning and provenance |
| --- | --- |
| Observations | Player-permitted interface evidence, observation time/turn, source and freshness |
| Beliefs | Agent assessments with supporting sources, uncertainty and contradictory evidence |
| Private plans | Intended actions, assumptions, dependencies and status |
| Statements and commitments | Authenticated communication, explicit consent and separately confirmed engine outcomes |

An opponent's statement can support a belief; it cannot overwrite an observation.
For example, “My army is nowhere near your border” establishes that the opponent
made that statement. It does not establish the army's location or erase an
earlier sighting. An earlier sighting also does not prove the unit is still there.

Generate:

- `SOUL.md`: long-term objectives and strategic principles.
- `STRATEGY.md`: current priorities and unresolved decisions.
- `economy.md`: economic plans and resource needs.
- `relationships/<civilization>.md`: private assessments and references to
  discussions or commitments.
- A compact index of other available records.

Agents propose revisions through the service. Strategy files cannot override
operator instructions or tool permissions.

Replace concatenated historical turn summaries with task-selected context:
compact core strategy, relevant records and new events. Preserve readable
`turn_N` summaries, receipts and chat transcripts for inspection.

The host owns shared commitment records, with participant-filtered copies in
civilization memory:

1. **Statement or offer:** exact source messages, author, participants, terms,
   conditions and optional expiration.
2. **Acceptance:** explicit consent to an exact proposal version, followed by any
   withdrawal or supersession.
3. **Engine outcome:** confirmed action/deal and permitted receipt information.

Changing terms requires fresh consent. Natural-language interpretation never
establishes another player's acceptance. A social commitment does not itself
enact peace or transfer resources.

## 3. Proposal approval and feature integration

Introduce a common proposal gateway for existing gameplay commands,
communication, commitment decisions and memory updates.

- Supply mandatory dependencies by proposal type; models may add assumptions but
  cannot omit required checks.
- Version structured records independently: turn authority, relevant units/cities,
  economic state, diplomatic status, accepted commitments and applicable private
  policies.
- Validate on result arrival and immediately before dispatch. The host checks
  authoritative dependencies and legality atomically with execution.
- Accepted peace invalidates a conflicting attack proposal. An unrelated greeting
  does not invalidate economic work or restart the turn.
- Preserve independent valid results from partially stale jobs. Reconsider only
  affected proposals.
- Treat cancellation as a computational optimization. Invalidated results must
  remain unable to execute even when cancellation arrives late.
- Preserve observation and confirmation requirements. Refreshing a revision
  cannot silently acknowledge changed information the model has not seen.
- Outside a civilization's gameplay turn, allow communication, social commitments
  and private-memory updates. The subsequent economic trade slice adds a narrow
  bilateral-consent path for supported economic transfers. Ordinary gameplay
  commands remain turn-bound. Never switch Unciv's active player to bypass checks.

Conflicting old proposals are invalidated by social commitments. A fresh decision
may explicitly reconsider and break a promise; record that choice.

Existing war/peace commands and economic trades publish permitted outcomes into
this system. Further trade categories, friendship and demands reuse its version,
consent and receipt boundaries.

## 4. Normal recovery and the emergency rollback

**Normal recovery preserves confirmed work.**

- Persist host actions, shared records, receipts and outgoing events through the
  host's atomic checkpoint mechanism.
- Commit processed-event cursors, accepted memory changes and outgoing requests
  transactionally in each civilization's SQLite store.
- Persist requests before sending; reconcile uncertain responses using stable
  request IDs and host receipts.
- Discard unfinished job drafts and resume from committed state. Preserve
  independently completed work.
- Regenerate interrupted Markdown exports. Keep failed-job diagnostics outside
  compact working memory.

**Emergency rollback restores the complete match.**

Automatically capture a coordinated checkpoint at the start of every numbered
game round, before its first civilization receives gameplay authority:

- Briefly quiesce state commits and action dispatch across the host and
  civilization services. Continue receiving events into queues; do not wait for
  model jobs to finish.
- Reconcile outstanding external requests before establishing the checkpoint's
  consistent boundary.
- Record the native game, host receipts and ledgers, each civilization's memory
  revisions, inbox positions, pending events, outgoing-request state and scheduler
  position.
- Publish a checkpoint manifest only after every required component is durable
  and verified. An incomplete capture is not a restore point.
- Reference immutable history and memory revisions rather than duplicating
  complete transcripts. Retain round checkpoints and their referenced data in
  this first release.

Provide an explicit operator command to restore a selected round:

1. Pause the match, fence mutation access and cancel all model jobs.
2. Preserve the abandoned timeline in operator-only archives.
3. Stage and verify every checkpoint component.
4. Restore all participants under a new match epoch, invalidating old jobs,
   requests and credentials.
5. Regenerate readable files and start fresh model contexts using only the
   restored timeline.

Restoration is coordinated and restartable. If any component cannot be restored,
keep the entire match paused; never resume a mixture of timelines.

Usage accounting and operator audit history are retained across emergency
rollback. Abandoned messages, beliefs, plans and accepted commitments do not enter
restored agent contexts. A human's recollection of previously read messages cannot
be undone.

## 5. Delivery and verification

Implement in this order:

1. Versioned storage, memory projections and context selection.
2. Responsive event handling, bounded jobs and proposal validation for existing
   gameplay.
3. Public/private chat, event delivery and social commitments.
4. CLI access, coordinated round checkpoints and emergency restoration.
5. End-to-end regression and live acceptance scenarios.

Use fresh saves; no backward compatibility layer is required.

Required tests:

- Hold a planning job open while messages and a short response job are processed.
- Verify zero model calls during idle periods and bounded dispatch during message
  bursts.
- Use three civilizations to prove public delivery and private isolation.
- Verify misleading player speech remains attributed evidence rather than an
  observation.
- Accept peace during an outstanding attack job; reject the stale attack while
  retaining independent valid proposals.
- Deliver an unrelated greeting without invalidating the plan.
- Exercise explicit commitment reconsideration, changed terms, expiration and
  duplicate acceptance.
- Interrupt before/after memory commits, message publication, command execution
  and receipt delivery.
- Verify interrupted exports regenerate and concurrent jobs cannot overwrite
  newer memory.
- Restore a round after both players have acted and chatted; confirm all game
  state, memory and commitments match the checkpoint.
- Crash during coordinated capture and restoration; verify partial checkpoints
  remain unusable and mixed timelines never resume.
- Verify old credentials, queued requests and late job results cannot affect the
  restored epoch, while usage accounting remains unchanged.
- Run existing gameplay regressions through the proposal gateway, then audit a
  live scenario against native saves, host receipts and commitment records.

Keep dependency checks, approval reasons, timing and usage in readable
diagnostics, outside compact strategy context.
