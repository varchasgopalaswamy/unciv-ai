# Monitoring and explicit exports

The operator dashboard reads `runner/monitoring.sqlite3` and local per-player
memory databases for strategic reports. It never constructs a player service,
contacts the game, or opens a write transaction. These databases are owner-only;
the operator database must not be mounted in a player's sandbox.

## Records and ownership

`monitor_records` stores JSON payloads with indexed collection, identity, and
revision columns. Updating a record assigns a new, monotonically increasing
revision; unchanged payloads keep their revision. The collections are:

| Collection | Owner | Contents |
| --- | --- | --- |
| `state`, key `run` | Scheduler | Authoritative run settings, budgets, active turn, sessions and pending accounting |
| `state`, key `result` | Host | Scores, winner, match totals and setup provenance |
| `state`, key `diplomacy` | Host | Current recovery epoch and the active shared negotiation, if any |
| `diplomacy` | Host | Shared messages, offer versions, decisions and native outcomes, deduplicated across recipient copies |
| `turns` | Host | Per-turn tokens, calls, timing and accounting completeness |
| `jobs` | Host | Reported usage and settings for each model job |
| `host-api` | Host | Native API counts by handoff and actor |
| `streams` | Scheduler | Epoch, player, turn and activation bindings |
| `events` | Scheduler | Sanitized service events, indexed by stream and sequence |

The host publishes changed rows from its durable accounting database and committed
game state on a separate worker, normally once per second. Native game changes
remain authoritative in the game checkpoint; job reports and counters live in
`<checkpoint>.accounting.sqlite3`. See [host accounting](Host-accounting.md) for
the shared commit outbox and restart behavior. A failed
monitoring write cannot undo a game command: operator status exposes
`telemetryWriteError`, dirty rows remain pending, and restart republishes them.
The host never replaces scheduler state or event streams when rebuilding telemetry.

The scheduler's run state and event cursor are committed directly to SQLite;
there is no parallel `run.json` or event-file mirror. Services still retain their
private append-only event journals provide the authenticated event stream,
including when services run on another machine. Per-player memory, frozen review
inputs and accounting databases remain separate from the operator store.

## Concurrent reads and writes

Writers use short transactions and SQLite WAL. The TUI uses `mode=ro` connections
and ordinary `BEGIN`, not the memory store's `BEGIN IMMEDIATE`. Each poll closes
its read transaction before rendering. It reads revised status/turn rows and at
most 1,000 new events per selected stream, keeping decoder state between polls.
Unchanged histories are not reread. Selecting another turn starts its own cursor.

Rewinds retain operator streams under their original epochs. New attempts get
new bindings; the viewer filters its current view by the scheduler's epoch.
No history-directory moves are required. Native saves, communication state,
private memories, pending review inputs and accounting keep their existing recovery
contracts. Monitoring does not replace the coordinated turn checkpoint.

Diplomacy rows derive from the host's committed communication streams. The host
tracks copied recipient cursors in memory. The publication worker retains queued
rows until the monitoring transaction succeeds. Shared events use one participant's stable event
identity, so recipient copies appear once while repeated decisions remain distinct.
Restart rebuilds the projection; unchanged rows keep their revisions. An epoch change reprojects
only the restored communication history. Event bodies retain original game-turn
numbers; monitoring publication time is not presented as a message timestamp.

The Strategy tab reads completed advice and pending review metadata directly from
the local player's `reviews` table. It does not copy reports into the operator
database or load frozen inputs. Read-only queries of gameplay snapshots identify
the first preload that included each report. Review duration and usage come from
the host's existing `jobs` records, matched to the source turn, civilization and
epoch. Source epochs can predate a rewind; the player's current identity and
restored review rows determine which reports remain visible. Discarded future
reports and gameplay deliveries are excluded. Remote services need their local
review databases accessible to the operator to display reports.

## Reports

Gameplay does not render Markdown, export notebooks, copy completed turn archives,
or regenerate JSONL telemetry. Turn completion requires durable command outcomes
and accounting. Scheduled review inputs commit before handoff, and reports are
written asynchronously. The two-second service task samples and flushes accounting
only. Explicit archive requests remain available for inspection.

```sh
civ-agent history export --state-dir /path/to/match
civ-agent history export Greece --state-dir /path/to/match --output-dir /path/to/reports
civ-agent history export Rome --state-dir /path/to/match --notebooks
```

The command reads committed monitoring records and creates a fresh export directory
with conversations, telemetry, diplomatic exchanges in `diplomacy.jsonl`, and an
index. Player selection includes diplomatic messages sent or received. The command
makes no model or game API requests.
`--notebooks` also reads the selected local players' private databases through
read-only connections and writes their notes, summaries and receipts under
`notebooks/`. This does not instantiate services, interrupt active jobs, or modify
canonical memory. Remote private databases must be exported on their own machine.
Native saves, configuration, credentials, private event journals, final player
result records and diagnostic logs remain durable files.

## Storage and backups

The runtime reads the database format. Older file-based matches require the older
code; there is no import or migration command. Start a fresh match with this version.

Backups must include committed SQLite data and any live WAL. For a running database,
use SQLite's backup API; copying only the main file can omit recent records.
