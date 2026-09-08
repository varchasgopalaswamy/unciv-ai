# Player protocol

The current interface covers turn management, exploration, settlement, basic city development,
first-contact acknowledgement and initial diplomacy observations.
It remains an incomplete player API.
Use `player_capabilities` to inspect its operations and recovery guarantees.
Authentication fixes the player's civilization; tool arguments cannot override it.

## Tools

- `player_capabilities`: supported commands, protocol version, incomplete-coverage
  flag, and whether durable recovery is configured.
- `player_turn`: phase, opaque epoch and revision, pending decisions, city handles
  and construction choices, research options, policy options, Great Person options,
  informational alerts, and `map` (explored tiles, frontier coordinates and owned units
  with current-turn move previews). Lists are detached from engine state.
  Owned cities include `development` (displayed yields/breakdowns, growth, worked
  tiles and indexed construction progress); worker units include `worker` (jobs,
  prerequisites and progress). `research` exposes displayed science and technology
  progress. Yield amounts are strings retaining the relevant UI display precision.
  First-contact alerts include `firstContact`: `civilizationId`, `civilizationName`,
  `leaderName` (the displayed leader caption), `message`, and `acknowledgement`.
  Every supported informational alert also includes `content`: `title`, ordered
  `paragraphs`, nullable `quote`, and the `acknowledgement` label. This contains the
  full popup prose; technology descriptions use the observing civilization's context.
  `diplomacy.knownCivilizations` lists met, surviving major civilizations with their
  identity, displayed leader caption and `atWar` flag. It excludes unmet civilizations
  and does not expose hidden locations, resources, plans or diplomatic modifiers.
- `player_command`: submit exactly one command using the latest turn snapshot.
- Existing summary and prototype tools remain available during migration. Their
  mutation tools are disabled when durable checkpoints are enabled; use
  `player_command` in that mode.

Example, with values copied from `player_turn`:

```json
{
  "requestId": "rome-turn-17-research-1",
  "epoch": "<epoch>",
  "revision": "<revision>",
  "kind": "SET_RESEARCH_QUEUE",
  "queue": ["Pottery", "Writing"]
}
```

| Command | Additional fields |
| --- | --- |
| `SET_RESEARCH_QUEUE` | `queue`: nonempty names in prerequisite order |
| `CHOOSE_FREE_TECHNOLOGY` | `selection`: currently researchable technology |
| `ADOPT_POLICY` | `selection`: offered branch or policy |
| `DISMISS_POLICY_PICKER` | None; this does not spend or dismiss a free policy |
| `QUEUE_CONSTRUCTION` | `target`: city handle; `selection`: offered construction |
| `CHOOSE_FREE_GREAT_PERSON` | `selection`: offered unit name; placement may still fail |
| `RENAME_CITY` | `target`: city handle; `selection`: valid new name |
| `ACKNOWLEDGE_ALERT` | `target`: handle of an informational alert with `canAcknowledge=true`; fetch and read its full `content` in `player_turn` at the submitted revision first |
| `MOVE_UNIT` | `target`: owned unit handle; `destination`: `{ "x": ..., "y": ... }` copied from that unit's `moves` |
| `FOUND_CITY` | `target`: settler handle; `confirmBreakPromise: true` only when accepting its listed settlement promises |
| `START_WORKER_JOB` | `target`: worker handle; `selection`: available name from `worker.options` |
| `STOP_WORKER_JOB` | `target`: worker handle; `worker.canStop` must be true |
| `SET_WORKED_TILE` | `target`: city handle; `destination`: listed tile coordinate; `worked`: boolean matching its offered action |
| `SET_TILE_LOCKED` | `target`: city handle; `destination`: listed tile coordinate; `locked`: boolean matching its offered action |
| `REMOVE_CONSTRUCTION` | `target`: city handle; `index`: removable construction entry |
| `MOVE_CONSTRUCTION` | `target`: city handle; `index`: construction entry; `toIndex`: one of its `moveToIndices` |
| `END_TURN` | None; `canEndTurn` must be true |

Unused fields must be absent or have their default value (`null` for target and
selection, destination, worked, locked, index and toIndex; `[]` for queue,
`false` for confirmBreakPromise). Unknown fields and command names are rejected. Requests
must use a unique printable ASCII `requestId` of 1–128 characters per player. Retries
use the same ID **and the original payload**, including the original revision.

Successful retries return the original receipt, including after the player's turn
ends. Reusing an ID for different content fails. A new request using an old
observation fails with `STALE_REVISION`; observe again and submit a new request.
Revision tokens change with the returned player state or a successful command,
not a global count of opponent actions. Reloading an ordinary save starts a new
epoch; restoring a durable checkpoint preserves the epoch and receipts.

`END_TURN` checks shared engine picker requirements and additional unimplemented
alerts/trades. The interface permits leaving idle units idle, as the UI does with
unit cycling disabled. It does not silently run a major civilization's AI or resolve
mandatory choices. Decisions whose implementation is missing have `supported=false`.

First contact has one acknowledgement, matching the desktop popup. Read its
introduction, then submit `ACKNOWLEDGE_ALERT` using that alert's handle and the latest
epoch/revision. Meeting effects already happened; acknowledgement removes only the
message and does not change war/peace status. Each player acknowledges their own
message during their own turn. Paused and waiting snapshots retain its content but
set `canAcknowledge=false`. Mixed alert queues can contain both acknowledgeable
information and unsupported choices; acknowledging the former cannot clear the latter.
War declarations, peace offers and other diplomatic commands remain unimplemented.

For all supported informational popups, a revision returned only by a command
receipt is insufficient to acknowledge: `OBSERVATION_REQUIRED` asks the player
to fetch and read `player_turn` first. A changed popup description invalidates
the prior revision. After process restart, pending acknowledgements require a
fresh read; exact retries of committed acknowledgements retain their usual replay
semantics. See [informational popups](Informational-popups.md) for content and
agent reading requirements.

## Launch and recovery

Create a fresh Ancient-era Vanilla game with Rome and Greece in human slots:

```sh
./scripts/launch_host --state-dir /absolute/match --new
```

This uses Quick speed, a Tiny Pangaea map, domination/time victories, no city-states,
barbarians, ruins or mods. The checkpoint path must be new. An optional `--seed <integer>`
sets the map seed; it does not fix every engine random choice. See the
[exploration guide](Exploration-and-settlement.md) for a copyable launch command
using the credentials from the earlier manual test.

Generate the existing two-player fixtures with `./gradlew generateS01Scenarios`.
The MCP grant file is a JSON object with `players`, an array of entries containing
`civilizationId` and `token`. Tokens are distinct 43-character unpadded Base64URL
encodings of 32 random bytes. Keep this operator file outside player-accessible
storage with owner-only permissions (`chmod 600`).

```sh
./gradlew run --args="--save /absolute/game.json --data-dir /absolute/data --checkpoint /absolute/checkpoint.json --mcp-config /absolute/players.json --tls-config /absolute/tls.json"
```

Resume the same match and command history after restarting the process:

```sh
./gradlew run --args="--resume-checkpoint /absolute/checkpoint.json --data-dir /absolute/data --mcp-config /absolute/players.json --tls-config /absolute/tls.json"
```

The server prints its HTTPS Streamable HTTP endpoint. Serving requires `--tls-config`;
the launcher generates a local CA and host certificate automatically. The default
port is 8765; the low-level CLI supports `--port 0` for an available port. Clients
verify the certificate and use their bearer token at `/mcp`. The operator interface
uses a separate credential at `/operator`; see [automatic turns](Automatic-turns.md).

Checkpointing writes engine state and receipts into one temporary file, flushes it,
atomically replaces the checkpoint, and flushes the directory before acknowledging
a command. Newly issued player references are also persisted before a query returns.
Unit handles are also preserved in checkpoints; raw unit IDs never enter player responses.
The checkpoint and adjacent `.lock` file are operator artifacts; an exclusive lock
prevents another host from opening the same checkpoint. Atomic replacement is
required. Existing checkpoints require explicit resume rather than overwrite.

Persistence or mutation failure halts player operations. Restore the checkpoint and
retry the original request: it either has a receipt or was not committed. Ordinary
operator pause/resume is available through `/operator` and `GameExecutor`; resume cannot clear a
mutation failure. Paused matches still support observations and successful retries.

## Limits of this implementation

The bounded scheduler does not establish complete-match coverage or agent filesystem isolation. The extracted
executor is independent of headless bootstrap, but the desktop has not been wired
to that executor. Selected desktop pickers now share engine validation; the rest of
the UI still has direct access paths. Turn snapshots intentionally cover only this
slice, including only a subset of alert details and construction controls. Movement
commands attempt one turn of an advertised path; they do not schedule future movement.
Aircraft relocation, paradrops, attacks and other special actions are absent.
Worker automation, route-to-destination and instant improvements are also absent.
Citizen specialists, focus changes, tile purchases and construction purchases remain outstanding.
Explored terrain and ownership follow the map UI's current data; improvement names
use Unciv's remembered values. This is not a complete historical map snapshot. Consult
the coverage ledger for the remaining work.
