# Concurrent economy management

Every agent player runs a main player and an economy manager during its native
turn. Each has a fresh conversation, a separate job credential, model usage, and
delivered observations. Conversations continue across tool calls, negotiation
suspension, and additional passes within the same turn. Neither model advances
the game directly: `finish_role` reports completion and the trusted turn
coordinator issues the native `END_TURN` after both roles finish, accepted calls
drain, negotiations close, and a fresh native turn review permits advancement.

The economy manager owns focus, citizen and specialist allocation, growth flags,
construction queues, purchases, building sales, city tiles, and shared-city tile
allocation. The main player owns strategy, research, policies, workers, movement,
combat, capture decisions, and upgrades. Both can spend available treasury gold.
Purchasing units belongs to economy management; deploying them belongs to the
main player. A newly purchased unit with movement reopens an already completed
main role. A newly owned city reopens a completed economy role.

`economy_request` records a priority, optional city, and request text. Routine
requests arriving after economy completion carry to the next own turn. Urgent
requests reopen its current conversation. The manager reports completed,
deferred, or rejected requests through `finish_role.requests`, with reasons.
`city_override` gives the main player economic control of the named city for the
rest of the turn and records why. Ownership is checked when issuing commands,
including commands queued before the override. Native legality remains the
engine's responsibility.

## Plans and preload

The economy manager owns one versioned `city_plan` notebook record per city.
It records reasons for focus, growth, citizens, specialists, construction, and
significant purchases, together with expected benefits and reconsideration
conditions. A command can cite the delivered record through `intent.cityPlan`.
Successful native receipts attach service-owned `confirmedActions`; rejected
commands never acquire confirmations. Authored reasons remain provisional and
do not prove the decision's expected benefit occurred.

Each economy preload includes fresh empire constraints, current city facts,
the main player's strategy, saved reasons for every owned city, outstanding
requests, and explicit overrides. Detailed options and older evidence remain
available on demand. Lost-city plans are historical private knowledge, not
current authority. Inherited choices without a known rationale must be described
as inherited. Keeping a suitable allocation or queue is a valid decision.

The main player owns global strategy and cannot rewrite ordinary city plans
without an override. Economy management cannot rewrite strategy or negotiate
trades. City-related Decisions API advice goes to the economy manager, using the
existing optional advisory thresholds and budget; the main role does not request
a duplicate batch.

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
Human and built-in AI entries cannot configure model settings. Resolved settings
are persisted separately for each role and retained on restart. Private memory
format 12 requires a fresh match directory. Existing matches retain their pinned
runtime image and memory format.
