# Concurrent economy management

Every agent player runs a main player, with an economy manager activated by
economic events and periodic reviews. Each active role has a fresh conversation,
a separate job credential, model usage, and
delivered observations. Conversations continue across tool calls, negotiation
suspension, and additional passes within the same turn. Neither model advances
the game directly: `finish_role` reports completion and the trusted turn
coordinator issues the native `END_TURN` after active roles finish, accepted calls
drain, negotiations close, and a fresh native turn review permits advancement.

The economy manager owns focus, citizen and specialist allocation, growth flags,
construction queues, purchases, building sales, city tiles, and shared-city tile
allocation. Main owns strategy, research, policies, workers, civilian and escort
movement, declarations and capture decisions. Military owns other combat-unit
movement, combat and upgrades in peace and war. All roles can spend available treasury gold.
Purchasing units belongs to economy management; deploying them belongs to the
appropriate unit controller. A newly purchased unit with movement reopens its
controller if needed. A newly owned city reopens a completed economy role.

`economy_request` records a priority, optional city, and request text. Requests
arriving during an active review join that pass and expand its city scope.
Routine requests arriving after completion wait for a scheduled review; urgent
requests reopen the current conversation immediately. Exact pending requests
with the same city and text share one ID; raising their priority preserves that
ID, and a repeated routine request cannot lower an urgent priority. Completed
requests are never automatically treated as proof that a new request is unnecessary.
Before accepting a new urgent request, the gateway checks the sender's delivered
queues and request dispositions against current facts. `ECONOMY_CONTEXT_CHANGED`
returns a fresh `stateUpdate` and coordination context without publishing the
request. The sender reconsiders and may submit a new request. Free text is never
classified as an already-fulfilled gameplay objective. The manager reports completed,
deferred, or rejected requests through `finish_role.requests`, with reasons.
Deferred requests may specify a future `reviewTurn`; without one they wait for
another economic event or the periodic review. New urgent requests require an
explicit disposition before economy can finish.
`city_override` gives the main player economic control of the named city for the
rest of the turn and records why. Ownership is checked when issuing commands,
including commands queued before the override. Native legality remains the
engine's responsibility.

## Wakeups and periodic reviews

`agents.economy_interval` defaults to 5 own game turns since the last completed
full economy pass. Urgent city events start a targeted review; they do not reset
the full-review clock. Each pass retains fresh empire constraints, while its
preload and city advice focus on the selected cities and their plans. A full
review covers every owned city. `1` requests a full review every own turn with cities.
Before founding the first city the economy role sleeps. Restarting a service or
resuming a checkpoint is not itself a wakeup: the saved baseline and review turn
are restored with private memory.

The host compares existing, player-visible observations using deterministic code;
it makes no JEV or model call to decide whether economy should run. Immediate
targeted reviews handle new cities, missing plans, empty/Idle queues, unassigned
citizens, starvation, stalled production, newly available economic options or
specialist capacity, watched affordable purchases and explicit review deadlines.
Native construction costs/production after a technology or policy change can
also select affected cities. First reviews and the periodic deadline select all
cities, as do changed empire resource access, war state or the following bands:

- Happiness crossing the scheduling bands below 0, -10 and -20; entering/leaving a
  gold deficit; and a treasury runway crossing zero or the review horizon.
  These bands schedule attention; native gameplay calculations remain authoritative.

Routine tile-yield/allocation changes, population growth without an unresolved
citizen/food problem, completed buildings with a queued successor, city
connections, golden-age transitions, and technology/policy completion without
changed economic options are collected in `pendingReasons`/`pendingCities` for
a scheduled review. Routine requests follow the same batching. Urgent requests,
due deferred requests and internal coordination still receive prompt attention.

Ordinary food/production accumulation, countdowns, treasury growth without a
watched purchase becoming available, and unit movement do not trigger reviews.
Events observed together produce one pass. Completing the pass acknowledges its
final native position for the reviewed cities, including its own changes.
Unchanged unresolved conditions are acknowledged once per own turn rather than
reopening the same conversation repeatedly. Local completion never acknowledges
another city's unseen changes. Checkpointed `cityReviewTurns` and
`lastFullReviewTurn` live in private service metadata, separate from authored
plans. A handled plan deadline is acknowledged without clearing or advancing
the plan's `reviewTurn`; a new future deadline is still honored.
Free-text reconsideration conditions are retained for the model; the host does
not classify or interpret them.

A dormant role has no model process, startup/advisory calls, compute permit or
usage. Main and military retain their normal ownership boundaries. Urgent requests
and economic events can start economy later in the same native turn. The final
native turn check wakes it if a city decision still blocks advancement. Sleeping
economy never blocks the turn barrier. The Economy dashboard displays its sleep
state, next periodic review, and the reasons for its latest wakeup.

## Plans and preload

The economy manager owns one versioned `city_plan` notebook record per city.
It records reasons for focus, growth, citizens, specialists, construction, and
significant purchases, together with expected benefits and reconsideration
conditions. A command can cite the delivered record through `intent.cityPlan`.
Successful native receipts attach service-owned `confirmedActions`; rejected
commands never acquire confirmations. Authored reasons remain provisional and
do not prove the decision's expected benefit occurred.

Each economy preload includes fresh empire constraints, selected city facts,
the main player's strategy, saved reasons for the selected cities, outstanding
requests, and explicit overrides. Detailed options and older evidence remain
available on demand. The default `economy` view lists available wonder names;
`construction_details` retrieves details when needed, and `player_turn(view="full")`
retains the complete catalog. Reviews appear as dated retrieval references;
accepted strategy and city plans remain the current intent. Lost-city plans are historical private knowledge, not
current authority. Inherited choices without a known rationale must be described
as inherited. Keeping a suitable allocation or queue is a valid decision.
It requires only `finish_role`: leave the plan unchanged unless a decision,
rationale, expectation or specific deadline changes. Progress figures and
countdowns belong to fresh observations rather than repeated plan rewrites.
`player_turn.cities` can select city handles; `cityScope.ownedCities` retains the
owned-city index. Omit the city selector for all cities in the selected view;
omitting `view` preserves that view. Other cities' details,
plans and native-legal actions remain available on demand; review scope guides
attention rather than defining a gameplay permission boundary.

When another role changes the game, the next detail read refreshes the role's
observation and returns a `stateUpdate` alongside the requested detail. Apply
that update before using the detail. A stale command or city-plan edit stays
unapplied and receives the same refresh for reconsideration, without requiring
a separate `player_turn` read. Replayed calls retain their original responses.
Economy startup prepares construction details; unit details remain available
on demand for questions about workers, protection or deployment.

The main player owns global strategy and cannot rewrite ordinary city plans
without an override. Economy management cannot rewrite strategy or negotiate
trades. City-related Decisions API advice goes to the economy manager, using the
existing optional advisory thresholds and budget; the main role does not request
a duplicate batch.

## Coordination delivery

Startup layout 10 delivers a role-specific `economyCoordination` working set per
fresh conversation, bounded to 12,000 serialized characters including delivery
metadata. It contains current goals and active operations for main/military,
economic requests and ownership relevant to each role, and addressed unread
messages or unresolved requests. Collection budgets keep a message backlog from
crowding out goals and requests. Exact rows are retained when they fit;
`history.omitted` identifies additional records available through filtered,
paginated `war_goals` reads. No omission completes or acknowledges a record.
Exact service-owned copies are removed from prepared reads and details; native
facts and authored text are retained. Startup takes its final coordination snapshot after the
preloads. Preparing a snapshot does not acknowledge it: the service commits the
delivery baseline when the model conversation starts.

Ordinary replies omit unchanged coordination and otherwise carry
`coordinationUpdate`. Its `scopeId` belongs to one conversation and epoch;
`sequence`, `baseStateId` and `stateId` protect ordering and replay. Changed
fields use the same replacement/removal conventions as native overview updates.
Requests and `warState.messages` use keyed collection updates. Replacing other
`warState` fields preserves the separate messages collection. The reference
reducer reconstructs the complete original state without interpreting messages.
Native `stateUpdate` remains a separate channel.

An explicit `player_turn` read returns the complete bounded working set.
`war_goals` defaults to current state; historical reads select `section`, optional
`ids`, `status` or `role`, and follow `nextCursor`. A single record larger than a
history page is returned exactly with `oversizedRecord`, never silently cut.
Context compaction, a fresh conversation or a restored job
clears the delivery baseline. An absent/mismatched baseline requires a full
snapshot. Durable responses precede cursor advancement, and replaying an older
call cannot acknowledge newer messages or rewind an already delivered state.
Delivery tracking is separate from durable message acknowledgement:
`role_message(action="acknowledge", ids=[...])` records that a recipient read a
message. A message sent with `kind="request"` remains open until its recipient
uses `action="resolve"` with a reason. Lasting instructions belong in current
goals, ongoing operations or economic requests rather than an unread transcript.
Supervisor status reports role lifecycle and counts, not these stored histories.

Operator-only `payloadCharacters` counters in the local accounting ledger and
startup metadata measure serialized payloads and coordination overhead. They are
character counts, not provider token estimates, and are never added to model
context or native accounting totals. Models and reasoning effort are unchanged.

Economy startup requests `player_turn(view="economy")`: city decisions, native
empire constraints, current threats, agreements and civilian work/travel plans.
The brief explicitly identifies deferred rosters, site catalogues and historical
comparisons. `player_turn(view="full")`, `chat_read`, `victory_progress` and owned
detail tools retain access to complete information. Automatic action refreshes
and reads that omit `view` preserve the selected view; an explicit `full` changes
it. Strategic advice and strategy/civilization references
remain available in startup. Economy's notebook schema contains city-plan create,
amend and retire operations; existing per-edit permission checks remain intact.

Main preloads promotion and upgrade menus only when the overview advertises a
currently available choice. Other required inspections and construction preloads
remain; complete unit menus are explicitly readable. See
[payload delivery](Agent-context.md#compact-payload-delivery) for versioned detail
reads, shared feedback facts and notebook receipts.

## Treasury and trade commitments

The engine retains total gold and separately tracks commitments owned by exact
published proposals. Available gold is total minus active commitments, floored at
zero. Gold transfers, research-agreement signing costs, and offered luxury and
strategic resources are protected. Drafts and nonbinding social promises create
no commitments. Pending gold-per-turn terms do not escrow future income.

Publication, replacement, spending, and settlement validate and mutate under the
game monitor. Settlement excludes its own commitment during validation and
consumes it with the actual exchange. Incoming gold can fund signing costs in
that exchange; it cannot fund another pending offer or an unrelated purchase.
Closing an offer releases its commitments. Mandatory gold or resource losses
invalidate newest unfunded offers and release their entire allocations.

The same accounting applies to native UI, native AI, automation, and structured
callers. Resource allocations do not enact exports or remove luxury benefits
before settlement. Observations expose total, committed, and available gold,
plus committed and spendable resource quantities. Native spending checks remain
authoritative even when a model acts from older observations.

## Suspension, accounting, and recovery

Shared diplomacy suspends both unfinished roles. Accepted calls retain their
identities and receipts, and resumed conversations receive current results and
facts. Waiting or completed roles release compute capacity while retaining job
leases, allowing diplomacy to run. Built-in AI requests continue allowing play,
with their offered assets protected by engine commitments.

Both roles count against match limits. Operator status reports combined usage
and separate `mainUsage` and `economyUsage`; native accounting reports each job
once. Economy model failures retry at most three reconciled attempts. Uncertain
actions, storage failures, unconfirmed shutdown, or exhausted limits pause for
inspection rather than silently skipping economy management.

Turn checkpoints include plans, requests, overrides, completion state, diplomacy,
and engine commitments. Rewind abandons both conversations and starts from
restored game facts. Usage remains outside rewind. Completion signals are bound
to the current role activation and turn attempt; old completions cannot advance
a restored turn. The native handoff receipt is the final authority for whether
the turn advanced.

## Configuration

Economy management is active for every future agent player. Settings inherit
from the player's resolved model configuration unless overridden:

```toml
[agents]
economy_interval = 5

[[players]]
name = "Player One"
model = "gpt-6-luna"
reasoning_effort = "medium"

[players.economy_model]
provider = "openai"
model = "gpt-6-luna"
reasoning_effort = "high"
```

The nested table belongs to the immediately preceding `[[players]]` entry.
`game start --economy-interval N` overrides the TOML interval; standalone
`agent-server start --economy-interval N` accepts the same setting.
Human and built-in AI entries cannot configure model settings. Resolved settings
are persisted separately for each role and retained on restart. Private memory
format 13 requires a fresh match directory. Existing matches retain their pinned
runtime image and memory format.
Review-scope metadata is additive within format 13: older schedule records use
their saved review turn as the last full review until new per-city stamps are
written. Updating the runtime does not itself trigger a review.

Military joins this barrier whenever a planning or execution pass is required;
see [Military manager](Military-manager.md) for scheduling, unit ownership, war
goals and peace consultation. Sleeping military does not hold advancement.

See [controlled manager acceptance](../scenarios/Manager-acceptance.md) for the live economy and war exercises.
