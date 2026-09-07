# Player protocol

The current interface is a turn-management slice, not a complete player API.
Use `player_capabilities` to inspect its operations and recovery guarantees.
Authentication fixes the player's civilization; tool arguments cannot override it.

## Tools

- `player_capabilities`: supported commands, protocol version, incomplete-coverage
  flag, and whether durable recovery is configured.
- `player_turn`: phase, opaque epoch and revision, pending decisions, city handles
  and construction choices, research options, policy options, Great Person options,
  and informational alerts. Lists are detached from engine state.
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
| `ACKNOWLEDGE_ALERT` | `target`: handle of an acknowledgeable informational alert |
| `END_TURN` | None; `canEndTurn` must be true |

Unused fields must be absent or have their default value (`null` for target and
selection, `[]` for queue). Unknown fields and command names are rejected. Requests
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

## Launch and recovery

Generate the existing two-player fixtures with `./gradlew generateS01Scenarios`.
The MCP grant file is a JSON object with `players`, an array of entries containing
`civilizationId` and `token`. Tokens are distinct 43-character unpadded Base64URL
encodings of 32 random bytes. Keep this operator file outside player-accessible
storage with owner-only permissions (`chmod 600`).

```sh
./gradlew run --args="--save /absolute/game.json --data-dir /absolute/data --checkpoint /absolute/checkpoint.json --mcp-config /absolute/players.json"
```

Resume the same match and command history after restarting the process:

```sh
./gradlew run --args="--resume-checkpoint /absolute/checkpoint.json --data-dir /absolute/data --mcp-config /absolute/players.json"
```

The server prints its loopback Streamable HTTP endpoint. The default port is 8765;
`--port 0` selects an available port. Clients use their bearer token at `/mcp`.

Checkpointing writes engine state and receipts into one temporary file, flushes it,
atomically replaces the checkpoint, and flushes the directory before acknowledging
a command. Newly issued player references are also persisted before a query returns.
The checkpoint and adjacent `.lock` file are operator artifacts; an exclusive lock
prevents another host from opening the same checkpoint. Atomic replacement is
required. Existing checkpoints require explicit resume rather than overwrite.

Persistence or mutation failure halts player operations. Restore the checkpoint and
retry the original request: it either has a receipt or was not committed. Ordinary
operator pause/resume is available through `GameExecutor`; resume cannot clear a
mutation failure. Paused matches still support observations and successful retries.

## Limits of this implementation

There is no complete-match runner or agent filesystem isolation yet. The extracted
executor is independent of headless bootstrap, but the desktop has not been wired
to that executor. Selected desktop pickers now share engine validation; the rest of
the UI still has direct access paths. Turn snapshots intentionally cover only this
slice, including only a subset of alert details and construction controls. Consult
the coverage ledger for the remaining work.
