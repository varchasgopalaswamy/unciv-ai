# Military manager

Military is a civilization-local role that plans and controls combat units in
peace and war. It sleeps between useful passes. Barbarian encounters can wake
it for execution without creating a diplomatic war episode.
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

War status and military scheduling are refreshed at activation, after actions,
during the negotiation watcher and before handoff. Beginning a war wakes military
immediately; ending the last war keeps its unit ownership and requests a planning
review. Main reopens when changed war status requires strategic attention. New
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

## Planning and execution schedule

`agents.military_interval` defaults to five own turns between completed full
planning reviews. `game start --military-interval N` and standalone
`agent-server start --military-interval N` select the same positive interval.
The first meaningful military review is full. Full reviews assess opportunities,
readiness, upgrades, surplus units and the cost of continuing the current policy
over the same horizon as acting. A targeted execution pass does not reset this
planning clock.

Military, Main and the strategic adviser share combat-assessment guidance. Before
recommending preparation or delay, they estimate what the current force can do:
useful attack positions after declaration, an attack sequence with a capturing
unit, likely timing and losses. A quick, modest-cost capture receives weight for
both gaining a city and denying it to a rival; existing-army operations need not
displace city production. Reconnaissance should resolve a specific uncertainty
before the opportunity expires. Unseen forces are assessed by their possible
arrival and effects on capture or defense, rather than requiring complete enemy
knowledge. Native attack previews still govern execution after declaration.
Military sends Main an explicit recommendation to attack, take a specified
preparation step with a decision deadline, or decline for a concrete reason.
These are model instructions, not host-enforced strategic rules or war triggers.

The compact overview includes currently visible foreign cities and units,
including peaceful neighbors beyond our borders. It preserves native health and
defense values and provides map inspection coordinates. These summaries are
bounded at 24 cities and 48 units, report omitted counts, and exclude fogged
tiles and our own units. Military considers these opportunities during planning;
absence of hostile contacts alone does not establish that the army lacks an
attainable objective. Rejected commands require reconsideration and recovery;
`applied=false` does not mean an intended move or attack happened.

War requires execution on every own turn. Changed visible hostiles, combat-unit
or city damage, losses/reassignment, movement completion/interruption, units newly
needing orders, and explicit operation review dates can wake military earlier.
An explicit `role_message(to=military)` also wakes it immediately. Keeping an idle
unit or a sound plan unchanged remains a valid decision; the acknowledged state
does not wake it again without a new event or periodic review. Persistent native
movement orders continue between passes.

With Decisions enabled, changed permitted material facts can request a full
planning review before its scheduled date. Triage compares the completed planning
baseline with force capabilities, visible contacts, border forces, strategic
resources, supply penalties, accepted goals and operation criteria. It selects
only `review` or `wait`; military evaluates opportunities and main retains war
and peace authority. It does not classify every movement, stored-progress tick
or routine operation-progress rewrite. Immediate events and periodic reviews do
not require a classifier's approval. Classifier unavailability, budget exhaustion
or low confidence leaves those schedules intact.

Triage runs once at activation for relevant changed context, before subordinate
model work starts. Its original request evidence and identity are persisted before
submission; unchanged completed judgments are not reclassified, and uncertain
requests reconcile through the existing broker receipt mechanism. A refreshed
native view rejects a stale answer. The last full review, execution baseline,
planning baseline and triage state live in checkpointed civilization memory.
Finishing a military pass verifies the latest facts against its delivered view;
unseen changes require a fresh read before completion.

`economyCoordination.militarySchedule` reports `fullReview`, `atWar`, wake reasons
and review dates. `militaryActive` now indicates the manager's continuing role;
`militaryAtWar` separately reports diplomatic war status. Sleeping military has
no model process before its first wakeup and does not hold the native completion
barrier. A finished pass suspends inference and releases compute capacity while
its conversation waits for another pass or turn completion.

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

Military considers offensive opportunities during existing wars as well as in
peace. Repelling an attack, taking a city or completing a limited objective is a
reason to reassess whether another useful objective is attainable. A defensive
war's origin does not permanently determine its scope. Military proposes revised
goals and alerts main through the existing coordination tools; accepted goals
remain current until a revision is finalized.

Campaign review dates are reassessment points. Removing defenders, establishing
firing positions, healing or bringing up reinforcements can justify continuing
even before a city falls. Military compares that progress and the remaining
objective with changed feasibility and projected losses. A campaign need not be
immediate or nearly lossless to be worthwhile; progress also does not justify
continuing after the opportunity has deteriorated.

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
assessment, evidence references, reconsideration conditions and an optional
`reviewTurn` for a concrete decision deadline. Peacetime readiness or reconnaissance
operations can have an empty opponents list. These are authored
assessments, not service-confirmed game outcomes. `escort_assignment` belongs to
main. `role_message` exchanges private internal messages and reopens a finished
recipient; messages for inactive military remain available for its next activation.
Military can also send `economy_request` for production and spending support.

A changed strategic recommendation or proposed campaign needs a `role_message`
to main; storing an operation alone does not request main's attention.
Pre-declaration planning uses visible defenders, terrain, staging, resources,
travel and uncertain reinforcement. Native executable attack offers concern
current enemies, so their availability cannot be a prerequisite for declaring
war. After declaration, refreshed native previews still govern execution.
Missing pre-war previews establish neither safety nor success. No hypothetical
declaration or hidden enemy information is introduced.

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

These independent consultation conversations receive the same campaign guidance
as gameplay and strategic advice. Reaching a review date or completing the initial
objective does not by itself favor peace; both roles assess what continuing can
still achieve against its current costs and risks.

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
capacity reserves economy and military partners in peace and war; the
default per-civilization maximum is four for gameplay plus diplomacy. Smaller
limits that cannot admit the active roles return ROLE_CAPACITY_TOO_SMALL.

Tests cover war predicates, episode persistence, goal discussions and races,
escort ownership, role activation and dormancy, the three-role turn barrier,
binding peace detection, required military/main decisions, freshness and exact
approval reuse. Native tests cover partner reservation and suspended capacity.
Model play quality must be assessed in fresh games or saved-position evaluations;
coordination tests do not establish stronger tactical play.

Controlled live acceptance is available through [the manager exercises](../scenarios/Manager-acceptance.md).
For isolated Winnow scheduling scenarios, run
`.venv/bin/python -m tests.live_military_planning --output build/military-live-NEW`.
This uses synthetic permitted observations and a fresh authenticated broker with
the real local Winnow endpoint; it retains probabilities, accepted judgments,
latencies and deterministic scheduling checks. A low-confidence abstention is
reported separately from the classifier's top choice. Native strategic positions
from `generateDominationPositions` include ordinary, reinforced and damaged-city
comparisons and run through the saved-position evaluation harness.
