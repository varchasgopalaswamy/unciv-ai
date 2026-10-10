# Compact observations and victory progress

The civilization service returns a compact `player_turn` overview. The Kotlin host
still creates the complete permission-filtered snapshot; the service caches it for
the current job. This reduces model input, not the engine work needed to produce a
snapshot. Projected entity details reuse the cache without another host request.
Native pathfinding, special-mission previews and tree/victory queries run on demand in the host.

The overview includes tactical and strategy reviews, city and unit rosters,
production queues, growth, empire totals, research/culture progress, expansion and
exploration, resources, known diplomatic status, and a confirmed-action count.
Unit activity, strength and role are provided
once in the unit roster. Alerts and decisions retain gameplay facts,
effects and consequences; [audited native flavor text](../archive/reports/Model-flavor-text-audit.md)
is omitted from model views. Ordinary notifications and combat reports retain
their full content. Agents must read popup facts
before acknowledging it. [Ordinary notifications](Ordinary-notifications.md) can
be dismissed in batches after reading; agents choose which assessments to update.
Confirmed-action bodies and detailed economic/map data move to explicit reads.
Friendship popups include `friendship.accept.command` and, when permitted,
`friendship.declineCommand`. Both use the popup's alert handle, with the exact
`Accept`/`Decline` selection; the sender's civilization ID is not the action target.

| Tool | What it returns |
| --- | --- |
| `unit_details({target,sections})` | Selected unit sections listed below; omit `sections` or use `["all"]` for full details |
| `city_details({target,sections})` | Selected city sections listed below; omit `sections` or use `["all"]` for full details |
| `construction_details({target})` | Compact construction catalogue with city-specific yield gains, native completion times and entries unavailable for purchase |
| `construction_details({target,names})` | Up to 16 named entries with before/after yields and source changes, descriptions, costs, requirements, confirmations, queue progress and copyable commands |
| `map_region({center,radius,format})` | Explored tiles and frontier within radius 0–4 of an observed coordinate; respects world wrap; `format` defaults to `compact`, with `full` available |
| `player_details({section})` | `economy`, `diplomacy`, `settlement`, `research`, `policies`, `turn_history`, or `map_index` |
| `research_details({names})`, `policy_details({names})` | Existing on-demand tree catalogues and selected UI descriptions |
| `movement_preview({target,destination})` | Native adjacent route paths, estimated turn stops and costs; required before a matching `SET_UNIT_DESTINATION` |
| `mission_preview({target,mission,destination})` | Native rebase, paradrop, air sweep or nuclear preview; required before a matching special-mission command |
| `victory_progress({names,sections,metrics,civilizations,fromTurn,toTurn})` | Victory Status: own milestones, permitted global progress, current rankings and recorded trends by default; bounded charts and demographics on demand |

For example, read `player_turn`, copy a unit handle from `units`, read
`unit_details({target:"unit2",sections:["movement"]})`, and choose one of its
offered destination commands. For a distant destination use `movement_preview`
and its `SET_UNIT_DESTINATION` command. After a successful command, apply its
`stateUpdate` to the last overview and reread relevant `invalidatedDetails` before
the next action. Unchanged prior inspections remain valid. Queries for
missing entities return `available:false` without inventing their current state.

Named construction reads validate against the selected ruleset's construction
catalogue. Unknown names return `available:false`, `unknownNames` with spelling
suggestions, and a discovery read; they produce no construction row. A catalogue
read includes `knownNames`, including locked constructions. Known choices retain
`canQueue:false` and `queueUnavailableReasons`, including puppet control. Public
construction names are kept in the cached snapshot, outside the turn preload.
`detailLevel` distinguishes named reads from catalogue reads; it does not claim
that descriptions or completion estimates exist for every locked choice.

Unit and city rosters separate `attackOptionCount` (alternative attack commands)
from `attacksRemaining` (unused native allowance). Multiple origins or targets
can spend the same allowance. An unused attack still requires a legal target and
readiness. `availableGreatPersonAbilityCount` counts only usable abilities.

Worker choices expose `steps` with remaining work and `totalTurns` for the actual
native queue, preserving partial progress and any retained jobs. `yieldChanges`
compares the occupied tile after all steps; signed `maintenanceChanges` are
separate upkeep effects. `completionGrants` records one-time clearing yields and
their receiving city. These estimates assume the current worker and conditions.
Receipts name the work actually underway, including preparatory clearing.

Recognized city-state quests retain `objective.requirement`: neutral completion
conditions, public targets and expiry rules. Return-civilian options include
`diplomaticEffect` with the native operation, value and before/after values:
city-state influence is added; a major's `ReturnedCapturedUnits` opinion modifier
is set, so repeated returns do not stack it. This describes native diplomacy,
without predicting a human or agent player's intentions. Prior opinion values
are shown only for built-in AI owners, matching native diplomatic visibility.

Upgrade `additionalResourceRequirements` means the additional resources needed
after releasing the old unit's consumption. Economy details distinguish
`unimprovedOwnedTiles` from `unimprovedAlliedCityStateTiles`; allied locations
remain undisclosed.

## Opportunities at turn start

The separately prepared `victory_progress` is deliberately limited to a compact
rankings table and changes across up to ten recorded turns. It uses the existing
startup read and does not add a model request. Full milestone data and chart history
remain on demand; see
[Victory Status observations](Victory-and-turn-blockers.md#victory-status-observations).
These are observations at the returned `turn`, independent of `player_turn`
command deltas. Reread relevant sections when a later action changes a comparison
needed for a decision.
These reads now register their exact queries in `invalidatedDetails`, alongside
research/policy and trade/agreement/chat reads. A full overview does not refresh
those independent reads.

The prepared `player_turn` includes these summaries before the model's first
response. They also participate in ordinary command deltas; no additional opening
tool calls are needed.

- `civilizationProfile`: the selected ruleset's own civilization ability, all
  unique units/buildings (including future unlocks), and their differences from
  ordinary replacements. Effects retain native conditions; technology conditions
  also report `met`, so France's culture bonus visibly expires at Steam Power.
  Inherent promotions include their mechanical effects. Entries show required and
  missing technologies, resources, prerequisite buildings/improved resources,
  obsolescence and upgrade paths. Comparisons include lost benefits as well as
  gains, such as the Burial Tomb's reduced maintenance and missing specialist slot.
  `baseStats` uses unmodified ruleset values; city construction details supply
  current costs, yields and availability. Dotted stat keys distinguish flat
  `yields`, `yieldPercent`, `specialistSlots` and `greatPersonPoints`.
  Empty optional fields are omitted, while `ordinary`/`unique` comparisons retain
  zero or empty values that indicate a removed cost, benefit or requirement.
  An empty `missingTechnologies` remains explicit; meeting those requirements
  alone does not establish city-specific buildability.
  The profile is present before founding a city and in frozen strategic-review
  facts. It updates with own research and is omitted from unchanged command deltas.
- `research`: science rate, overflow, stored progress, native costs and current-rate
  completion estimates for up to eight technologies: the current choice, the next
  queued choices, then researchable alternatives. Upcoming units, buildings,
  wonders, improvements, resource reveals and mechanical effects use the native
  technology descriptions without quotations. Estimates exclude waiting behind
  earlier queue entries. `otherCandidateCount` identifies omitted alternatives;
  `player_details(section="research")` retains the full progress catalogue.
- `culture`: stored culture, the integer rate actually credited by native turn
  processing, next policy cost and estimated time, plus adopted branches and their
  remaining policies. Free choices have a zero wait; a stalled rate has no ETA.
  `player_details(section="policies")` includes this progress alongside choices.
- `cities[].yields` and `citizens`: current yields, focus, growth settings, stored
  food, unassigned population, specialist occupancy/slots and their benefits.
  City Great Person rates accompany empire `greatPeople` progress, native
  thresholds and estimates at the current rate. These cover city-generated Great
  People, not combat-earned General points. Assignments, shared thresholds and
  future rate changes can alter completion times.
- `exploration`: up to four geographic frontier groups, with an explored approach
  tile and two nearby owned units for each. Distances respect world wrap; they
  describe proximity, not a safe route or arrival time. The grouping only reads
  explored tiles and frontier coordinates, never hidden terrain or rewards.
  `player_details(section="map_index")` retains the complete frontier.
- `economy.resources`: available, used, imported, exported and offered balances,
  including shortages and city demands. Detailed sources stay in the economy
  inspection. `cityStates` includes known friendship/alliance benefits, resources,
  ally influence, unique units and the existing quest/action summaries.
- `settlement`: `eligibleSites` and `blockedSites` counts for explored tiles, plus the
  up to three candidate centers selected with native minimum-city spacing between
  them, so adjacent alternatives do not fill the summary. This is not a guarantee
  that all three cities can coexist or share their resources.
  Candidates must pass current native site restrictions, even when an undiscovered
  city causes a restriction. Selection follows score order. `NO_SITES_IN_EXPLORED_TERRITORY`
  describes the explored search scope; unexplored land may contain more sites.
  This summary is included even without a Settler,
  so it can inform whether to produce one. Scores adapt Unciv's advisory settlement heuristic
  to current and last-seen information. Full candidates are available through
  `player_details(section="settlement")`; native `movement_preview` supplies routes.
  With a Settler, the selected candidates also carry bounded native route estimates, including
  `unexploredTiles`, `tilesOutsideCurrentSight`, or that no movement remains. `settlers` retains each
  unit's current tile and destination even when the destination falls outside the
  top three or becomes blocked. Current-tile `canFoundNow` and the roster's
  `CAN_FOUND_NOW` status take precedence over surrounding fog uncertainty; an
  unseen surrounding tile alone does not prohibit a native-legal founding action.
  `firstObservedTurn`, `turnsObserved`, `turnsWithoutMovement`, and the last five
  observed destination changes expose delay without declaring it a mistake.
  Without a Settler, candidates name `nearestCity` and their hex distance from it;
  `nearestSettler` and unit route estimates are absent. With neither a city nor a
  Settler, scoring has no distance adjustment. Scores use permitted current
  and remembered observations. Site eligibility is the deliberate exception:
  hidden city or border changes can alter the settlement lens.
  These counters survive checkpoint recovery. They start at first observation,
  not an invented production date. Route turns are estimates using remaining
  movement, not a guarantee of safety, arrival, or future founding legality.
- Each settlement candidate includes known luxury, strategic and bonus resources
  within the native work radius, last observation turn, ownership, revealed
  strategic quantities, and overlap with existing cities' work ranges. It lists
  Worker/Work Boat requirements and missing improvement/clearing technologies.
  Unobserved workable tiles are counted, not filled with invented resources.
  The conditional luxury total counts each unclaimed luxury type once; already
  available luxuries add no ordinary happiness. Resources belonging to another
  player are not counted as acquisition benefits. Settling directly on a resource
  uses native city-center connection rules: for example, forested Spices still
  need Calendar, but founding does not require a Worker to clear the forest.
- `economy.happinessBudget` combines the native current happiness breakdown with
  available happiness buildings in existing cities, their independent completion
  gains, maintenance and production times. Settlement `newCityHappiness` groups
  identical site estimates: native city/population unhappiness at starting
  population and after growth, plus ordinary happiness-building possibilities,
  costs, technology requirements and blockers. These are cost components and
  conditional benefits, not a promised net founding result; free buildings,
  specialists and other founding effects are excluded. Site estimates use a detached
  map of remembered terrain, so fog does not suppress known costs. Missing terrain
  is counted in `unexploredWorkableTiles`; building `unresolvedConditions` identify
  requirements that unexplored surroundings might satisfy. Native prerequisites
  remain listed, including technologies and improved resources. If no city center
  has recorded terrain, `happinessBaseline` uses a known home tile
  for illustrative empty-city costs and supplies public base building rules,
  explicitly excluding policy/religion bonuses and site-specific availability.
  Buildings and luxuries take time to obtain. No estimate invents a production
  rate for an unfounded city or sums mutually dependent building scenarios.
- `economy.development`: unimproved owned resource tiles, including bonus
  resources, worked status, builder unit names, improvement requirements and
  current jobs. Missing technologies include required clearing steps: forested
  Spices list both Calendar for the Plantation and Mining for `Remove Forest`.
  Learning Calendar alone leaves Mining reported as a blocker; after Mining,
  the clearing job remains necessary before the Plantation site is available.
  `builder` names the civilization's native unit: `Worker` for land resources and
  `Work Boats` for water resources. Fish explicitly list `Work Boats` alongside
  Sailing as a missing technology when it has not been researched.
  Road and railroad technology separately enable lists of cities
  lacking their corresponding capital connection. Harbor connections follow the
  native engine. Site availability is not a promise that a unit can act or travel.
- `units[].upgrades`: native upgrade target, gold/resource costs, availability and
  blockers, with the focused economy inspection needed to issue the command.
- `units[].promotions`: acquired promotion names, unspent and total XP, the next
  promotion cost, and whether the unit can still earn XP from barbarians. When
  choices are legal now, the summary includes their mechanical descriptions,
  whether they are retained after selection, newly satisfied promotion
  prerequisites, copyable commands, and the XP balance/cost after selection.
  `inspections.PROMOTE_UNIT` supplies a focused read for comparing promotion choices.
  The complete promotion tree and unavailable reasons stay in
  `unit_details(sections=["promotions"])`. Units with no XP or promotions and no
  legal choices omit this summary.
  `recovery` compares resting without taking a promotion: healing at this turn's
  end, healing per subsequent rest turn, and estimated turn ends to full health.
  Movement or attacks may prevent this turn's healing without preventing healing
  on later rest turns. Estimates assume the same tile and bonuses with no further
  damage; they do not establish that waiting is safe. No healing on the current
  tile produces a null recovery time, while full health produces zero.
  Healing amounts include native final bonuses and the current missing-health
  cap; the orders inspection reports the same current-turn amount.
  Instant healing spends the native promotion cost without retaining a promotion
  or satisfying its prerequisites. Barbarian XP eligibility uses the engine's
  lifetime XP accounting, including XP already spent; a combat award can cross
  the limit before further awards stop. Prerequisite unlocks describe paths,
  not an immediately available next promotion.
- `cities[].constructionChoice`: the selection turn, observed duration and **all
  currently available construction names absent when that option was selected**.
  New options remain listed while the choice persists; this is not merely a
  previous-turn comparison. Imported choices have `chosenTurn:null` and an
  explicit first-observed baseline. Queue moves and checkpoint recovery preserve
  tracked selection history. Read named construction details to compare effects.
- `cities[].constructionEstimates`: compact production accounting and ongoing
  building gains for the current construction and the choice's `availableSinceChoice`.
  Wonder estimates are excluded here and live under `wonderOpportunities`, even
  when a wonder is the current construction or became available since that choice.
  The full catalogue and named comparisons use the same records; see
  [Construction estimates](#construction-estimates). These are comparisons under
  current conditions, not forecasts of population or assignments at completion.
- `cities[].constructionConversion`: when the current construction converts
  production to Science or Gold, its target stat, production per turn, base yield
  per production, and projected yield per turn **after native modifiers**. The
  base Science rate is 0.25; the final yield also reflects science bonuses.
  Named `construction_details` and the compact catalogue expose `conversion`
  before selection; `city_details(sections=["production"])` includes the city's
  `productionConversions`. These use a detached native statistics calculation for
  the proposed construction, without initializing a city copy or changing tile ownership, retaining current worked tiles, specialists and modifiers.
  They exclude future growth and citizen reassignment. Production overflow does
  not contribute to conversion. Resistance yields zero conversion output.
- `wonderOpportunities`: world and national wonders shown in the native city
  construction list, including queued wonders and visible unmet prerequisites.
  Benefits, Great Person points and specialist slots appear once per wonder,
  with eligible cities and native turn estimates, a shared production cost when
  identical across cities, and blocked cities grouped by common prerequisites.
  Available city rows retain stored production, production per turn and immediate
  while-building yield changes, in addition to completion benefits.
  Available/queued city rows also include that city's ongoing yield gains and
  maintenance change, plus growth estimates when food or growth changes. Named
  `construction_details` uses the same estimate record with full before/after
  yields, source changes, production accounting and blockers.
  Estimates describe building that
  wonder now, excluding time spent on earlier queue entries; blocked entries
  remain unavailable. Future wonders hidden by the native catalogue and completed
  wonders are omitted. Quotes, rival queues and rival progress are not included.
  Entries use the wonder name for deterministic row updates; completed wonders
  are removed and unchanged entries are not resent. An unrelated action does
  not resend unchanged wonder benefits. It provides information, not a
  recommendation to replace an existing construction.
- `diplomacyOpportunities`: surplus luxuries that can be offered, missing luxuries
  in known partners' permitted trade menus, and agreement terms expiring within
  five turns. `missingLuxuriesAvailableForTrade` is the partner's inventory;
  `spareLuxuriesToOffer` is this player's surplus. `luxuryTradeBasis` explains that
  terms require negotiation and consent. Agents and humans use the chat/trade
  interface; built-in AI players use its native offer bridge. Inventories describe
  possible negotiation, not willingness to accept a deal. Inspect `trade_options`
  before proposing terms.

These are information and suggestions. They do not enforce a strategic policy,
require a production change, or prohibit actions because of social promises.

`militaryReadiness` separates reconnaissance, melee, ranged, naval and air
forces using native unit types. It includes city garrisons, non-recon defenders
within three hexes, and currently visible foreign military within three hexes of
owned territory, including peaceful neighbors. Distances use native map wrapping;
they are not travel times. Foreign forces carry their observation turn and war
status and never reveal hidden units or total foreign armies. Recruitable military
names are grouped by city; existing roster upgrades and named construction/unit
economy reads provide costs and requirements. Losing non-recon units or adding
cities without increasing that force flags related plans for advisory review.
The optional strategy guide discusses defense and the costs of settlement delay.
This is a separate overview section, so a tactical change does not resend an
unchanged recruitment or border-force summary.

Memory validation reports the actual field's item/character limit or numeric
bound, using schema-owned field names and counts rather than rejected content.
For example, 23 subject tags report `subjects`, maximum 20, received 23, and advice
to replace that list. Creation errors reject the batch before writing; amendment
results retain per-edit acceptance, and a failed edit leaves its record untouched.

## Construction estimates

The host records native estimates for each owned city's displayed buildings and
units, including visible blocked choices and queued items. Existing projections
select the appropriate amount of detail:

| Surface | Estimate detail |
| --- | --- |
| Turn overview | Compact current construction and newly available choices; city-specific wonder gains |
| `construction_details({target})` | Compact gains and production accounting for the catalogue |
| `construction_details({target,names})` | Before/after yield vectors, changed sources, growth and production accounting for named entries |
| `city_details({target,sections:["production"]})` | Detailed estimates for queued items |
| Full city read | All displayed construction estimates |

`production` includes native cost, stored production, overflow, production per
turn, and `turnsIfStartedNow`. This uses the city's production modifiers for that
specific item and excludes waiting behind earlier queue entries. Resistance or
no production yields a null time. Native `queueUnavailableReasons` and `canQueue`
still determine availability; an estimate does not authorize construction.

The overview's current-item time lives in `constructionEstimates[name].production`;
there is no separate `constructionTurns` field. Queue progress, named estimates
and wonder options share one native completion-time calculation. City details
put per-copy production in `development.constructions[].production`, and omit it
from the corresponding queued item's benefit estimate. Named construction reads
share the entry's `production` with `progress`; a progress row includes its own
`production` only when different, such as a later unit copy with no stored work.

`completion` compares ongoing yields before and after installing a building at
the current population and citizen assignments. Science, net food surplus,
production and gold always include explicit changes, even zero. Other zero
changes are omitted in compact deltas. Gold includes building maintenance;
`maintenanceChange` separately identifies that cost. Named reads also include
the changes in native source rows. Specialist slots and Great Person points are
listed separately; empty new slots do not contribute worked specialist yields.
Units have production estimates and no building-completion comparison.

Both sides of the completion comparison use no active construction. Separately,
`whileBuilding` compares starting the proposed item with the current queue head.
For example, switching from Science to a Library can reduce science while it is
being built, then add population-dependent science after completion. Stopping a
Settler's food conversion is also a queue-switch effect, not a building benefit.

`growth` supplies native rounded food accumulation and growth/starvation times
before and after hypothetical completion now. This matters when fractional food
rounds to a different amount in storage. Compact views include this when food or
growth changes, including unchanged growth times despite a fractional food gain.
Neither growth nor yields predict future population,
citizen reassignment, or conditions at the actual completion turn. Completion
triggers such as free buildings, units, technologies and population, and yield
changes in other cities, are excluded. The building's existing description
remains necessary for these and other non-yield effects. A shared
`constructionEstimateBasis`/`estimateBasis` explains these limits to the model.

The reusable native comparison uses an isolated game copy, never temporarily
adding a building to the live city. Estimates are cached until native saved
state or owning-city statistics change; command execution clears the cache.
Only owned-city results enter the permission-filtered snapshot. Detailed
breakdowns remain in selective reads; repeated unchanged overview sections are
not resent. Existing evidence and delta handling invalidates a selected
construction read when its returned facts change. Reading these estimates
requires no additional host or model round trip.

## Compact map tiles

City tiles carry the observed city name, owner and original-major-capital status.
Those facts retain their last observation under fog, with `observedTurn`.
`cityPopulation`, `cityHealth`, `cityMaxHealth` and `cityDefensiveStrength` are
available only while the city tile is visible. Unknown city facts remain null;
unexplored coordinates are not returned. Hidden capture, renaming, growth,
fortification or razing does not refresh remembered city information.

The default `map_region` response may include `data.tileDefaults`. A tile inherits
those defaults and replaces them with its own explicit fields. For example,
`tileDefaults: {"owner": null}` applies to every returned tile except those with
their own `owner`. Explicit `null`, `false`, empty lists and empty objects are
real overrides. Missing fields remain distinct from nulls. Position, terrain,
visibility, units and yields remain directly on each tile.

This factors repeated values after the existing visibility and region selection;
it preserves every returned fact, including fields unknown to the adapter. Small
responses keep ordinary tiles if defaults would make them larger. Use
`format: "full"` for expanded tile objects. The reference
[`expand_tiles`](../../src/civ_agent/map_format.py) reconstructs them exactly.
Changing representation alone does not mark a plan's evidence as factually changed.
See the [saved-response measurement](../archive/reports/Gameplay-read-audit-2026-09-22.md#implementation-follow-up).

## Focused sections and commands

Every section includes the entity's identity and basic status. Multiple sections
can be requested together; order and duplicate selections are canonicalized.
Full reads retain all native fields, including future fields not yet assigned to
a section. These projections use only the permission-filtered cached snapshot.

| Unit section | Details and applicable actions |
| --- | --- |
| `movement` | Native offered paths/costs, active destination/cancellation and nearby hostiles |
| `combat` | Strength, remaining attacks and full combat previews |
| `orders` | Current order, automation status and available fortify/sleep/skip/wake/explore/automate controls |
| `promotions` | Experience, prerequisites and promotion choices |
| `work` | Worker jobs, prerequisites, yield changes and Work Boat improvements |
| `settlement` | Current-tile founding diagnostics, active destination's site summary, nearby hostiles and native settlement promises |
| `economy` | Upgrades, pillage, disband, costs and confirmations |
| `abilities` | Great Person abilities, spaceship assembly and city-state gifts |
| `air` | Missions, ranges, bases/capacity, transport and interception readiness |

| City section | Details and applicable actions |
| --- | --- |
| `status` | Capture choices and city status |
| `production` | Queue, available names, progress, queued construction estimates and queue management |
| `citizens` | Growth, food, focus, worked/locked tiles, specialists and shared tiles |
| `economy` | Yields/breakdowns, buildings and sales with confirmation text |
| `purchases` | Construction/tile costs, eligibility and confirmations |
| `combat` | Full bombardment previews |

Prefer named research, policy and construction reads after selecting candidates
from the overview or a catalogue. Named research includes copyable native
prerequisite queues and free-technology choices; named construction includes
queue/purchase commands. Available entity options and native movement/mission
previews also include commands. Copy only command arguments, optionally add private
structured `intent`, and read any `confirmationField` and consequences before
explicitly confirming. Templates never prefill consent or intent, and native
legality is checked again during execution. Air-base options provide the exact
preview request; reading an air section alone does not satisfy a mission preview.

Routine commands do not require a particular detail read. Use named construction,
unit sections and city sections to inspect options and consequences when useful.
The host still checks legality and explicit confirmations at execution.
`DETAIL_REQUIRED` applies nothing and names an exact native movement, mission or
road preview needed before its command. Air-section reads cannot replace a mission
preview, and another action may invalidate a previously delivered preview.
Social commitment decisions require `chat_read`; formal trades require `trade_read`
so consent remains tied to the exact delivered terms.

Available major-player diplomatic responses expose a `responseTool` template
for `chat_send` with `nativeAction=RESPOND_DIPLOMACY`. The agent supplies its own
text and optional intent after inspecting consequences. City-state responses retain their
separate `RESPOND_CITY_STATE_ALERT` commands; trade and peace use trade tools.

Worker and Work Boat details include `workScope`: the offered jobs apply to the
unit's current tile only. A copyable `map_region` query exposes nearby observed
terrain, improvements and yields without claiming that no work exists elsewhere.
Movement receipts include `exploration` with before/after explored tile counts and
`newlyExploredTiles`, including zero. These counts compare only the player's
permitted maps; movement success alone does not establish exploration progress.

Argument errors return the selected command's expected shape and safe field error
codes, without echoing invalid values. `OBSERVATION_REQUIRED` supplies a concrete
refresh query. Successful gameplay actions, including alert acknowledgements,
return [state updates](../architecture/Command-state-updates.md) with deterministic section/row
changes. Only changed inspections expire; native movement/mission previews still
expire on a changed revision. An argument error with `applied:false` does not
itself require another overview. Transport refresh failures preserve the applied
receipt and require a full `player_turn`. Invalid snapshots or internal
projection/persistence failures stop with `STATE_UPDATE_FAILED` for inspection;
neither case repeats the action.

Pydantic defines the service's overview, detail input and detail result schemas.
`observation:overview` cites the game overview; each detail response has an
short `evidence` handle such as `e123` for precisely that delivered revision.
After refreshing a changed query, use its new handle for ongoing assumptions;
the old handle remains a historical citation. Cached raw data is not marked as
delivered. Previously requested detail projections are recomputed when a new
snapshot arrives so changed assumptions are flagged, without claiming the model
read those changes. Movement and mission previews are retained for an unchanged revision and
invalidated when the revision changes; they require a fresh native query rather
than reconstruction from cached map data. The compact unit roster retains the
active destination, including `movementOrder.settlement` for settlers, Work Boat
action availability, available air missions, transport state and automatic
interception readiness. The destination's visible site blockers arrive in command
updates even when another unit reveals them; `settlementStatus` still describes
the settler's current tile. Full `unit_details.air` adds bases, remaining slots and ranges. A greeting or a change to
another entity does not invalidate
a plan depending only on the inspected section. Changes to another section of
that same unit also leave its focused evidence unchanged. Historical snapshots remain
available through explicit memory reads with the same flavor-text projection.
Raw host observations and receipts remain in audit storage. Restart and interrupted-turn recovery use the
same versioned SQLite evidence store.

Victory progress uses Unciv's pure milestone completion checks and victory-screen
header text, including cultural counts and spaceship-part progress. Rival next
milestones follow `VictoryScreenCivGroup`'s visibility rule; unmet living rivals
are omitted before ordering. `names` selects the displayed victory types returned
by this query. The turn limit remains visible for Time victory. Total score is
operator-only and excluded from preloads, reviews and all player statistics.
The query does not acknowledge popups.

Rankings, charts and demographics follow the selectors and visibility rules in
[Victory Status observations](Victory-and-turn-blockers.md#victory-status-observations).
The [coverage ledger](Player-api-coverage.md#every-victory-screen-tab-and-terminal-control)
tracks remaining victory-screen controls.

Regression coverage includes cached detail reads, required confirmations, unread
evidence rejection, selective assumption invalidation, restart, map-wrap distance
against the engine, full alerts, response-size reduction, native victory progress,
HTTPS delivery and unmet-rival changes.

### Saved-position verification

`prepareStrategyPositions` can observe a copied host checkpoint while preserving
its remembered map, handles and progress counters:

```sh
./gradlew prepareStrategyPositions -PstrategyMode=observe \
  -PstrategySource=/absolute/path/host-checkpoint.json \
  -PstrategyOutput=/absolute/path/new-output-directory
```

This mode copies the checkpoint before loading, writes `initial-turn.json`, and
checks that observing did not change native game state. A raw game save lacks
remembered settlement knowledge and is not an equivalent replay.
Use `-PstrategyCivilization=France` to inspect a particular player rather than
the current player; waiting players retain their normal action restrictions.

The production/wonder checks compared projected Science and Gold with actual
native output after queue changes, including science/gold modifiers, Golden Ages,
overflow and resistance. Wonder checks covered unlocks, unmet national-wonder
prerequisites, queued/completed wonders and independence from rival build progress.
The France turn-100 checkpoint reproduced current Science conversion of 13.1 in
Paris, 3.6 in Orleans and 2.8 in Lyon without changing native state. Its 20 visible
wonder entries occupy 6,686 UTF-8 bytes in the compact overview, versus 16,873 bytes
for full per-city accounting; the latter remains in named construction reads.
Those size measurements predate the additional city-specific building gains.

Building-estimate checks compare native completion with population-dependent
Library science, worked Wheat/Granary food, Jungle/University effects, unstaffed
specialists, maintenance and Golden Age production. They verify live-state
purity, repeatability, cache refresh, HTTPS delivery and independence from rival
construction progress. The checked-in France turn-26 slow-economy position
reproduces Orleans at population 1: Library adds zero science and takes 49 turns;
Granary adds 0.5 net food and takes 40 turns under those current conditions.
Unhappiness limits food, and native rounding leaves food accumulation at 1 per
turn and growth at 9 turns both before and after the Granary. Python checks cover
compact versus named output, startup delivery without extra host reads, and selective evidence
invalidation. These deterministic checks do not measure model strategy changes.

All nine saved strategy positions were also prepared and reloaded with these
estimates. Their compact overviews add 1,138–3,041 UTF-8 bytes, including the
shared explanation and per-city wonder gains. They carry one or two current
construction estimates while the private snapshots contain 7–32 catalogue
entries. The France turn-26 overview adds 1,600 bytes. These are payload sizes,
not token counts; existing command deltas omit unchanged sections. In the
focused France check, initial observation took 280 ms and an unchanged repeated
observation took 50 ms on the development machine.

The September 24 checks covered nine existing strategy saves, the current game's
Rome turn-74 checkpoint, legal founding with surrounding fog, blocked targets,
exhausted movement, progress after restart, peaceful border forces and hidden-unit
changes. France's actual turn-68/70/71 rejected amendments now identify subject
lists of 22/22/21 items against the limit of 20. These checks validate the delivered
information and recovery guidance, not a measured improvement in model strategy.

The September 20 [efficiency evaluation](../archive/plans/Agent-efficiency.md#tasks-2-and-5-validation-and-live-results)
records focused unit/city reads, named choices, matching commands, payload sizes,
and remaining refresh retries in a real Luna/medium opening turn.

## Live verification, September 14, 2026

A fresh seed-412 Vanilla game completed one round through the three HTTPS services
with `gpt-5.6-luna`, medium reasoning. Evidence is retained locally in
`/tmp/civ-overview-check-20260914-130526`, including `verification.json`, the native
checkpoint, per-player transcripts, receipts, private memory and diagnostics. All
verification processes were stopped afterward.

The native save reached turn 1 with Rome active. Rome and Athens each had a
Monument queued with 4 production stored; Paris had a Scout queued with 4 production.
All three researched Pottery and retained their Warriors; final movement receipts
matched native unit positions. Each player read victory progress and used the
detail queries, then committed exactly one handoff. There were no unresolved
dispatched requests. Rejected argument shapes, stale assumptions and missing reads
applied nothing; the agents recovered and completed their turns.

| Civilization | Median full snapshot | Median overview reply | Reduction | First-turn duration |
| --- | ---: | ---: | ---: | ---: |
| Rome | 30,782 bytes | 3,355 bytes | 89.1% | 206.1 s |
| Greece | 33,411 bytes | 2,795 bytes | 91.6% | 158.1 s |
| France | 39,826 bytes | 2,752 bytes | 93.1% | 152.1 s |

These compare serialized permitted snapshots with delivered overview replies from
the same run, not tokens or a controlled before/after timing experiment. Median
host turn-query time was 10–18 ms. Model calls and repeated inspection still cost
substantial time; smaller replies alone do not establish a turn-time speedup.
Greece recovered from invalid memory priorities by creating plans without topic
subjects, which limits automatic topic-specific review. A single opening round
does not establish later expansion, barbarian defense or strategic reliability.
