# Shared player API implementation

This is the current design, agreed in September 2026. Earlier host code is a proof
of concept, not a compatibility constraint. Progress is recorded in
[the coverage ledger](Player-api-coverage.md); working smoke tests never imply full
gameplay coverage or a complete privacy boundary.

The [asynchronous chat, structured memory and emergency recovery slice](Async-chat-and-memory-plan.md)
is implemented. It replaces summary concatenation with selected memory, allows
concurrent computation behind serialized approval, and preserves confirmed work
on normal recovery. Whole-match rollback is an explicit operator action using
coordinated round checkpoints. See [current operation](Chat-memory-and-recovery.md).

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
   after an interruption. Keep failed attempts for debugging, outside turn
   memory. Use separate rootless
   containers and restricted networking; no host saves, source checkouts, unrelated
   tools, or other players' credentials or histories. Model choice and budgets are
   operator configuration, not game rules.
4. Persist game state, receipts, communication and memory consistently. Normal
   recovery preserves confirmed work and reconciles uncertain requests before new
   computation. Coordinate optional whole-match recovery at round boundaries;
   never mix restored and abandoned timelines. Unrecoverable host failures halt.
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
- Each civilization keeps private structured memory and readable turn summaries
  and receipts. Fresh jobs use selected notes and events; transcripts stay available
  on demand. The service owns credentials and request bookkeeping.
- One scheduler owns gameplay turn authority. Each civilization may also run a
  communication job concurrently; the inbox keeps receiving events while models
  compute. Only the active civilization can issue ordinary gameplay commands.
- Provide game-turn and invocation limits, per-activation timeouts, persisted model
  usage accounting, and operator stop/resume. Distinguish enforced limits from estimated
  token/cost budgets; do not promise a hard spending cap without enforcement support.
- Pause active agents and host mutation access on failure, unsupported decisions,
  required human confirmation, or exhausted limits. Do not choose substitute actions
  or automatically skip blocked turns. Stop on game completion and do not activate
  waiting, paused, or defeated players.
- Persist runner progress, job identities, confirmed actions, messages, notes and
  receipts. Normal recovery reconciles uncertain requests without rewinding game
  state. Explicit emergency recovery restores all participants from a complete
  round manifest, fences old epochs and preserves operator audit/usage accounting.

This first scheduler supports trusted development sessions. Filesystem and
network isolation remain an explicit later milestone; separate directories alone
do not establish a privacy boundary. Scheduling and initial budget controls belong
to this slice even while the isolated runner is outstanding.

Acceptance: two agents develop a fresh duel over a configured short run (for example,
20 game turns), automatically handing off turns, building workers, improving tiles,
growing cities, and managing production. Pause and restart partway through, verify
that incomplete turns retain confirmed work, start fresh conversations
with selected private memory, and inspect the resulting position with `civ-agent host inspect`.
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
defeat use the result delivery added by the [combat slice](Combat.md); they remain
separate from ordinary in-game informational popups. No generic ruleset-event dismissal is added.

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

The basic melee/ranged combat, promotion, capture and result-delivery slice is
implemented; its inventory and verification are recorded in [Combat.md](Combat.md). Full trade, remaining city/unit controls, embedded human play
and isolation retain their places in the broader delivery sequence. Add each
action together with the observations and resulting decisions needed to use it.

## Delivered slice: economy and ordinary unit actions

The [economy slice](Economy-and-unit-actions.md) adds own empire finances,
maintenance, happiness, resource balances and city connections, plus gold purchases
of units, buildings and visible city tiles. Land units can inspect and execute paid
upgrades, pillage and disband, with unavailable reasons, costs, benefits, required
confirmations and actual receipts. Corresponding ordinary desktop controls share
engine validation and execution.

Scripted HTTPS tests improve Iron, wait through scheduled turns, purchase
reinforcements, upgrade, pillage/heal and reduce upkeep by disbanding. They inspect
actual state across restarts, exact retries and interrupted-turn rollback. Paired
hidden-world checks cover the new observations.

The operator's `/tmp/civ-economy-1` live run was audited against native saves and
persisted receipts: Rome completed the resource, purchase, upgrade, pillage/heal and
disband/upkeep checks by turn 5, and scheduling reached turn 12 with 24 handoffs.
All 73 persisted command receipts matched committed service command records; three
rolled-back attempts were excluded. Greece also purchased and repaired its pillaged
Farm. The fixture only gave Rome the Iron, upgrade and raiding opportunities, so
this establishes Rome's objectives, not symmetric coverage or a completed duel.
The cities retained some economic inefficiencies; the exercise was not a strategy
quality benchmark. Visual desktop and human-versus-agent acceptance remain separate.

## Delivered slice: city management

The [city management slice](City-management.md) adds focus and Avoid Growth,
manual/automatic specialists and one-citizen assignment, specialist yields and
great-person progress, validated building sales and shared worked-tile transfers.
The desktop delegates to the same engine operations. Sales remove allocations whose
specialist slots disappear, and reading thresholds no longer mutates saved counters.

The prepared scenario gives both players two cities and the same controls to test.
Scripted HTTPS checks assert native allocations, yields, refunds, capped food,
population growth and accumulated great-person points across turns, rollback,
restarts and exact retries. A live-model run of this new scenario is still pending.
Full overview details, remaining unit actions, general trade, embedded human play
and isolation remain in the wider plan.

## Delivered slice: Great Person abilities and decision details

The [Great People slice](Great-people.md) adds shared execution-time validation for
Great Person abilities, detached effects and unavailable reasons, unit consumption
and durable receipts. The Vanilla exercise covers research and construction hurry,
Artist/General Golden Ages and all five non-religious Great Person improvements.
City-state trade missions remain outside the player API's supported setup.

On-demand research and policy queries provide catalogues and selected desktop
descriptions, prerequisites, costs, requirements, research paths and branch
completion effects. Full descriptions are not repeated in every turn snapshot.
The new exercise pairs immediate ability checks with ordinary specialist-generated
Scientists and Liberty's free Great Person choice, using native-state assertions,
scheduled HTTPS handoffs, process restarts, rollback and exact retries.

## Delivered slice: asynchronous economic trade

The [economic trade slice](Asynchronous-trade.md) adds host-owned, versioned
gold/GPT/luxury/strategic-resource proposals, counteroffers, acceptance, decline
and withdrawal. Both players see canonical clauses with explicit directions;
Python binds a simple decision to the exact version that job inspected. The host
validates and enacts economic deals during either player's gameplay turn without
changing turn ownership. Engine effects, consent, events and receipts share the
native checkpoint and coordinated rollback protocol.

The prepared `async-trade` scenario negotiates Silk for GPT plus immediate gold.
Scripted HTTPS tests inspect actual resources, balances, durations and handoffs,
including stale terms, private visibility, restarts and storage failures. See the
runbook for human trade commands and the remaining trade categories.

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
