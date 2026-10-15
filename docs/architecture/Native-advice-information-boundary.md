# Native advice information boundary

Privacy eligibility is deterministic engine/host behavior. The Decisions API does
not receive game-state inputs or decide what the native advice view may disclose.
Its separate role is scheduling already-authorized diplomatic messages.

Advice is an additional permitted information surface, rather than a reason to
grant visibility. The host validates the authenticated player, observed epoch,
revision, active turn and observation cache before reading advice. It captures
value-only selector inputs under the game lock, selects outside that lock, and
rechecks the same boundaries before returning. Unsupported dependencies make a
branch unavailable; an omniscient recommendation cannot be made safe merely by
removing names or coordinates from its result.

## Branch dependencies

`NativeAdvice.kt` supplies `ReadOnlyAutomation` with these inputs. Public rules
means the selected Vanilla ruleset; owned state means the player's own cities,
units, economy, technologies, policies, resources and native preferences. The
table describes the allowed dependency boundary, not exhaustive player API
coverage.

| Branch | Permitted dependencies | Cases not selected |
| --- | --- | --- |
| Continue construction | Observed owned-city queue, actual production cost and investment | No construction is replaced merely to reconsider existing investment |
| Construction | Observed candidates and estimates, observation-time native building preferences, public rules, owned-city/empire yields and worker count | World-wonder competition, foreign-capital defensive bonuses, strategic military, Work Boat and space-part production |
| Purchase | Observed available current construction, actual gold cost, hurry modifier, investment and own treasury | Other constructions and unavailable purchase options |
| Free citizen/specialist | Observed owned-city assignments, tile yields and specialist options; own city focus, growth, happiness, research and economy; coordinate tie-breaking | Reassignment of existing citizens and unavailable/manual specialist options |
| Upgrade | Observed available upgrades, actual cost/resource requirements, own unit requirements and public unit production costs | Unavailable upgrades and native candidates requiring a new resource type |
| Promotion | Observed promotion choices, own health, experience and mobility; public promotion effects | No native random draw is consumed; alternatives remain alternatives |
| Healing | Observed own health, rest order and healing; observed owned-city health; distances to currently visible hostile military units in the full permitted map | Hidden enemies are never consulted. Outside the native safe-city exception, a visible enemy at distance three prevents resting advice |
| Attack/bombardment | Already-permitted legal attack previews and damage bounds, observed target type/health and movement, observed terrain plus public terrain definitions | Hidden damage rolls, unsupported specialized branches and unit attacks on cities requiring a complete surrounding-force count |
| Military advance | Already-permitted attack previews, legal one-turn movement entirely within current sight, observed origin terrain and target coordinates | Strategic enemy-city routing, risky moves and hidden routes |
| Worker improvement | Observed current tile/resource/improvement, available single-step job, actual turns and yield changes, own economy, nearby observed improvements and public improvement definitions | Current-job replacement, outside-border ranking, adjacency planning, roads, repairs, removal and multi-step jobs |
| Exploration | Observed legal one-turn paths within sight, observed destination terrain/owner, public terrain/nation definitions, observed hostile positions and snapshot known-coordinate set | Hidden terrain/ownership, invisible units and paths outside current sight; unseen contents are never scored |
| Settlement | Retained permitted tile observations and yields, own cities/capital/resources, public rules, legal known one-turn destinations and map geometry | Long routes, undisclosed city identities, foreign diplomatic flags and simulated arrival/discovery |

Map geometry may supply coordinates, neighbors and wrapped aerial distances. It
does not authorize reading a tile's live ownership, terrain, resources or units.
Destination terrain damage and exploration elevation are derived from the
snapshot's terrain names plus public terrain definitions. Exploration ownership
and discoveries also come from that snapshot. Therefore changes under fog cannot
affect these ranks until another permitted observation delivers them.

The healing danger input deliberately does not reuse the two-tile
`nearbyHostiles` warning. It derives the native three-tile rule from visible
hostile military units in the full snapshot. Advice describes the absence of
**observed** enemies, and explicitly leaves unseen threats unknown. When the
native owned-city exception applies, its explanation identifies that exception
instead of claiming there are no nearby enemies.

## Caching, failures and validation

Successful advice is cached by player, epoch and revision. The revision guard
precedes cache lookup; selective city/unit reads filter that complete result
without recapturing engine inputs. Rewind or a new epoch cannot reuse the old
key. Expired/cancelled requests and results made stale during computation return
unavailable with no recommendations. Capture failure, timeout, cancellation and
planner saturation have distinct public reasons without exposing exception
contents or hidden engine values.

`NativeAdviceProbe.kt` checks a visible enemy exactly three tiles away, hidden
enemy movement/health invariance, the safe-city exception, withheld terrain/
ownership/discovery invariance, cache hits that do not enter input capture,
expired cached requests, stale epoch/revision rejection, and save/cache/handle
purity. It also executes a suggested citizen command through the ordinary HTTPS
schema and native command path.

These checks cover the implemented selectors. They do not establish exhaustive
player API privacy or native automation parity. Advice inherits the permitted
facts and legal previews supplied by `player_turn`; changes to those upstream
surfaces require their own disclosure review. Mod content and unsupported
selector branches remain outside this audit.
