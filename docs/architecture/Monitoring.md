# Monitoring and explicit exports

The operator dashboard reads the native runtime's published epoch and status,
`runner/monitoring.sqlite3`, and private per-player domain and Harness stores.
It does not contact the game, Temporal or a provider. These are privileged
operator views; operator databases and other players' stores must not be mounted
in a player's sandbox.

## Records and ownership

`monitor_records` stores JSON payloads with indexed collection, identity, and
revision columns. Updating a record assigns a new, monotonically increasing
revision; unchanged payloads keep their revision. The collections are:

| Collection | Owner | Contents |
| --- | --- | --- |
| `state`, key `result` | Host | Scores, winner, match totals and setup provenance |
| `state`, key `diplomacy` | Host | Current recovery epoch and the active shared negotiation, if any |
| `diplomacy` | Host | Shared messages, offer versions, decisions and native outcomes, deduplicated across recipient copies |
| `turns` | Host | Per-turn tokens, calls, timing and accounting completeness |
| `jobs` | Host | Reported usage and settings for each model job |
| `host-api` | Host | Native API counts by handoff and actor |

`native-runtime/game.json` and the verified current-epoch publication bind the
frozen game settings and player identities. `native-runtime/status.json` and
`result.json` are workflow projections, separate from the host result. A bounded
workflow stop is not a native victory. Missing publications remain pending;
invalid or conflicting ownership and epochs require inspection. The dashboard
does not fall back to old scheduler records.

The host publishes changed rows from its durable accounting database and committed
game state on a separate worker, normally once per second. Native game changes
remain authoritative in the game checkpoint; job reports and counters live in
`<checkpoint>.accounting.sqlite3`. See [host accounting](Host-accounting.md) for
the shared commit outbox and restart behavior. A failed
monitoring write cannot undo a game command: operator status exposes
`telemetryWriteError`, dirty rows remain pending, and restart republishes them.
Host accounting remains authoritative for input, cached input, output and
reasoning output tokens, including its completeness flag. SDK histories can
explain individual requests; repeated snapshots are not additional requests.
Per-player domain state, frozen review inputs and SDK histories remain separate
from the host publication.

## Concurrent reads and writes

Writers use short transactions and SQLite WAL. Operator database readers use
read-only connections and close each transaction before rendering. Native
Harness history readers use SQLite backup copies and public `SqliteStepStore`
APIs; they do not initialize a schema in a live store. Scope checks bind runs,
conversations and tasks to the published player and epoch. Activity views are
bounded and terminal text is escaped.

While an explicit Recovery is selected, the dashboard shows its recovery
projection instead of reading an unpublished target as an ordinary game. After
publication it uses the selected target epoch. Historical source data remains
private and cannot silently supply current activity. Monitoring does not replace
the coordinated host, domain, Memory and Planning checkpoint.

Diplomacy rows derive from the host's committed communication streams. The host
tracks copied recipient cursors in memory. The publication worker retains queued
rows until the monitoring transaction succeeds. Shared events use one participant's stable event
identity, so recipient copies appear once while repeated decisions remain distinct.
Restart rebuilds the projection; unchanged rows keep their revisions. An epoch change reprojects
only the restored communication history. Event bodies retain original game-turn
numbers; monitoring publication time is not presented as a message timestamp.

The Strategy tab reads completed advice and pending review metadata from the
player's domain database. It does not load frozen inputs or copy reports into
the operator database. Review duration and usage come from matching native host
jobs; live progress comes from the native SDK history. The current projection
does not establish the first gameplay delivery of a report, so that measurement
remains unavailable. Restored review rows and the published epoch determine
which reports remain visible.

## Reports

Gameplay persists native histories and authored state. Inspection exports are
explicit and are not part of turn completion.

```sh
civ-agent history export --state-dir /path/to/match
civ-agent history export Greece --state-dir /path/to/match --output-dir /path/to/reports
civ-agent history export Rome --state-dir /path/to/match --notebooks
```

The command validates the current native epoch publication and creates a private
export directory with `index.json`. Public Harness `SqliteStepStore` APIs read
SQLite backup copies; conversations retain Pydantic AI's canonical message JSON
and native run, snapshot, event and tool-effect records. It makes no provider,
Temporal or game API requests and never initializes an SDK schema in a live store.
The default is the published current epoch. `--epoch EPOCH` explicitly selects
an ancestor in the verified publication lineage; unselected player stores are
not read. The files are privileged operator exports containing private context.

`--notebooks` adds exact native Markdown notes/version metadata and all four
native role plans through public Memory/Planning APIs on backup copies. Each
database is independently committed: this is an observational export, not a
coordinated recovery checkpoint. Live stores and active jobs remain unchanged.
The option refers to authored game notes, not Jupyter notebooks. There is no
legacy service-history fallback.
Native saves, configuration, credentials, private event journals, final player
result records and diagnostic logs remain durable files.

## Optional model-worker tracing

Use the local [Phoenix](https://arize.com/docs/phoenix/self-hosting) UI at
<http://127.0.0.1:6006> for model, tool and agent traces. The pinned nonroot service
is defined in [phoenix.compose.yaml](../../containers/phoenix.compose.yaml). Its
SQLite database persists in `civ-local-phoenix-data`; restarting or recreating
the container preserves traces. No account or hosted telemetry project is needed.

Create the dedicated resources once, then start the service with a Compose
provider available to Podman:

```sh
podman network create --internal civ-local-telemetry
podman volume create civ-local-phoenix-data
podman compose -f containers/phoenix.compose.yaml up -d
```

The external network must be internal. Only model workers and Phoenix join it;
the launcher does not attach the game host, coordinators or credential brokers.
Only the UI/OTLP HTTP port is published, bound to `127.0.0.1`. Phoenix's external
resources, usage telemetry, automatic pricing downloads, assistant, MCP and
provider integrations are disabled. The collector receives no model credentials.

Tracing remains disabled unless selected explicitly. Add this to a fresh game's
configuration to send model-worker traces to the local collector:

```toml
[runtime.tracing]
local = true
endpoint = "http://phoenix:6006/v1/traces"
```

Local mode accepts no token or ambient OTLP header configuration, and its HTTP
client ignores proxy/netrc settings.
An authenticated HTTPS collector remains supported through explicit references:

```toml
[runtime.tracing]
token_file = "/absolute/private/path/logfire.token"
endpoint = "https://logfire-us.pydantic.dev/v1/traces"
```

In HTTPS mode the launcher mounts the token read-only in model workers. It is not included in
workflow inputs, coordinator/JVM mounts or exported histories. Worker startup
configures Pydantic AI instrumentation before constructing the Temporal worker;
shutdown flushes the exporter after admitted work settles.

The maintained OpenInference integration classifies Pydantic AI spans for the
Phoenix UI. The final export boundary replaces names and resources with constants
and retains only fixed span-kind labels, allowlisted numeric usage counters,
timing, status and trace linkage. Prompts,
completions, tool content, events, paths, raw errors and arbitrary attributes are
removed. Exporter failure does not determine gameplay outcomes. Host accounting
continues to own usage totals. Content-free traces show execution and usage;
private native SDK histories retain the evidence needed to assess model reasoning.

The local setup runs without hosted authentication. Use host accounting for
reasoning tokens, model names, and complete usage totals.

## Storage and backups

The runtime reads the database format. Older file-based matches require the older
code; there is no import or migration command. Start a fresh match with this version.

Backups must include committed SQLite data and any live WAL. For a running database,
use SQLite's backup API; copying only the main file can omit recent records.
