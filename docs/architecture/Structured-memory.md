# Structured civilization memory

The service uses Pydantic for agent-authored records, tool inputs and outputs, and
structured summaries. SQLAlchemy Core defines the tables and queries; SQLite is
authoritative. Markdown files are generated views,
and editing them does not change an agent's memory. This memory format requires a
fresh match; no migration from the previous freeform store is provided.
The [SQLAlchemy migration](Database-access.md) opens existing structured-memory
format-2 databases directly and does not require a new match.

## Record types

| Type | Required content | Meaning |
| --- | --- | --- |
| `soul` | `text` | Long-term principles; one active SOUL |
| `plan` | `objective`, `approach`, `successCriteria` | Intended actions, not enacted game facts |
| `belief` | `claim`, `uncertainty`, `sources` | A sourced assessment with low, medium or high uncertainty |
| `note` | `text` | Miscellaneous private information; optional related `plan` |

Each supports `subjects`, `priority` (1–5, default 3), and optional `expiresTurn`.
The service assigns short IDs such as `mem1`, creation/update turns and revisions.
Subjects can name a civilization, an entity handle, or a topic such as `economy`.
Players choose their own subjects; no strategic categories are required or favored
by gameplay context selection. Plans include success criteria and an optional
future `reviewAfterTurn`. Omitting it schedules no automatic review. A scheduled review is a
progress check, not retirement: the plan stays active but requires reconsideration
before it can be reused as an action's intent. Revise with a new review turn or
retire it after checking the evidence. Success criteria remain agent assessments.
Observations, attributed statements and receipt-linked decisions are separate,
service-owned record types. Agents cannot create or overwrite them.

```json
{"changes":[{"op":"create","content":{"type":"plan","objective":"Improve Rome's food supply","approach":"Queue a granary and improve food tiles","successCriteria":"Rome has adequate food for growth","subjects":["economy","c1"],"priority":4}}]}
```

Pass this to `memory_update`. The result contains the assigned ID. Use
`memory_read` with `{"ids":["mem1"]}` before revising it, then send `op: "revise"`,
`id: "mem1"` and the complete updated `content`. Code attaches the version actually
delivered to the job. Conflicting edits return an individual rejection while
independent valid edits in the same batch commit. Retrying the same tool call
does not duplicate records.

For a small revision, use `op: "amend"` with only the changed content fields:

```json
{"changes":[{"op":"amend","id":"mem1","fields":{"approach":"Improve the Wheat before starting the next building","reviewAfterTurn":null}}]}
```

Omitted fields retain their values. A list replaces the entire old list; it does
not append. Explicit `null` clears an optional nullable field. The service
validates the merged content before writing and retains the same delivered-version,
evidence, review and retirement checks as `revise`. Type changes require `replace`;
IDs, timestamps and review metadata are never editable. An invalid amendment
rejects that edit completely; independent valid edits in the batch can still commit.
The service records routine observations and actions automatically. Private memory
is useful for durable objectives, reasons, decisions and unresolved questions;
duplicating the current roster or writing a turn diary is optional overhead.

## Sources and assumptions

`sources` cite records or messages already delivered to this job, including
`observation:overview` from `player_turn`, `observation:communication` from `chat_read`,
or the `evidence` ID returned by a detail query. Cached raw snapshots are not
delivered evidence until explicitly read. Beliefs also accept
`contradictingSources`. The service pins each source revision. Historical search
returns evidence handles for referring to the specific revision that was read.

Each source list has a maximum of **20 citations**. Select relevant support for
the current revision rather than accumulating every historical reference. Earlier
revisions retain their evidence when a list is shortened. Oversized lists return
the field path, received count, limit and repair guidance without echoing evidence.

Detail responses use short handles such as `e123`, pinned to that exact revision.
Copy them instead of constructing observation hashes. Reading the query again
after it changes yields a new handle; the old one remains a historical citation.
Handles are usable only by jobs that received them. A revision may retain its own
existing citations without rereading their sources, including across turns.
This does not let a new claim borrow unread evidence from another record.

`sources` and `contradictingSources` are historical references, even when a cited
private record has since changed, needs review, or is retired. A note's `plan`
field is also a historical link: recording why a plan failed never requires
repairing or reactivating that plan. Neither form authorizes a gameplay action.

Plans optionally declare ongoing `assumptions`, for example:

```json
{"text":"This city remains available for development","source":"<evidence from city_details>"}
```

A historical sighting does not become false when the unit moves. An ongoing
assumption about that unit's current location does need review. Retracted/replaced
beliefs flag plans that explicitly assume those beliefs still hold. Merely citing
the old belief does not create that dependency. Relevant diplomatic changes and loss of owned
cities or units flag plans with matching subjects. Turn counters and unrelated
greetings do not invalidate plans. A due progress review independently flags the
plan. New confirmed combat losses flag defense and economy plans, losing the last
military unit flags defense, and a fall in city count also flags expansion.
`player_turn.strategyReview` lists current plans, review reasons and missing topic
coverage. Read a listed record with `memory_read` unless its current copy was
already delivered in startup context or `memoryUpdates`. `observation:tactics`
records the delivered tactical review. These flags and deadlines are versioned in
SQLite and participate in the existing restart and rollback mechanism.
See [compact observations](../reference/Compact-observations.md) for detail-query delivery and
selective invalidation when a new snapshot arrives.

Overview reads and successful gameplay action responses include `memoryUpdates`
when a private record already delivered to this job has changed. Each entry is
`{"version": 4, "record": {"id": "mem1", ...}}`; replace older copies by ID.
Updates include review flags, external edits and retirements. They commit with
the observation and saved response. Replaying an old response does not rewind
the service's delivered versions. Records not previously read remain selective.

`reviewSources` identifies evidence that triggered a record's review. Receiving
the updated record does not mark that evidence as read: retaining an assumption
requires current evidence. A full revision can explicitly drop an obsolete
assumption without rereading it. Other unread review evidence remains relevant.
Rejected memory edits with unread,
outdated or unreviewed evidence carry `requiredSources`, `recoverable: true`,
and `recovery.reads`, including the necessary detail queries and a final private
record refresh. Prefer narrow detail evidence for ongoing assumptions; the whole
overview changes on many ordinary actions.

Unknown source IDs receive bounded `recovery.sourceSuggestions` containing only
previously delivered references and their queries. The service never silently
substitutes a similar ID, and it does not suggest rereading a nonexistent record.
The agent must decide whether a suggested source supports the statement.
During the background memory phase, `recovery.unavailableSources` lists requested
references that were not delivered before handoff. Rereading a private plan does
not deliver its `reviewSources`. Save independently supported changes using the
completed turn's evidence, or leave changes that need newer evidence for a later
job. The service keeps the newer review flags and never fetches live game/chat
data or silently drops citations to make an edit pass.

## Retirement and retrieval

```json
{"changes":[{"op":"retire","id":"mem1","reason":"completed","assessment":"Food supply is sufficient; shift spending to science"}]}
```

Reasons are `completed`, `abandoned`, `superseded`, `retracted`, `expired`, and
`irrelevant`. Explicit deadlines expire when a trusted observation reaches that
game turn. Retired records leave active context, default retrieval and Markdown.
Their revisions remain in history. Retirement records the assessment; it does
not establish an engine fact or cancel a public commitment.

Use `memory_read` with `includeRetired: true` to inspect a retired record.
`op: "replace"`, an existing `id`, and new `content` atomically create a successor
and retire the predecessor. An ordinary revision cannot reactivate an old ID.
Permanent purge is not implemented.

`memory_search` accepts `type`, `subject`, `query`, `state` (`active`, `retired`,
`all`), `id`, `history`, and `offset`. Results have at most 20 records, `hasMore`
and `nextOffset`. Historical reads cannot select an abandoned rollback branch.

## Decisions and files

Consequential commands and diplomatic decisions require `intent`:

```json
{"kind":"QUEUE_CONSTRUCTION","target":"c1","selection":"Granary","intent":{"plan":"mem1"}}
```

Alternatively use `intent: {"reason":"Increase food for early growth"}`.
Optional `expectedOutcome` and `assumptions` add detail. Research, policies,
settlement, construction queue changes, purchases/sales, upgrades/disbanding,
Great Person abilities, combat, captured-city decisions, friendship and
trade/commitment decisions require intent. Routine movement and other actions
may also provide it. The gateway strips intent before forwarding to Kotlin.

Missing intent returns `INTENT_REQUIRED` with `applied: false`. A retired,
changed or unreviewed plan returns recoverable `STALE_ASSUMPTIONS` before dispatch.
Read the indicated evidence and revise the plan, or supply a fresh reason for a
new decision. Retiring a plan after approval cannot undo an in-flight action.

Each stale memory-edit rejection includes recovery reads and conflict versions.
The host counts failed retries for each operation and record within the job.
A second stale rejection without an intervening success stops the run with
`REPEATED_STALE_ASSUMPTIONS`, saves diagnostics and interrupts the models.
A successful operation resets its counter; a later conflict starts a new recovery.
Agents follow recoverable responses and do not declare this terminal condition
themselves. Successful edits in a partially rejected batch remain applied.
Transport replays do not count as another attempt.
See [recovery diagnostics](../guides/Chat-memory-and-recovery.md#recovery-diagnostics).

`turn_N/summary.json` contains typed actions, receipts, observations, memory
assessments and unresolved work. `summary.md` renders at most 200 words. It does
not depend on a final response and is regenerated after receipt reconciliation.
Queueing a building remains queueing; a requested move does not establish arrival.
The exact requests and responses are in `receipts.jsonl`; full tool calls and
model reports remain in `commands.jsonl` and attempt transcripts. Communication
jobs have their own directories under `communication/`.

Current views include `SOUL.md`, `STRATEGY.md`, `economy.md`, `notes/mem1.md`,
`relationships/Greece.md` and `memory-index.md`. Relationship views link to the
separate commitment ledger. Obsolete generated files are removed on refresh.
Exports regenerate changed history groups and current views, preserving unchanged
turn files. Memory tool responses wait for the SQLite commit; generated files
refresh periodically and during finalization.

Scheduled turns now include a [post-handoff memory phase](Background-turn-memory.md).
The same conversation can batch private memory updates while the next player
acts. Game observations stay frozen, concurrent private edits retain version
checks, and newer review flags cannot be cleared using old evidence. No gameplay
or diplomacy calls are allowed in this phase. Its model usage remains attributed
to the completed turn.

Fresh jobs receive whole active records in deterministic subject/priority order,
review notices and new events. Configure the private-memory allowance with
`agent-server start Rome --memory-context-chars 12000` (default 12,000). The
context reports how many records were omitted; search retrieves them on demand.
Projection maintenance uses no model calls. Agent-authored memory changes use
ordinary model/tool requests, including any made after handoff.

The selection is pinned at dispatch. After startup's native and social reads,
the service refreshes those selected private records in batches of up to 20,
accounted as `startup:memory_read`. It renders private context once, after any
observation-triggered review or expiry. A selected record that expires during
preparation is delivered as retired. Other cached evidence stays unread.

Normal restart preserves committed memory. Whole-match rollback restores record
lifecycle, decisions and evidence with all players and the native save, then
rebuilds projections. IDs are never reused and old jobs remain fenced.
