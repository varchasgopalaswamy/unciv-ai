# Private memory and observation history

Private memory preserves knowledge and provisional assessments. Current game
observations come first in the changing part of the turn preload; historical
strategy follows as data. Stable instructions and capabilities remain at the
front for prompt caching.

**Memory format 7 requires fresh private stores in a new match directory.** Older
databases and checkpoints are rejected without modification. There is no legacy
runtime or automatic migration. Preserve old match directories for auditing.
Restart and turn rewind work within format 7.

SQLite remains authoritative, with SQLAlchemy Core owning access. Immutable
observations, decisions, receipts, notebook revisions and communication
contributions retain their existing durability and privacy boundaries. Markdown
files are disposable views.

Job snapshots store immutable record/version references, with small patches for
frozen review notices. Dispatch loads notebook entries, contributions and pending
events; historical detail queries enter the job only when read. Completed jobs
retain those references without copying the full current record store again.
Operator reports use a separate reference to the
job's latest native view; that reference does not mark it as model-delivered evidence.
Dispatch also retains small version counters for command freshness checks, without
loading the corresponding observation bodies or marking them delivered.

## Memory content

| Type | Purpose | Updating it |
| --- | --- | --- |
| `strategy` | A provisional assessment, with reasons, uncertainties, conditions for reconsidering and conditions for changing course | Replace its complete content; at most one active |
| `handoff` | Unfinished work or questions for the next game turn | Replace its complete content; at most one active |
| `belief` | A claim with uncertainty and optional historical citations | Amend changed fields or retire |
| `note` | Useful retained information, optionally referring to another memory ID | Amend changed fields or retire |

The agent chooses its strategy; the service neither scores it nor requires a
particular victory route. There is no separate persistent `plan`, `soul` or
success-criteria record. Short-, mid- and long-term plans belong in the strategic
review as provisional decisions.

### Routine strategic review

A separate read-only adviser works asynchronously every K turns, starting at turn
K (`--review-interval`, default 5). It compares every enabled victory type,
remaining requirements, timing assumptions and observed rivals. Every path leads
with a useful intermediate objective, required investment and expected payoff
relative to rivals, including their likely responses. Development, expansion,
diplomacy and military action can contribute to any victory path. Consequential
uncertainties are tied to the next decision and how to resolve them.

The assessment can identify a preferred victory or leave the choice open. It
still recommends concrete next steps, options to develop, commitments to make or
defer, and evidence that would change the plan. It considers other promising
approaches, ways to disrupt rivals and conditions for pursuing or ending them,
and the relative value of agreements, then suggests short-, mid- and long-term
plans. The prompt defines the review's goals and leaves analytical methods to the
reviewer.
Projections can describe development milestones and conditions, with detail
suited to the game stage and evidence.

The service captures permitted position and planning data before the native
handoff. The reviewer uses the player's model, effort and priority, with frozen
read tools, known foreign cities and recent factual actions. Private strategy,
handoffs and the player's action rationales are excluded. It cannot act, communicate
or edit memory. Other civilizations can play during the review; this civilization's
next turn waits until the report is saved and includes it as dated advice at
dispatch. Following the advice is optional, and it need not be copied into the notebook.

Reports and pending inputs are durable and rewind with private state. Retry
counts and spending survive rewind. The default attempt limit is 3; exhaustion
stops for inspection. Completed reports replace pending inputs atomically.
See [background reviews](Background-turn-memory.md) for lifecycle and accounting.

Updating a handoff does **not** change the strategy's content, authoring date or
pending notices. The service dates writes to their authoring turn, even when
concurrent communication observes a later native turn. This date records authorship,
not a certification that the agent reconsidered every assumption.

A handoff authored on turn N is selected for gameplay context on N+1, including
retries of that turn. It is not preloaded into communication jobs. On a later
gameplay writer boundary it retires unless replaced; its history remains
searchable. Explicitly selected historical memory remains data the agent can read.

Handoffs describe unfinished work, unresolved questions and useful context that
current observations will not supply. The writing guidance omits routine rosters,
queues, treasury figures and instructions to preserve yesterday's plan. Provisional
strategic assessments belong in `strategy`; the service does not rewrite player
prose or reject a memory because of its strategic opinion.

## Notebook API

`memory_update` accepts up to 20 independent edits. For example:

```json
{"changes":[{"op":"create","content":{"type":"strategy","assessment":"Science looks promising while the border is quiet","reasons":["Our universities are productive"],"uncertainties":["Egypt's intentions"],"reconsiderWhen":["War or a major production opportunity"]}}]}
```

Read an existing entry with `memory_read` before editing it. Replace a strategy
or handoff with its complete desired content; omitted fields return to defaults:

```json
{"changes":[{"op":"replace","id":"mem1","content":{"type":"strategy","assessment":"The war changes our immediate priorities","reasons":["The border city has lost its defender"]}},{"op":"create","content":{"type":"handoff","text":"Check the worker's unfinished road next turn"}}]}
```

`amend` works for notes and beliefs only. Omitted fields remain; supplied arrays
replace the old array. It cannot change the content type or service metadata.

```json
{"changes":[{"op":"amend","id":"mem3","fields":{"uncertainty":"high","sources":[]}}]}
```

`retire` accepts a reason (`completed`, `abandoned`, `superseded`, `retracted`,
`expired` or `irrelevant`) and optional assessment text. Retired IDs cannot be
reactivated; create a new record if needed. Stored `retirementAssessment` stays
separate from a strategy's original `assessment`.

`memory_read` accepts up to 20 IDs and optional `includeRetired`. `memory_search`
filters by type, subject, text and active/retired/all state, with pagination.
`history: true` exposes reachable revisions, excluding rollback's discarded future.
Notes use optional `relatedMemory` for a historical memory ID, never prose.

Each accepted edit returns its version and full public record. Unread or
mismatched entries return `MEMORY_CONFLICT` with a narrow recovery read. Other
valid edits in the batch commit. Notebook conflicts do not halt the match.
Replay of a tool call returns its original result without duplicating edits.

## Evidence and notices

A citation identifies a historical revision; it does not prove a claim is true.
Exact detail handles and delivered observation/message IDs can be cited. An
entry can retain its own citations without rereading them. Unavailable citations
remain explicitly unverified, granting no read and exposing no hidden content.

Advisory notices are separate records projected as `needsReview` and
`reviewSources`. Relevant subjects and cited assumptions can produce notices.
Material changes such as confirmed losses, city-count changes and changes in war
status also notify the active strategy even without subject tags. Notices report
facts; they do not dictate a response or recursively invalidate beliefs.

Editing an entry acknowledges only that entry's notices already delivered to the
conversation. Later notices remain pending. A handoff edit cannot acknowledge a
strategy notice. No notice, citation, strategy age or retirement independently
blocks a native-legal command. Social promises remain advisory as well.

Consequential actions require a current `intent.reason`. An optional
`intent.strategy` references a historical assessment, but cannot replace the
reason. The service records its version when available without checking that the
assessment is active, reviewed or correct. Native legality and command-specific
freshness checks remain enforced.

## Background work, context and recovery

Gameplay is the sole notebook writer and can record optional knowledge while
playing. There is no post-turn model bookkeeping. Communication jobs append
immutable contributions; the reviewer only publishes its separate advisory report.
Contributions arriving after dispatch become available to the next activation.

Expiry runs at the next gameplay writer boundary so it cannot race the outgoing
writer. Startup selects complete records within `--memory-context-chars`
(default 12,000), prioritizing the strategy and eligible handoff, then relevant
knowledge. Equally relevant, equally prioritized records favor recent updates.
Historical notes/beliefs are eligible when updated in the last two turns, related
to current subjects, or carrying a review notice. Subject matching ignores case.
Startup uses the fresh permitted opening reads to identify owned cities/units,
current research/construction, wars and visible nearby forces, growth/economic
problems, resource use/losses, active expansion, and current social counterparts.
Subjects in this/last turn's diplomatic events also count. Player prose is not
parsed into priorities. Selection then stays fixed for the activation, without changing it when a background review completes. Selecting a
record does not deliver its evidence: the existing startup memory reads do that.
Selection refreshes eligible dispatched notebook records and freezes the versions
whose serialized sizes fit the budget. Startup reads deliver those same versions;
later explicit reads can obtain edits that arrived in the meantime.
Contributions arriving after dispatch still belong to the next activation.
Unused context budget does not pull in unrelated older records. Strategies,
eligible handoffs and pending communication contributions have explicit lifecycles.
Expired notes and communication contributions are excluded before selection, so
they cannot consume the preload budget. Retirement commits at the next gameplay
writer boundary, preserving history and the outgoing writer's frozen evidence.
The writer retires handled contributions in the same batch as retaining useful
knowledge, or without replacement when current observations already cover it.
Omitted records remain searchable. This prioritization supplies
continuity; it does not make the old assessment authoritative.

Ordinary overview progress does not generate a generic reconsideration notice.
Material changes include wars, confirmed losses, force/city losses, resource
unavailability, a Golden Age ending, negative happiness and victory milestones.
Focused historical assumptions can still receive notices identifying changed
fields; evidence freshness and action validation remain independent of notices.
No memory write is required when nothing useful changed. Temporary next-turn
context belongs in a handoff, not an indefinitely retained general note.

Turn summaries derive from confirmed actions and outcomes. Merely having an
active strategy no longer generates an unresolved obligation. Routine rosters
and turn diaries need not be copied into memory. `STRATEGY.md` and `HANDOFF.md`
render current briefs; all revisions remain in the database.

Requests, receipts and replay identities commit synchronously. Strategic reviews
and provider accounting overlap other players' turns. Human-readable reports are explicit exports. Telemetry counts accepted and
rejected edits separately from transport errors. Restart fences interrupted
writers; turn rewind restores captured heads and excludes abandoned
future revisions. IDs are not reused.

See [background reviews](Background-turn-memory.md),
[database access](Database-access.md),
[recovery diagnostics](../guides/Chat-memory-and-recovery.md#recovery-diagnostics)
and [saved-position comparisons](../scenarios/Strategic-positions.md#memory-comparisons).
