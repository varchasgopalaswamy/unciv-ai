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
   Codex. Preserve each player's explicit conversation ID and private notes. Use separate rootless
   containers and restricted networking; no host saves, source checkouts, unrelated
   tools, or other players' credentials or histories. Model choice and budgets are
   operator configuration, not game rules.
4. Persist game state and receipts consistently. On an agent failure, pause and
   resume without choosing player actions. On uncertain mutation failure, halt and
   recover the last consistent checkpoint. A retry must not repeat a committed action.
5. Complete Vanilla, then Gods & Kings, removing setup restrictions only when all
   reachable actions and information surfaces pass the coverage gates.

## Current milestone: workers, city development, and automatic turns

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
- Each match epoch/civilization keeps its own explicit conversation ID, working
  directory, notes and token. Only the selected player's permitted observations enter
  its conversation; operator status and other players' transcripts are not forwarded.
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
- Persist runner progress and conversation IDs. On restart, reconcile with the
  authoritative checkpoint and command receipts before activating a player. A lost
  end-turn reply can coexist with a committed handoff: preserve exact request IDs and
  payloads for uncertain retries, and never synthesize a second end-turn command.
  Uncertain mutation failures retain the existing halt-and-recover contract.

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
growing cities, and managing production. Pause and restart partway through, preserve
each player's conversation, and verify the resulting position with `inspect_game`.
Scripted CI clients must also exercise wrong-turn activation, duplicate supervisors,
timeouts, incomplete turns, unsupported choices, budget exhaustion, and interruption
on both sides of an accepted handoff. A live multi-turn model run complements these
checks; it does not establish a completed duel or full UI/API parity.

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
