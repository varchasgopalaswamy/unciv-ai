# Recovery and benchmark-workaround audit

Audited September 21, 2026: player prompts, gateway and private memory, service
and communication lifecycles, scheduler/history, state-update recovery, launch
scripts, and native scenario selection. Reviewed source, tests, prior run reports
and the commits introducing the relevant behavior. This is a targeted audit of
failure handling and exercise behavior, not a claim of complete gameplay coverage.

## Findings and changes

| Behavior | Origin/evidence | Resolution |
| --- | --- | --- |
| Repeated stale conflicts were deferred so gameplay could continue | Action guidance in `c1fc25f`; startup reinforcement after France stopped during the prompt benchmark, documented in `Prompt-streamlining.md` | Replaced with one recovery attempt, then a service-enforced stop for the same operation. |
| Individual memory edits reported only `reason: STALE_ASSUMPTIONS` | `CivilizationState.patch` swallowed the richer exception; this gap motivated the benchmark's blanket startup guidance | Each rejected edit now includes affected records, expected/current versions, and concrete recovery reads. Successful edits in the batch remain committed. |
| Overview-only recovery could leave cited detail evidence unread | The generic observation recovery branch remained after selective observations were introduced; a compact overview does not deliver every detail | Recovery now includes referenced plans/assumptions, reruns recorded detail queries, and explicitly reads remaining evidence. Plans are read after refreshes that could flag them for review. |
| Internal state-update errors were treated like temporary refresh outages | `413fea4` introduced a broad catch covering validation, programming and persistence errors alongside transport failures | Malformed snapshots and projection/persistence failures stop with `STATE_UPDATE_FAILED`. The confirmed action receipt survives; it must never be repeated. A transport outage can still require a fresh read. |
| Older services could omit the history API and scheduling still reported success | Compatibility branch from `2034b07`, including a test asserting continued turn scheduling | Removed. Unsupported history APIs and missing terminal history block further scheduling. Old persisted `eventsUnsupported` flags no longer bypass the check. |

Only the stale-deferral guidance has an explicit benchmark failure documented as
its motivation. The other findings were adjacent recovery/compatibility choices;
their presence alone does not establish they were temporary benchmark hacks.

## Recovery contract

1. The first `STALE_ASSUMPTIONS` rejection carries `recovery.reads`, conflict
   details, and instructions to reconsider the decision. No rejected action is
   dispatched. For memory batches, inspect each edit's `accepted` field.
2. One fresh attempt is allowed. Repeating the same transport call ID returns
   its original result and does not consume another attempt.
3. A second stale rejection for the same operation in that job records
   `REPEATED_STALE_ASSUMPTIONS`, `recoverable: false`, and a diagnostic ID. This
   also stops an agent that retries without doing the requested reads.
4. Gameplay operation identity includes the tool and chosen arguments, excluding
   explanatory `intent`. Memory-edit identity is operation plus record ID,
   independent of batch order and revised prose. Different operations have
   independent budgets. A successful operation clears its outstanding conflict.
5. SQLite stores the response, retry state and failure together. Diagnostics
   preserve both attempts, conflict versions, latest record writer/review reasons,
   and intervening read calls. The original read responses and versioned records
   remain in the database.
   An export/response failure after the database commits the stop cannot turn
   the failed job back into a runnable one.
6. New mutations and model jobs are fenced. The service stops its gameplay and
   communication models, without depending on their cooperation. The scheduler
   checks every civilization before/after turns and at five-second heartbeats,
   so an off-turn communication failure also stops the run. Already dispatched
   work retains its native receipt; no automatic rollback occurs.

Only the host decides that stale recovery is exhausted. Agents follow
`recoverable` responses rather than counting stale errors across a turn.
A successful action or memory edit resets that operation's counter, even when
another conflict occurs later for the same plan. The
[turn-11 audit](Gameplay-read-audit-2026-09-22.md) found an agent stopping itself
before this host condition fired; the prompt now states this distinction
explicitly. Tests cover successive recovery cycles and termination without model
cooperation. The stop remains enforced in code and persists across restarts.

Diagnostic JSON is stored at
`SERVICE_DIR/civilization/failures/<diagnosticId>.json`; the authoritative failure
also lives in `civilization/memory.sqlite3`. Agent status and scheduler output name
the error and diagnostic ID. A process restart preserves the stop. An explicit
operator reconnect during `match resume` clears the active stop only after old
jobs are closed and receipts reconciled; prior diagnostics remain. Checkpoint
restore uses the checkpoint's failure state under the usual new-epoch workflow.

## Reviewed and retained

- **Intentional unit/strategy deferrals:** agents may keep units idle or choose
  another goal. Those are player choices, not permission to ignore a recurring
  tool failure. Tactical review records the explanation without forcing an action.
- **Native built-in AI offers:** the native AI answers on its turn. Continuing
  gameplay while that offer is pending is required for it to answer.
- **Guided scenario outcomes:** trade/war/lifecycle exercises explicitly record
  unmet objectives and may continue ordinary turns for inspection. Their scenario
  observations are selected by the prepared map identity; ordinary Vanilla setup
  does not inherit their restrictions, seeded history or scripted objectives.
- **Confirmed handoff before model commentary finishes:** the native receipt is
  authoritative. Stopping the model after `END_TURN` avoids an extra paid summary;
  archives still must finalize and missing accounting is marked incomplete.
- **Transient history/transport retries:** polling and resynchronization can
  retry read-only work. Failed terminal history mirroring blocks the next turn;
  an uncertain action is reconciled by its original request ID.
- **Native telemetry exports:** a failed derived JSON export leaves the
  authoritative counters in the checkpoint, reports `telemetryExportError` in
  operator status, and retries on a later commit/restart. Native mutation or
  checkpoint failures themselves already halt the engine. The deadline worker's
  exception wrapper does not clear that halted state.
- **Launch limits and source provenance:** the Vanilla launcher has finite,
  generous limits and no automatic restart loop. A stopped run is reported for
  inspection. Quick speed, Prince difficulty and a Small Pangaea are explicit
  ordinary setup choices; all native victory types and the speed's turn limit
  are enabled. No shortened treaty or victory rule was found in that setup.
- **Prompt/cache optimizations:** startup context reuse, selective reads, single
  model-facing payloads and action deltas retain the underlying evidence and
  receipts; they do not mark failed commands successful.

No engine files or gameplay rules were changed in this audit. No live match was
restarted or new paid playthrough launched.

## Validation

- Full Python suite: **579 passed, 90 subtests passed, 1 skipped** (the opt-in
  installed-Codex cache capture).
- Ruff lint and formatting, Pyrefly, and `git diff --check` passed.
- Regression coverage includes successful recovery, explicitly cited detail
  refreshes, independent operations, transport replay, partial memory batches,
  durable stop/restart, export failure after committing a stop, cancellation
  notifications, off-turn communication failure, missing history APIs, and
  preserving confirmed receipts after state-update failures.
