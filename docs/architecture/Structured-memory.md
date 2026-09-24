# Private notebook and observation history

The service separates private assessments from command validation. SQLite stores
notebook entries, an immutable observation/action history, advisory notices and
an inbox of communication contributions. SQLAlchemy Core owns database access;
Markdown files are disposable views.

**This is memory format 3. Start a fresh match.** Older memory databases and
checkpoints are rejected without modification. There is no migration, old API
adapter or dual runtime. Restart and whole-match rollback work within format 3.

## Responsibilities

| Component | Responsibility |
| --- | --- |
| Observation/action history | Exact permitted observations, attributed statements, commands and confirmed receipts |
| Private notebook | Agent-authored plans, beliefs, reasons and questions |
| Advisory notices | Relevant changes and optional scheduled reminders, stored separately from notebook revisions |
| Command validation | Authority, relevant game-state freshness, native legality and duplicate-execution protection |

A belief is an assessment, even when it cites an observation. A citation proves
which revision was referenced; it does not prove that the claim follows from it.
A private plan's age, retirement or review notices never independently block a
native-legal command. Social promises remain advisory as well.

## Notebook API

`memory_update` accepts batches of up to 20 independent edits:

```json
{"changes":[{"op":"create","content":{"type":"plan","objective":"Improve Rome's food supply","approach":"Improve food tiles","successCriteria":"Rome can grow","subjects":["economy","c1"]}}]}
```

The four content types are `plan` (`objective`, `approach`, `successCriteria`),
`belief` (`claim`, `uncertainty`), `note` (`text`, optional related `plan` ID) and
`soul` (`text`, one active entry). Subjects, priority and expiry are optional.
Plan review dates and written assumptions are optional advisory information.
Belief citations are optional, including for speculative or uncertain claims.

Read an existing entry with `memory_read` before changing it. Use `amend` for
changed fields; omitted fields remain and arrays replace the old array:

```json
{"changes":[{"op":"amend","id":"mem1","fields":{"approach":"Improve the Wheat first","reviewAfterTurn":null}}]}
```

Use `retire` to archive an entry:

```json
{"changes":[{"op":"retire","id":"mem1","reason":"completed","assessment":"Food supply is sufficient"}]}
```

Retirement reasons are `completed`, `abandoned`, `superseded`, `retracted`,
`expired` and `irrelevant`. A retired ID cannot be reactivated. Create a new entry
when appropriate. The old `revise` and `replace` operations have been removed.

`memory_read` accepts up to 20 IDs and optional `includeRetired`. `memory_search`
filters by type, subject, text and active/retired/all state. It returns at most 20
records with pagination. `history: true` reads reachable historical revisions;
rollback's discarded future remains inaccessible.

The service owns IDs, timestamps and `basis`, which records the authoring job,
purpose and source turn. The job's durable delivered-observation record supplies
exact provenance without asking the model to maintain a list of everything it
read. The actual write turn can be later than the source turn during background
memory work.

## Writer ownership and communication contributions

Each civilization has one notebook writer: its gameplay conversation, including
its post-handoff memory phase. A second gameplay job cannot start while that
writer is running. A restart interrupts the orphaned job and releases ownership;
old jobs stay fenced.

Communication jobs can read private memory and use `create` to append an immutable
`contribution`. They cannot amend or retire notebook entries. Each contribution
retains the author's content and provenance; it never overwrites a plan.

A gameplay activation captures the available contribution IDs at its starting
snapshot. Selected contributions appear in bounded startup context; the rest can
be found with `memory_search(type="contribution")`. Later arrivals are saved for
the next activation, including during background reflection. The gameplay agent
can use a contribution to create or amend an entry, then retire the contribution
when handled. Incorporation is an agent decision, not an automatic semantic merge.

An unread or mismatched entry returns `MEMORY_CONFLICT` with a narrow recovery
read. The proposed edit is not applied. Other valid edits in its batch commit.
Repeated notebook conflicts do not set a match failure. Tool-call replay returns
the original result without duplicating entries or counting edits twice.
Each accepted edit returns its new version and full public record, including any
remaining advisory notices, so the model need not reread its own edit.

## Historical references and notices

Optional source lists contain at most 20 historical references. Exact detail
handles such as `e123` refer to immutable revisions. Current observation IDs and
message IDs are pinned to the versions actually delivered to the authoring job.
An amendment can retain its own existing citations without rereading them.

An unavailable citation produces `UNVERIFIED_REFERENCES` in the successful edit
result and `unverifiedSources` on the saved assessment. The text is retained, but
the reference is not treated as verified evidence. It grants no observation read,
reveals no referenced content, and does not silently substitute another source.

Notices are separate records projected as `needsReview` and `reviewSources`.
They do not change notebook content or its optimistic version. Relevant subjects,
direct explicitly cited assumptions, and optional review dates can generate a
notice. There is no recursive dependency invalidation and no obligation to repair
a plan before editing or acting. A changed belief can notify its direct dependents;
it does not invalidate a chain of beliefs and plans.

An amendment acknowledges notices delivered to that conversation. Notices from
later information remain pending. Background reads use the completed turn's
snapshot and cannot pull newer game, chat or contribution contents. Expiry runs
at the next gameplay writer boundary, so it cannot race an outgoing memory edit.

## Commands, context and durability

Consequential commands retain an audit intent: `{"reason":"why"}` or a plan
reference. The service records the referenced version when available, without
checking that the plan is active, reviewed or correct. Command-specific state
checks still apply. Repeated unexplained command-state conflicts still halt with
diagnostics; ordinary notebook disagreements do not.

Startup selects whole records within `--memory-context-chars` (default 12,000),
using deterministic subject/priority ordering. Omitted entries remain searchable.
The overview supplies current visible facts and advisory strategy notices.
`memoryUpdates` carries content revisions separately from game-state updates.
There is no requirement to save a turn diary or copy the current roster.

The service synchronously commits requests, receipts and replay identities.
Post-turn notebook work overlaps other players' turns in the same model
conversation. Archives and Markdown refresh asynchronously. Telemetry reports
accepted/rejected edits separately from transport/API errors; warnings on accepted
assessments are retained in the call journal.

Restart preserves committed entries, contributions, notices and receipts.
Whole-match rollback restores their captured heads together with all players and
the native game. IDs are never reused; history excludes abandoned branches.
See [background memory](Background-turn-memory.md),
[database access](Database-access.md) and
[recovery diagnostics](../guides/Chat-memory-and-recovery.md#recovery-diagnostics).
