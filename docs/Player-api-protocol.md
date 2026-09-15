# Player protocol

The current interface covers turn management, exploration, settlement, basic city development,
first contact, war declarations, negotiated peace, basic land combat and empire economy.
It remains an incomplete player API.
Use `player_capabilities` to inspect its operations and recovery guarantees.
Authentication fixes the player's civilization; tool arguments cannot override it.

This page describes the host wire protocol. Agent sessions use the civilization
service's [compact overview and detail queries](Compact-observations.md); trusted
code supplies request IDs, credentials and revisions for their commands.

## Tools

- `player_capabilities`: supported commands, protocol version, incomplete-coverage
  flag, and whether durable recovery is configured.
- `player_turn`: phase, opaque epoch and revision, pending decisions, city handles
  and construction choices, research options, policy options, Great Person options,
  informational alerts, and `map` (explored tiles, frontier coordinates and owned units
  with current-turn move previews). Lists are detached from engine state.
  `rules` contains public ruleset, difficulty, speed, enabled `victoryTypes`, and
  `maxTurns` (null when no enabled victory uses a turn limit). The same public
  settings appear in the communication context.
  Owned cities include `development` (displayed yields/breakdowns, growth, worked
  tiles and indexed construction progress); worker units include `worker` (jobs,
  prerequisites and progress). `research` exposes displayed science and technology
  progress. Yield amounts are strings retaining the relevant UI display precision.
  `economy` adds own finances, maintenance, happiness, supply, resource balances
  and city connections. `cities[].purchases` and `map.units[].economy` expose gold
  purchases, upgrades, pillage and disband options, costs, benefits, requirements,
  unavailable reasons and confirmations; see [economy and unit actions](Economy-and-unit-actions.md).
  `cities[].management` adds focus/growth controls, specialist yields and slots,
  great-person progress, building sales and shared-tile transfers; see
  [city management](City-management.md).
  First-contact alerts include `firstContact`: `civilizationId`, `civilizationName`,
  `leaderName` (the displayed leader caption), `message`, and `acknowledgement`.
  Every supported informational alert also includes `content`: `title`, ordered
  `paragraphs`, nullable `quote`, and the `acknowledgement` label. This contains the
  full popup prose; technology descriptions use the observing civilization's context.
  `notifications` contains ordinary messages with full translated text, category,
  icons, event locations, short notice handles, and `canDismiss`. Read these before
  `DISMISS_NOTIFICATIONS` with `targets=[notice handles]`; the receipt retains the
  exact dismissed content. Messages are cleared from the API pending list while
  native notification history is preserved. They do not block end turn. See
  [ordinary notifications](Ordinary-notifications.md).
  `diplomacy.knownCivilizations` lists met, surviving major civilizations with their
  identity, leader caption, status, peace treaty duration, legal war/peace actions,
  unavailable reasons and confirmation warnings. Peace trade options and incoming/
  outgoing offers include permitted terms and full leader text; see [war and peace](War-and-peace.md).
  Unmet identities and hidden locations, plans and third-party requests are excluded.
- `player_command`: submit exactly one command using the latest turn snapshot.
- `player_result`: your victory/defeat result and pending combat reports, also available
  after scheduling stops. Combat fields, orders and capture choices are documented in
  [basic combat](Combat.md).
- Existing summary and prototype tools remain available during migration. Their
  mutation tools are disabled when durable checkpoints are enabled; use
  `player_command` in that mode.

Example, with values copied from `player_turn`:

```json
{
  "requestId": "t17a1",
  "epoch": "<epoch>",
  "revision": "rev12",
  "kind": "SET_RESEARCH_QUEUE",
  "queue": ["Pottery", "Writing"]
}
```

Player references use separate, increasing counters: `city1`, `unit1`, `alert1`,
`offer1`, `decision1`, and `report1`. They are scoped to the authenticated player
and game epoch. For example, Rome's `city1` and Greece's `city1` refer to their own
cities. Both city queries return the same handles. Names can change without
changing a handle; removed objects' numbers are never recycled.

Revisions use `rev1`, `rev2`, etc.; the current turn review uses the same ID.
Copy identifiers from the latest observation instead of predicting values. The
game epoch is a compact random identifier beginning with `g`, separating counters
in independent games. Credentials are separate from these references.

Checkpoint format 2 persists handles, counters and report identities. Restart
preserves them. Rollback restores game state while retaining every player's
highest issued counters, so discarded handles and revisions cannot identify new
objects or authorize stale commands. Older checkpoint formats require a new game.

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
| `ATTACK` | `target`: owned unit; `destination` and `attackFrom`: exact pair from `combat.attacks` |
| `CITY_ATTACK` | `target`: owned city; `destination`: offered city attack |
| `SET_UNIT_ORDER` | `target`: owned unit; `selection`: available `combat.orders` name |
| `PROMOTE_UNIT` | `target`: owned unit; `selection`: available `combat.promotions.choices` name |
| `RESOLVE_CAPTURED_CITY` | `target`: capture alert; `selection`: available capture option after reading the current decision (also handles civilian return) |
| `MANAGE_CAPTURED_CITY` | `target`: owned city; `selection`: available `captureOptions` name after reading current options |
| `BUY_CONSTRUCTION` | `target`: owned city; `selection`: available name from `purchases.constructions` |
| `BUY_TILE` | `target`: owned city; `destination`: available position from `purchases.tiles` |
| `UPGRADE_UNIT` | `target`: owned land unit; `selection`: available `targetName` from `economy.upgrades` |
| `PILLAGE`, `DISBAND_UNIT` | `target`: owned land unit with the corresponding available `economy` option |
| `FOUND_CITY` | `target`: settler handle; `confirmBreakPromise: true` only when accepting its listed settlement promises |
| `START_WORKER_JOB` | `target`: worker handle; `selection`: available name from `worker.options` |
| `STOP_WORKER_JOB` | `target`: worker handle; `worker.canStop` must be true |
| `SET_WORKED_TILE` | `target`: city handle; `destination`: listed tile coordinate; `worked`: boolean matching its offered action |
| `SET_TILE_LOCKED` | `target`: city handle; `destination`: listed tile coordinate; `locked`: boolean matching its offered action |
| `SET_CITY_FOCUS` | `target`: city handle; `selection`: available `management.focusOptions` name |
| `SET_AVOID_GROWTH`, `SET_MANUAL_SPECIALISTS` | `target`: city handle; `enabled`: desired boolean setting |
| `ASSIGN_SPECIALIST`, `UNASSIGN_SPECIALIST` | `target`: city handle; `selection`: `management.specialists` name with the corresponding available action; changes one citizen |
| `RESET_CITIZENS` | `target`: city handle; clears tile locks and reassigns citizens |
| `SELL_BUILDING` | `target`: city handle; `selection`: available `management.buildingSales` name; `confirmAction: true` after reading the confirmation |
| `REASSIGN_CITY_TILE` | `target`: receiving city handle; `destination`: available `management.sharedTiles` position; requires a free citizen |
| `REMOVE_CONSTRUCTION` | `target`: city handle; `index`: removable construction entry |
| `MOVE_CONSTRUCTION` | `target`: city handle; `index`: construction entry; `toIndex`: one of its `moveToIndices` |
| `DECLARE_WAR` | `target`: known civilizationId; `confirmWar: true` after reading current confirmation |
| `END_TURN` | `reviewId`: current `turnReview.id`; `canEndTurn` must be true. If any review units are idle with legal destinations, include `deferReason` explaining intentional idling. |

Peace proposals and decisions use the same asynchronous `trade_*` tools as economic
trades, described in [Asynchronous-trade.md](Asynchronous-trade.md). They are not
turn commands.

Unused fields must be absent or have their default value (`null` for target and
selection, destination, attackFrom, worked, locked, enabled, index and toIndex; `[]` for queue,
`false` for confirmBreakPromise, confirmWar and confirmAction). Unknown fields and command names are rejected. Requests
must use a unique printable ASCII `requestId` of 1–128 characters per player. Retries
use the same ID **and the original payload**, including the original revision.

Successful retries return the original receipt, including after the player's turn
ends. Reusing an ID for different content fails. A new request using an old
observation fails with `STALE_REVISION`; observe again and submit a new request.
Revision tokens change with the returned player state or a successful command,
not a global count of opponent actions. Reloading an ordinary save starts a new
epoch; restoring a durable checkpoint preserves the epoch and receipts.

Gold purchases, upgrades, pillage and disband require a fresh `player_turn` read
at the submitted revision. Read costs and full confirmation text, and include
`confirmAction: true` when the option's `confirmation` is non-null. Read
`receipt.economy` for actual gold and unit changes, then re-observe for current
resource balances, maintenance and city state.

City management commands also require a fresh `player_turn`. Building sales require
the full confirmation and return both economy and city management outcomes.
`receipt.cityManagement` records actual settings, specialists, worked/locked tiles,
yields, removed buildings and gold change for affected own cities. Shared-tile
transfers preserve ownership and clear the donor city's tile lock.

`player_turn` puts `turnReview` near the beginning of the response. It contains
confirmed commands from this turn and each owned unit's remaining movement,
position, idle flag and legal destination count. Movement notes report actual
endpoints, including when discovery shortened a move. Failed commands and receipt
replays add no actions. `previousTurnReview` carries the previous completed turn's
confirmed actions and deliberately deferred units with the player's reason.

`END_TURN` checks shared engine picker requirements and additional unimplemented
alerts/trades, then requires a current review acknowledgment. Fetch and read
`player_turn`, reconsider any unused movement, and send `reviewId=turnReview.id`.
If any review units have `idle=true` and `legalDestinations>0`, include a nonblank
`deferReason` of at most 500 characters. `TURN_REVIEW_REQUIRED` rejects a missing
or mismatched acknowledgment; `OBSERVATION_REQUIRED` rejects an unread review.
Every successful mutation invalidates the prior review. Reviews and deferrals
persist in checkpoints, while a restart requires a fresh read before an uncommitted
end turn. Exact retries of committed end turns still return their original receipt.
The interface permits leaving idle units idle, as the UI does with
unit cycling disabled. It does not silently run a major civilization's AI or resolve
mandatory choices. Decisions whose implementation is missing have `supported=false`.

Close-start games also report `scenario.name=CLOSE_START_DIPLOMACY` and phase
`AWAITING_WAR`, `AT_WAR` or `COMPLETE`. Completion requires a recorded successful
war declaration between the two known civilizations followed by restored peace;
first contact alone is insufficient. This operator-created exercise is absent
from ordinary games and changes no engine legality rules.

First contact has one acknowledgement, matching the desktop popup. Read its
introduction, then submit `ACKNOWLEDGE_ALERT` using that alert's handle and the latest
epoch/revision. Meeting effects already happened; acknowledgement removes only the
message and does not change war/peace status. Each player acknowledges their own
message during their own turn. Paused and waiting snapshots retain its content but
set `canAcknowledge=false`. Mixed alert queues can contain both acknowledgeable
information and unsupported choices; acknowledging the former cannot clear the latter.
War declarations and peace offers use explicit commands as described in [war and peace](War-and-peace.md).
Other diplomatic systems remain incomplete.

For supported informational popups, war confirmation and incoming-offer responses, a revision returned only by a command
receipt is insufficient to acknowledge: `OBSERVATION_REQUIRED` asks the player
to fetch and read `player_turn` first. A changed popup description invalidates
the prior revision. After process restart, pending acknowledgements require a
fresh read; exact retries of committed acknowledgements retain their usual replay
semantics. See [informational popups](Informational-popups.md) for content and
agent reading requirements.

## Launch and recovery

Create a fresh Ancient-era Vanilla game with Rome and Greece in human slots:

```sh
uv run civ-agent host start --state-dir /absolute/match --new
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

Scheduled play also persists a restore point at the start of each player's turn.
When the supervisor resumes an interrupted attempt, it first checks the committed
handoff counter. A completed `END_TURN` is retained even if its response was lost.
A committed terminal victory is also retained, even without a handoff; its final
commands are archived and each player receives its own result over HTTPS.
Otherwise the authenticated operator `rollback` operation restores the full
turn-start game and receipt state, rotates stale-state revisions and revokes old
temporary credentials. Commands from the discarded attempt are no longer effects
of the current game, even if they returned successful receipts before interruption.
The match epoch stays the same. The next attempt uses a fresh conversation, while
failed-attempt logs remain available for inspection and outside turn-summary memory.
Rollback requires the current lease, epoch, server instance, handoff, active
civilization and activation ID. Its durable attempt binding prevents delayed retries
from resetting a newer attempt; it is not a player API operation. A `HALTED` host rejects rollback:
restart it from the durable checkpoint before resuming the supervisor, so an
uncertain checkpoint write cannot cause a committed handoff to be undone.
See [automatic recovery](Automatic-turns.md#pause-inspect-and-resume).

## Limits of this implementation

The bounded scheduler does not establish complete-match coverage or agent filesystem isolation. The extracted
executor is independent of headless bootstrap, but the desktop has not been wired
to that executor. Selected desktop pickers now share engine validation; the rest of
the UI still has direct access paths. Turn snapshots intentionally cover only this
slice, including only a subset of alert details and construction controls. Movement
commands attempt one turn of an advertised path; they do not schedule future movement.
Naval combat, aircraft relocation, paradrops, air/nuclear attacks and other special actions are absent.
Worker automation, route-to-destination and non-Great-Person instant improvements are also absent.
Citizen specialists, focus changes, tile purchases and construction purchases remain outstanding.
Explored terrain and ownership follow the map UI's current data; improvement names
use Unciv's remembered values. This is not a complete historical map snapshot. Consult
the coverage ledger for the remaining work.

## Great People and on-demand research/policy details

`ACTIVATE_GREAT_PERSON` takes an owned unit `target` and an offered
`map.units[].greatPerson.abilities[].name` as `selection`, following a fresh
`player_turn` read. Effects happen at the current tile and can consume the unit.
Read `receipt.greatPerson` and new alerts/choices. `economy.goldenAge` reports
remaining turns and happiness progress.

`research_details` and `policy_details` accept optional `names` arrays (up to 16
unique exact rule names). Omit names for a catalogue without long descriptions;
request specific entries for desktop descriptions and, for research, the complete
ordered `prerequisiteQueue`. Details do not acknowledge unit abilities or popup
content, and do not mutate game state. See [Great People](Great-people.md) for
semantics, current limits and the prepared exercise.
