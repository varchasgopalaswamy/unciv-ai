# Player protocol

The current interface covers turn management, exploration, settlement, basic city development,
first contact, war declarations, negotiated peace, land/naval/air combat and empire economy.
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
  `civilizationProfile` contains the bound civilization's ruleset ability and
  unique units/buildings/improvements, including unreached technology unlocks, inherent
  promotion effects, base-stat/replacement comparisons and obsolescence. It uses
  public rules and own research, without querying foreign state or treating
  technology readiness as city-specific buildability. See the
  [compact projection](Compact-observations.md#opportunities-at-turn-start).
  Owned cities include `development` (displayed yields/breakdowns, growth, worked
  tiles and indexed construction progress); worker units include `worker` (jobs,
  prerequisites and progress). `research` exposes displayed science and technology
  progress. Yield amounts are strings retaining the relevant UI display precision.
  City development includes border culture/cost/ETA and the next explored expansion
  tile, resistance and celebration timers, demanded luxury and razing status.
  Owned units include temporary `statuses` and `controls` for naming, promotion
  templates, swaps and road orders. City management includes saved default promotions.
  `turnControls` advertises native choice deferral, manual automated-unit movement
  and the available autoplay scopes.
  `economy` adds own finances, maintenance, happiness, supply, resource balances
  and city connections. `cities[].purchases` and `map.units[].economy` expose gold
  purchases, upgrades, pillage and disband options, costs, benefits, requirements,
  unavailable reasons and confirmations; see [economy and unit actions](Economy-and-unit-actions.md).
  `cities[].management` adds focus/growth controls, specialist yields and slots,
  great-person progress, building sales and shared-tile transfers; see
  [city management](City-management.md).
  `map.units[].combat.promotions` contains the native promotion tree plus
  `retainedAfterSelection`, `prerequisiteUnlocks`, and `afterSelection` XP
  accounting for currently legal choices. It also reports total XP, the native
  barbarian XP limit/eligibility, and `recovery` for resting without a promotion.
  See [promotion comparisons](Compact-observations.md#opportunities-at-turn-start)
  for estimate assumptions and the compact model-facing projection.
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

  Binding trade proposals also support `WarDeclaration` terms against a mutually
  known major: `from` declares war on `item.name`. Own warnings accompany trade
  options and `trade_read`; the target receives a separate war event without the
  private proposal or payment. See [third-party war trades](Third-party-war-trades.md).
- `movement_preview`: inspect a native route for an owned `target` and `destination: {x, y}`. Returns adjacent paths grouped by estimated turns, movement costs and counts of unexplored tiles and tiles outside current sight. Read at the current revision before setting a multi-turn destination.
- `player_inspect`: bounded, on-demand native reference and overview information;
  see [inspection subjects](#inspection-subjects). A road inspection must precede
  `CONNECT_ROAD` at the same revision, for the same unit and destination.
- `player_command`: submit exactly one command using the latest turn snapshot.
- `player_result`: your victory/defeat result and pending combat reports, also available
  after scheduling stops. Combat fields, orders and capture choices are documented in
  [basic combat](Combat.md).
- `player_summary`, `owned_cities` and `free_great_person_choice` provide small
  native queries. Renaming and free Great Person selection use `player_command`
  with `RENAME_CITY` and `CHOOSE_FREE_GREAT_PERSON`; there are no separate mutation
  endpoints or receipt caches for these actions.

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

Checkpoint format 6 persists handles, counters and report identities. Restart
preserves them. Rollback restores game state while retaining every player's
highest issued counters, so discarded handles and revisions cannot identify new
objects or authorize stale commands. Older checkpoint formats require a new game.

| Command | Additional fields |
| --- | --- |
| `SET_RESEARCH_QUEUE` | `queue`: replaces the entire queue, including current research; nonempty names in prerequisite order |
| `CHOOSE_FREE_TECHNOLOGY` | `selection`: currently researchable technology |
| `ADOPT_POLICY` | `selection`: offered branch or policy |
| `DISMISS_POLICY_PICKER` | None; this does not spend or dismiss a free policy |
| `QUEUE_CONSTRUCTION` | `target`: city handle; `selection`: offered construction |
| `CHOOSE_FREE_GREAT_PERSON` | `selection`: offered unit name; placement may still fail |
| `RENAME_CITY` | `target`: city handle; `selection`: valid new name |
| `RENAME_UNIT` | `target`: owned unit; `selection`: new name, or an empty string to clear the custom name |
| `SAVE_UNIT_PROMOTIONS` | `target`: owned unit in an owned non-puppet city's territory; saves its current promotions as that city's template for the unit type |
| `SET_DEFAULT_PROMOTIONS` | `target`: city handle; `selection`: saved unit type; `enabled`: desired setting |
| `ACKNOWLEDGE_ALERT` | `target`: handle of an informational alert with `canAcknowledge=true`; fetch and read its full `content` in `player_turn` at the submitted revision first |
| `MOVE_UNIT` | `target`: owned unit handle; `destination`: `{ "x": ..., "y": ... }` copied from that unit's `moves` |
| `SET_UNIT_DESTINATION` | `target`: owned unit; `destination`: exact coordinate from a current available `movement_preview`; moves now and persists the native order |
| `CANCEL_UNIT_MOVEMENT` | `target`: owned unit with `movementOrder.canCancel=true`; no movement is spent |
| `SWAP_UNITS` | `target`: owned unit; `destination`: advertised `controls.swaps` position; `captureRisks` identifies each affected unit and its risk, including civilians swapped out of a formation's protection. Use `confirmAction: true` only after accepting those risks. |
| `CONNECT_ROAD` | `target`: owned worker; `destination`: exact coordinate from an available current `player_inspect(ROAD)`; starts the native persistent road/rail order |
| `CREATE_WATER_IMPROVEMENT` | `target`: owned Work Boat; `selection`: available `waterImprovement.name`; consumes the unit |
| `ATTACK` | `target`: owned unit; `destination` and `attackFrom`: exact pair from `combat.attacks` |
| `CITY_ATTACK` | `target`: owned city; `destination`: offered city attack |
| `SET_UNIT_ORDER` | `target`: owned unit; `selection`: available `combat.orders` name, including independent `SET_UP`, `GUARD` and `STOP_AUTOMATION` for road orders |
| `PROMOTE_UNIT` | `target`: owned unit; `selection`: available `combat.promotions.choices` name |
| `RESOLVE_CAPTURED_CITY` | `target`: capture alert; `selection`: available capture option after reading the current decision (also handles civilian return) |
| `MANAGE_CAPTURED_CITY` | `target`: owned city; `selection`: available `captureOptions` name after reading current options |
| `BUY_CONSTRUCTION` | `target`: owned city; `selection`: available name from `purchases.constructions` |
| `BUY_TILE` | `target`: owned city; `destination`: available position from `purchases.tiles` |
| `UPGRADE_UNIT` | `target`: owned land/naval/air unit; `selection`: available `targetName` from `economy.upgrades` |
| `GIFT_UNIT` | `target`: owned unit with an available `gift.action`; recipient and native influence/opinion effects appear in `gift` |
| `PILLAGE`, `DISBAND_UNIT` | `target`: owned land/naval/air unit with the corresponding available `economy` option |
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
| `MOVE_AUTOMATED_UNITS` | None; requires `turnControls.canMoveAutomatedUnits` |
| `AUTOPLAY` | `selection`: `ALL`, `MILITARY`, `CIVILIANS` or `ECONOMY`; performs native automation within the current turn |
| `END_TURN` | `reviewId`: current `turnReview.id`; requires `canEndTurn`, or explicit `deferChoices: true` with `turnControls.canDeferChoices`. Modal decisions cannot be deferred. `deferReason` is optional audit context. |

Peace proposals and decisions use the same asynchronous `trade_*` tools as economic
trades, described in [Asynchronous-trade.md](Asynchronous-trade.md). They are not
turn commands.

Unused fields must be absent or have their default value (`null` for target and
selection, destination, attackFrom, worked, locked, enabled, index and toIndex; `[]` for queue,
`false` for confirmBreakPromise, confirmWar, confirmAction and deferChoices). Unknown fields and command names are rejected. Requests
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

Upgrades may relocate a unit, including to another terrain domain. The option
reports the destination when explored and requires its native relocation
confirmation. Unit gifts use the single `gift` field for both eligible majors and
city-states; `canGiftUnit` is the compact overview flag.

Autoplay does not advance the turn. Its receipt supplies the resulting state and
decisions. Repeated `AUTOPLAY` and `END_TURN` calls compose continuous autoplay.
`MOVE_AUTOMATED_UNITS` respects the native move-at-turn-start setting and
once-per-turn flag.

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
Every successful command receipt also contains `actionSummary`, the same factual
description used by the review. Command acceptance does not guarantee the
requested attack or destination was reached: the summary uses observed results.

`END_TURN` checks shared engine picker requirements and additional unimplemented
alerts/trades, then requires a current review acknowledgment. Fetch and read
`player_turn`, reconsider any unused movement, and send `reviewId=turnReview.id`.
Idle units and tactical warnings do not block the turn. An optional
`deferReason` may contain at most 500 characters. `TURN_REVIEW_REQUIRED` rejects a missing
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

## Inspection subjects

`player_inspect` returns `epoch`, `revision`, `turn`, `subject` and a detached
`data` object. Copy references from the current observation. Lists use `offset`
and `limit` (default 50, maximum 100), with `total` and `nextOffset` in the result.

| `subject` | Arguments and content |
| --- | --- |
| `CIVILOPEDIA` | Optional `category` and `search` list public ruleset entries. `names` (at most 16 exact names or reference links) includes their generated mechanical descriptions. Ruleset flavor text and quotations are omitted. |
| `POLITICS` | Met civilizations' eras, adopted policy-branch counts and disclosed third-party relationships. Unknown endpoints are omitted. |
| `WONDERS` | Native built/known/unlocated wonder status. Owners, city names and coordinates are included only when permitted. |
| `NOTIFICATIONS` | Retained native notification history, optionally filtered by `turn`. These are historical records, without dismissal commands or arbitrary UI actions. |
| `REPLAY` | Territorial state at `turn` (default current), paged over the currently explored map. Available from turn 5 or game end; undisclosed owners are unnamed. |
| `TILE` | `destination` in the explored map; current yields and their sources, defense and fresh water require current sight. Optional `target` is an own city that can work the tile. |
| `UNIT` | Either an owned `target` or a currently visible `destination`; mechanical unit reference, health, strengths, promotions and temporary effects. No foreign orders or private XP. |
| `ROAD` | Owned worker `target` and explored `destination`; native road/rail path and visible per-tile jobs. Full-route worker-turns and maintenance/connection effects require current sight of the whole route. |

For example, a Civilopedia index search followed by a selected entry:

```json
{"subject":"CIVILOPEDIA","search":"Worker","limit":10}
{"subject":"CIVILOPEDIA","category":"Unit","names":["Worker"]}
```

These are public ruleset references, not evidence that a civilization, unit or
resource exists in this match. The model service provides copyable action
arguments for offered controls, including road construction after inspection.

Road worker-turns exclude travel and interruptions. The completion comparison
assumes the full route is built now, with current cities and policies; native
automation may stop or change its route. Building `completion.oneTime` similarly
reports native completion grants separately from passive yield changes: stored
yields, free choices/units/buildings, population and Golden Age turns. Estimates
run in a detached game. In the compact overview, wonder estimates stay under
`wonderOpportunities`.

Diplomatic marriage receipts report whether the city-state was eliminated and
which city handles were gained; post-action influence is null if it no longer
exists. Each transferred city receives its native annex/puppet/destroy choice
through the capture interface, including after recovery.

## Player chat

`player_communication` accepts a `request` with `op="send"`, the current `epoch`,
a stable `requestId`, and `text`. Omit both addressing fields for public chat;
use `to` for one recipient or `recipients` for a private participant group:

```json
{"op":"send","epoch":"<current epoch>","requestId":"group-1","recipients":["Greece","France"],"text":"Shall we discuss our borders?"}
```

`recipients` must be nonempty, unique, and contain eligible ordinary chat players
other than the sender. It cannot accompany `to` or `nativeAction`. Built-in AI
does not receive ordinary chat. Invalid addressing rejects the request before
any delivery or native effect; it never becomes public chat.

Private messages store the sender and recipients in `participants` and set
`public=false`. Conversation identity is the sorted, exact participant set:
recipient order does not matter, and `recipients:["Greece"]` identifies the same
conversation as `to:"Greece"`. A changed audience starts a different conversation
without sharing earlier history. Public chat is distinct even when a private
audience includes every eligible player.

To reply to all, copy the received `participants` minus your own civilization into
`recipients`. France replying to the message above would send:

```json
{"op":"send","epoch":"<current epoch>","requestId":"group-reply-1","recipients":["Greece","Rome"],"text":"Yes, let us compare proposals."}
```

Only participants receive private content, and every recipient except the sender
is notified. Optional `mentions` must belong to the audience; they do not narrow
private delivery or notifications. Public messages notify only mentioned players.
Receipts, checkpoint restart and rewind use the existing communication transaction.
During strategic review, human text is queued privately to its author and released
in order when reviews finish. Trusted operator access is unchanged.

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
uses a separate credential at `/operator`; see [automatic turns](../guides/Automatic-turns.md).

Checkpointing writes engine state and receipts into one temporary file, flushes it,
atomically replaces the checkpoint, and flushes the directory before acknowledging
a command. Newly issued player references are also persisted before a query returns.
Unit handles are also preserved in checkpoints; raw unit IDs never enter player responses.
Version 8 stores durable player knowledge (including recent barbarian sightings), identifiers and receipts, but no derived
turn overview or observation revision. Loading rebuilds observations with fresh
revisions. Earlier checkpoint formats require a new game; there is no migration.
The checkpoint and adjacent `.lock` file are operator artifacts; an exclusive lock
prevents another host from opening the same checkpoint. Atomic replacement is
required. Existing checkpoints require explicit resume rather than overwrite.

Persistence or mutation failure halts player operations. Restore the checkpoint and
retry the original request: a committed command replays its saved receipt unchanged.
An uncommitted command needs a fresh observation and revision before resubmission.
Ordinary
operator pause/resume is available through `/operator` and `GameExecutor`; resume cannot clear a
mutation failure. Paused matches still support observations and successful retries.

Scheduled play also persists a restore point at the start of each player's turn.
When the supervisor resumes an interrupted attempt, it first checks the committed
handoff counter. A completed `END_TURN` is retained even if its response was lost.
A committed terminal victory is also retained, even without a handoff; its final
commands are archived and each player receives its own result over HTTPS.
Otherwise the scheduler restores the coordinated start-of-turn boundary through
operator `barrier`, `snapshot`, `restore`, `rotate_players` and `restore_complete`
operations and the corresponding agent-service endpoints. The game, communication
and private state rewind together under a new epoch. Commands from the discarded
attempt no longer have effects, even if they previously returned receipts.

The native host rejects a boundary whose handoff has already committed and never
rewinds a terminal victory. A `HALTED` host must restart from disk before recovery.
A restore journal keeps a partially restored match fenced until explicit resume
finishes it. Historical round checkpoints and the host-only rollback endpoint have
been removed. See [turn recovery](../architecture/Turn-recovery.md).
See [automatic recovery](../guides/Automatic-turns.md#pause-inspect-and-resume).

## Limits of this implementation

The bounded scheduler does not establish complete-match coverage or agent filesystem isolation. The extracted
executor is independent of headless bootstrap, but the desktop has not been wired
to that executor. Selected desktop pickers now share engine validation; the rest of
the UI still has direct access paths. Turn snapshots intentionally cover only this
slice, including only a subset of alert details and construction controls. `MOVE_UNIT` attempts a current-turn path; `SET_UNIT_DESTINATION` retains a native
order for later turns. Aircraft relocation, paradrops and special combat use the
mission operations below. Worker automation, road-connection orders and supported
Great Person improvements use native operations. Citizen specialists, focus changes, tile
purchases and Gold construction purchases are supported; other currencies and
full city controls remain incomplete.
Explored terrain, ownership and infrastructure use current or last-seen facts,
including discoveries during intermediate movement steps. Technology can reveal
resources in explored fog without refreshing hidden mutable facts. Improvement
names also retain Unciv's permitted remembered values. Loaded saves can contain
explored tiles whose mutable facts the host has never observed. Consult
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

Research details also return `researchQueue`, the existing queue.
`SET_RESEARCH_QUEUE` makes the first entry active and preserves science already
invested in other technologies. To finish Mining before Calendar, supply
`["Mining", "Calendar"]`; `["Calendar"]` switches immediately. Named gateway
offers label their replacement `command` and, when distinct, provide an
`appendToQueue.command` that retains the entire existing queue and adds missing
prerequisites and the selected technology. Engine legality is checked on execution.
The gateway's successful response adds `researchChange.current` and, when an
unfinished technology was displaced, `researchChange.paused` with its name and
actual retained science. This uses the usual internal state refresh and requires
no extra model call.

See [naval gameplay and destination orders](Naval-gameplay.md) for continuation timing, route estimate limits, progress/cancellation and actual movement/improvement receipts.

## Air missions and special military actions

Read `unit.air` for supported missions/ranges, candidate city/carrier bases and
remaining slots, carried unit handles, transport state and automatic interception
readiness. Reuse construction, promotions, upgrade and disband operations for
ordinary aircraft. Native capacity and resources determine availability.

Call `mission_preview` with `target`, `mission` (`REBASE`, `PARADROP`, `AIR_SWEEP`,
`NUCLEAR_STRIKE`) and `destination: {x,y}`. Issue the matching command
(`REBASE_UNIT`, `PARADROP`, `AIR_SWEEP`, `NUCLEAR_STRIKE`) with that same owned unit
and destination after reading the current overview and preview. Do not send
`mission` or `attackFrom` in these commands. A nuclear launch additionally needs
`confirmAction:true` after reading its consequences and confirmation. A new
revision or restart requires a fresh preview; completed requests replay unchanged.

Nuclear previews list explored blast tiles, visible victims and known war
warnings from current or remembered ownership. Destination and blast visibility
fields distinguish unexplored terrain from areas outside current sight. Sweep and
interceptable nuclear previews list visible interception capabilities, without
private readiness or a predicted responding unit. Unseen victims can change
native legality or effects. `receipt.mission.resolution` distinguishes a completed
combat mission from an intercepted nuclear launch. Relocations have no combat
resolution. The receipt includes own unit movement/health/removal, new wars and
new player-filtered combat reports. It never enumerates hidden casualties.

Aircraft and guided missiles use existing `ATTACK` previews. These estimates
exclude interception; `receipt.combat.resolution`, actual damage and subsequent
combat reports describe what happened. Interception is automatic and does not
need a separate order. See [air gameplay](Air-gameplay.md) for scope and validation.
