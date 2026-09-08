# Shared player API implementation

This is the current design, agreed in September 2026. Earlier host code is a proof
of concept, not a compatibility constraint. Progress is recorded in
[the coverage ledger](Player-api-coverage.md); working smoke tests never imply full
gameplay coverage or a complete privacy boundary.

## Contract

Players receive every ordinary Unciv action and information surface as structured
queries and commands. Bulk inspection is allowed, but precision, visibility,
notifications, remembered information, and previews must match what a human could
inspect. Unciv mechanics are authoritative; suspected fairness defects are reviewed
and corrected, not copied blindly. Mods and general Civ V fidelity work are excluded.

The private host owns authenticated player sessions, detached filtered snapshots,
opaque references, command serialization, receipts, checkpoints, and orchestration.
Small reusable engine operations own gameplay rules that would otherwise be
duplicated from graphical UI callbacks. Migrate the desktop UI to these shared
operations and queries with each gameplay slice. Existing View classes are not a
proven privacy boundary: several expose mutable engine objects and unchecked data.

One process owns one authoritative game. Headless and desktop bootstrap must share
the same execution service. Major civilizations controlled by external agents use
human engine slots so built-in AI cannot silently take their turns. Operator control
(create, save, load, pause, credentials, diagnostics) is separate from player tools.

## Delivery sequence

1. Inventory UI actions and information, establish shared turn requirements, and
   implement explicit capabilities, session identity, stale-state protection, and
   serialized execution. Unknown actions and unsupported decisions fail closed.
2. Implement a Vanilla duel from an Ancient start: two major civilizations, Quick
   speed, small land map, no city-states, barbarians, ruins, or mods; domination and
   time victories. Include development, research, policies, diplomacy, combat, all
   reachable mandatory choices, and desktop UI integration for the same slice.
   Add automatic turn scheduling with the workers and city-development slice below,
   so multi-turn testing does not require manually prompting each player.
3. Add isolation to the local scheduled runner and support a desktop human alongside
   Codex. Preserve each player's completed-turn summaries, detailed command records
   and private notes. Start a fresh conversation for every new turn and every retry
   after a turn-start rollback. Keep failed attempts for debugging, outside turn
   memory. Use separate rootless
   containers and restricted networking; no host saves, source checkouts, unrelated
   tools, or other players' credentials or histories. Model choice and budgets are
   operator configuration, not game rules.
4. Persist game state and receipts consistently, including a turn-start restore
   point for scheduled players. On an agent failure, pause without choosing player
   actions. On resume, retain any committed handoff; otherwise restore the complete
   turn-start state and start a fresh conversation. A lost reply must not undo or
   repeat a committed turn. Unrecoverable host failures remain halted.
5. Complete Vanilla, then Gods & Kings, removing setup restrictions only when all
   reachable actions and information surfaces pass the coverage gates.

## Delivered slice: workers, city development, and automatic turns

The exploration and settlement slice, interactive player launchers, and desktop
snapshot inspection are available. The development slice adds worker/city controls
and automatic scheduling. See the coverage ledger for implemented details; the
acceptance criteria below remain broader than a passing scripted test.

Gameplay scope:

- City and tile observations: yields, worked tiles, population, growth, production
  progress, and research progress, with the precision and visibility of the UI.
- Worker jobs: legal improvements and roads, prerequisites, work progress and
  completion estimates, and validated orders through shared engine operations.
- Basic city management: citizen assignment, changing production, and editing
  construction queues.

Automatic scheduling scope:

- Add a trusted operator interface for turn status and pause/resume. The supervisor
  checks the host's active civilization after checkpoint commit and rechecks before
  dispatch. Handoffs follow authoritative state, never model prose, the round counter,
  or player-scoped revisions. Waiting does not consume model calls.
- Each match epoch/civilization keeps its own working
  directory, notes and token. Only the selected player's permitted observations enter
  a fresh conversation for each new game turn using concatenated prior completed
  summaries; operator status and other players' transcripts are not forwarded.
  Readable `agents/Rome/turn_1/` folders contain dense `commands.jsonl` and light
  `summary.md` records. Recovery restores an unfinished turn's starting checkpoint
  and starts a fresh thread. Rolled-back attempts remain archived for debugging and
  contribute neither action claims nor observations to the next turn's context.
  Their commands and full public reports stay in `turn_1/attempts/attempt_1/`;
  the top-level turn files contain only retained attempts. Summary report excerpts
  have a 200-word limit; full reports remain available on explicit inspection.
- One scheduler owns the match, with one agent activation at a time. Prevent old
  processes from surviving into another turn. A completed activation must reconcile
  with host status before the next one starts. If the agent returns while still owning
  the turn, report the incomplete turn and pause rather than relaunching without progress.
- Provide game-turn and invocation limits, per-activation timeouts, persisted model
  usage accounting, and operator stop/resume. Distinguish enforced limits from estimated
  token/cost budgets; do not promise a hard spending cap without enforcement support.
- Pause active agents and host mutation access on failure, unsupported decisions,
  required human confirmation, or exhausted limits. Do not choose substitute actions
  or automatically skip blocked turns. Stop on game completion and do not activate
  waiting, paused, or defeated players.
- Persist runner progress, attempt identities and finalized summaries. On restart,
  reconcile with the authoritative checkpoint before activating a player. A lost
  end-turn reply can coexist with a committed handoff: retain that completed turn
  and never synthesize a second end-turn command. If the handoff did not commit,
  restore the turn-start game and receipts, rotate stale-state revisions, revoke
  temporary credentials and mark the discarded attempt as rolled back. This
  operator-only recovery retains the match epoch and consumed budgets. Missing or
  inconsistent restore state must stop recovery rather than guess. A `HALTED` host
  must restart from its durable checkpoint before rollback, preserving the disk's
  authority when a checkpoint write had an uncertain outcome.

All network communication uses authenticated, certificate-verified HTTPS: player
MCP, operator control, and per-civilization agent-service control. Each service uses
a private Unix socket for its internal Codex app-server connection, with no stdio
protocol or plaintext network listener. Remote endpoints and CA trust can be
configured without giving the agents access to the host filesystem.

This first scheduler supports trusted development sessions. Filesystem and
network isolation remain an explicit later milestone; separate directories alone
do not establish a privacy boundary. Scheduling and initial budget controls belong
to this slice even while the isolated runner is outstanding.

Acceptance: two agents develop a fresh duel over a configured short run (for example,
20 game turns), automatically handing off turns, building workers, improving tiles,
growing cities, and managing production. Pause and restart partway through, verify
that incomplete turns return to their starting state, start fresh conversations
with completed-turn memory, and inspect the resulting position with `inspect_game`.
Scripted CI clients must also exercise wrong-turn activation, duplicate supervisors,
timeouts, incomplete turns, unsupported choices, budget exhaustion, and interruption
on both sides of an accepted handoff. A live multi-turn model run complements these
checks; it does not establish a completed duel or full UI/API parity.

The September 2026 live run reportedly progressed until a first-contact popup
required an unsupported acknowledgement. The development fixture builds cities
and improves tiles without directing military units to meet the opponent; its
twenty-round result did not cover that encounter.

## Delivered slice: first contact and the diplomacy foundation

This slice addresses that blocker without changing the Vanilla duel's setup
restrictions. The [encounter inventory](Encounter-and-diplomacy.md) records all
30 alert types, their desktop text and effects, setup prerequisites and remaining
command/test gaps. The subsequent war/peace slice is described below; broader diplomacy remains outstanding.

- `FirstContact` exposes the encountered civilization's identity, displayed leader
  caption, introduction and acknowledgement label. `ACKNOWLEDGE_ALERT` and the
  desktop popup use shared engine operations and text. Meeting effects already
  happen before the popup; acknowledgement removes only the bound player's message.
- `player_turn.diplomacy.knownCivilizations` lists met, surviving major civilizations,
  their displayed identity and `atWar` status. Its detached fields follow the desktop
  diplomacy list; it contains no hidden rival positions, orders or private decisions.
- Informational alerts advertise acknowledgement only while the player is active.
  Paused/waiting players can still read introductions. Unsupported choices remain
  visible and blocking, including mixed queues with supported informational alerts.
- Agent instructions describe reading introductions and explicitly acknowledging
  supported alerts through the existing HTTPS player API. The scheduler does not
  clear alerts or choose diplomatic responses on a player's behalf.

Verification uses engine `PlayerFirstContactTest` and `PlayerOperationsTest`, host
`DiplomacyIntegrationTest`, and `EncounterRunIntegrationTest`. The encounter fixture
uses legal advertised exploration to create first contact, restarts with alerts
pending, acknowledges Rome, restarts again and retries the exact acknowledgement,
then lets both players complete four rounds through scheduled HTTPS commands.
The separate development regression still covers workers, improvements and growth.
Authority/privacy checks cover wrong-player, wrong-turn, paused, stale and unknown
handles; detached UI text; hidden opponent state; and mixed supported/unsupported
alerts. Acknowledgement must never repeat diplomacy or city-state meeting effects.

The next live acceptance run will use a new save, as requested. Compatibility with
older checkpoint formats is outside this slice; recovery and exact retries within
this version are required. The operator will rerun the agents separately and
report any next unsupported decision. Automated scenarios do not establish a
completed live duel or full diplomacy coverage.

## Delivered slice: ordinary informational popup content

The [informational-popup slice](Informational-popups.md) exposes full desktop text
for opening introductions, technology discoveries, completed wonders and Golden
Ages, alongside existing first-contact messages. Technology descriptions use the
observing civilization's context. Desktop handlers and player observations share
the same engine query; acknowledgement removes only the message.

The player must fetch `player_turn` at the current revision before acknowledging;
command receipt revisions alone are insufficient. Agent prompts require reading
the content and making a brief public note before dismissal. The host records
the query, while the transcript provides reviewable evidence of the agent's use
of the message. Same-version restart and receipt replay remain covered.

Diplomacy/trade/war and other system choices retain their own slices. Victory and
defeat require result delivery after scheduling stops; they remain separate from
ordinary in-game informational popups. No generic ruleset-event dismissal is added.

## Delivered slice: war and negotiated peace

The [war and peace slice](War-and-peace.md) adds shared engine diplomatic
availability and confirmation, war declaration and message acknowledgement, and
peace negotiation through treaties, gold, GPT and tradable resources. Player
observations include full incoming messages/terms and accept/reject/counteroffer
actions. Outgoing proposals can be retracted. Exact retries and pending offers
survive checkpoints; a scripted HTTPS duel verifies scheduled turns across war,
cooldown, peace negotiations and process restarts.

Gameplay launchers and the automatic runner now accept model and reasoning-level
overrides and preserve resolved player settings across fresh turns and resumes.

Continue the duel sequence with basic melee/ranged combat, promotions and
capture decisions. Full trade, remaining city/unit controls, embedded human play
and isolation retain their places in the broader delivery sequence. Add each
action together with the observations and resulting decisions needed to use it.

## Verification gates

Each slice needs engine behavior tests, host authority and privacy tests, transport
tests, and UI/API equivalence checks. Paired worlds differing only in hidden state
must yield equivalent permitted responses, options, errors, and previews. Check
ownership loss, hidden targets, precision, remembered state, stale decisions,
concurrent requests, turn transitions, elimination, retries, and crash recovery.

Regular CI uses scripted clients and fixtures. Live model matches are separate:
require a completed Codex duel and a human-versus-Codex acceptance run. Neither is
a substitute for the full inventory. Full-state logs belong to the trusted operator;
player transcripts contain only permitted observations.

Engine changes remain independently reviewable and contain no private project
context. Publish engine commits before updating the host's dependency pin; validate
with code and assets from the same selected checkout.
