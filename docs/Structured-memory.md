# Structured civilization memory

The service uses Pydantic for agent-authored records, tool inputs and outputs, and
structured summaries. SQLite is authoritative. Markdown files are generated views,
and editing them does not change an agent's memory. This memory format requires a
fresh match; no migration from the previous freeform store is provided.

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
Gameplay plans should separately cover `defense`, `expansion` and `economy`.
Use measurable success criteria and a future `reviewAfterTurn`. When omitted,
the service schedules a review five turns after creation or revision. This is a
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

## Sources and assumptions

`sources` cite records or messages already delivered to this job, including
`observation:game` from `player_turn`, `observation:communication` from `chat_read`,
or a scope such as `observation:city/c1`. Beliefs also accept
`contradictingSources`. The service pins each source revision. Historical search
returns evidence handles for referring to the specific revision that was read.

Plans optionally declare ongoing `assumptions`, for example:

```json
{"text":"This city remains available for development","source":"observation:city/c1"}
```

A historical sighting does not become false when the unit moves. An ongoing
assumption about that unit's current location does need review. Retracted/replaced
beliefs flag dependent records. Relevant diplomatic changes and loss of owned
cities or units flag plans with matching subjects. Turn counters and unrelated
greetings do not invalidate plans. A due progress review independently flags the
plan. New confirmed combat losses flag defense and economy plans, losing the last
military unit flags defense, and a fall in city count also flags expansion.
`player_turn.strategyReview` lists current plans, review reasons and missing topic
coverage; use `memory_read` before revising a listed record. `observation:tactics`
records the delivered tactical review. These flags and deadlines are versioned in
SQLite and participate in the existing restart and rollback mechanism.

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

Fresh jobs receive whole active records in deterministic subject/priority order,
review notices and new events. Configure the private-memory allowance with
`agent-server start Rome --memory-context-chars 12000` (default 12,000). The
context reports how many records were omitted; search retrieves them on demand.
No extra model calls or model polling are used for memory maintenance.

Normal restart preserves committed memory. Whole-match rollback restores record
lifecycle, decisions and evidence with all players and the native save, then
rebuilds projections. IDs are never reused and old jobs remain fenced.
