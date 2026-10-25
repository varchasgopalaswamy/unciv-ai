# Military manager

Military is a third civilization-local role during wars with living major
civilizations or city-states. Barbarian encounters alone leave control with main.
Main and economy continue concurrently, and only the host issues native END_TURN
after every active role finishes and accepted actions settle.

## Control and lifecycle

Military controls combat units (including scouts), conventional missions, city
bombardment, promotions, upgrades, pillage and disbanding. Main keeps civilians,
war declarations, nuclear strikes, diplomatic gifts, captured-city disposition,
and military units assigned to escort duty. Main controls both the civilian and
its escort, including their independent moves. An escort assignment survives
unpairing and turn boundaries until main releases it or the unit disappears.
Release requires first unpairing any native formation.

The gateway checks all actors affected by swaps, escort formation movement and
carrier transport. Bulk automation is unavailable in managed turns. Native
legality and available-resource accounting still govern every action; role
ownership adds no engine gameplay rules.

War status is refreshed at activation, after actions, during the negotiation
watcher and before handoff. Beginning a war activates an isolated military
conversation; ending the last war retires it and reopens main if needed. New
cities reopen economy, captures reopen main for disposition, and purchases reopen
roles that may need to issue orders. Diplomacy and peace consultations suspend
all active gameplay roles. Suspended and finished conversations release compute
capacity while keeping renewable leases.

The shared role runtime owns a separate conversation, credentials, accounting,
heartbeat and bounded reconciled retries for each subordinate role. Conversation
context persists within the turn. Each subsequent turn starts a fresh
conversation with current observations and durable war records. Military settings
inherit main's provider/model/effort, with optional `[players.military_model]`
overrides using the same fields as `economy_model`.

## Goals and private coordination

`war_goals` reads war episodes, accepted goals, revisions, proposals, operation
notes, escort assignments and private messages. Main uses `action=initial` with
an opponent and a nonempty goal list before voluntary declarations, including
trade commitments and native diplomatic replies. Each goal records outcome,
priority (1 highest), completion criterion, acceptable costs and reconsideration
conditions. A failed declaration leaves a draft; an observed successful war
creates an episode. When attacked, military can defend before main provides its
initial goals, but main must establish them before finishing its turn.

Either main or military can `propose` goals against the current version. The
counterpart must `respond` with agreement/disagreement and a reason. After a
response either may `finalize` an agreed proposal; main may resolve disagreement
with its own reason. Old goals remain current until finalization, and stale
versions cannot overwrite newer decisions. Renewed wars create new episodes.
Completion is an assessment, never an automatic instruction to make peace.

The `war_goals` schema advertises each action's required fields separately:

| Action | Required arguments |
| --- | --- |
| `read` | None; omitting `action` also reads. |
| `initial` | `opponent`, nonempty `goals`, `reason`. Main supplies these even when the war already exists. |
| `propose` | `opponent`, current `version`, replacement `goals`, `reason`. |
| `respond` | `opponent`, current `version`, `proposal`, explicit `agree`, `reason`. |
| `finalize` | `opponent`, current `version`, `proposal`; main also supplies `reason` when overriding disagreement. |

Goal priority 1 is highest. Missing arguments receive field-specific recovery
instructions before dispatch. Escort assignment requires a current observation;
an invalidated observation requests `player_turn` before checking unit ownership.

`military_operation` stores military's plan, opponent references, progress
assessment, evidence references and reconsideration conditions. These are authored
assessments, not service-confirmed game outcomes. `escort_assignment` belongs to
main. `role_message` exchanges private internal messages and reopens a finished
recipient; messages for inactive military remain available for its next activation.
Military can also send `economy_request` for production and spending support.

These service-owned records live transactionally in civilization memory and are
included in checkpoints/rewinds. Mutation receipts make exact retries idempotent.
They are separate from ordinary model-editable notebook entries.

## Binding peace

Before publishing an outgoing binding peace offer or accepting one, including
city-state makePeace and canonical third-party peace clauses, the service freezes
permitted tactical observations, current war goals and exact terms. A bounded
read-only military conversation assesses objectives, battlefield position,
continuing-war costs/gains, immediate threats, concessions and uncertainty. A
separate main conversation then approves or declines those exact terms using its
private strategic memory and the military overview. Main may disagree with the
recommendation but cannot waive a missing overview.

Consultations also work off turn while native negotiations hold gameplay. They
have no live engine connection or mutation tools and run sequentially in slots
released by suspended gameplay. The caller's pending request is allowed to settle
before its normal conversation resumes. Capacity/invocation failures are visible;
consultation admission is time-bounded.

Reports and approvals are bound to the permitted tactical evidence, war episode
and revisions, and exact terms. The service checks freshness before dispatch;
trade mutations additionally use an optional native `expectedRevision` checked
under the game monitor. Native command revisions protect city-state peace.
Changed terms or tactical evidence require renewed consultation. Already
published offers remain native offers unless withdrawn. External native peace is
observed and reconciled without inventing a veto over engine behavior. Social
promises do not pass through this gate and remain nonbinding.

A failed overview/approval attempt stays pending, with at most three reconciled
attempts per evidence/terms basis. The failure and prior successful military
report survive restarts. Exact successful action retries use existing receipts.
Unconfirmed process shutdown and uncertain native requests stop the run.

## Compatibility and validation

Memory format 13 requires fresh match state; existing match images and stores
are not automatically migrated. No vendor engine changes are needed. Native
capacity admits economy and, only during war, military partners together; the
default per-civilization maximum is four for gameplay plus diplomacy. Smaller
limits that cannot admit the active roles return ROLE_CAPACITY_TOO_SMALL.

Tests cover war predicates, episode persistence, goal discussions and races,
escort ownership, role activation/retirement, the three-role turn barrier,
binding peace detection, required military/main decisions, freshness and exact
approval reuse. Native tests cover partner reservation and suspended capacity.
Model play quality must be assessed in fresh games or saved-position evaluations;
coordination tests do not establish stronger tactical play.

Controlled live acceptance is available through [the manager exercises](../scenarios/Manager-acceptance.md).
