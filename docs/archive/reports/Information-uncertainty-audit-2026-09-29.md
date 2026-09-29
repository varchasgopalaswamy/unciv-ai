# Information uncertainty audit — September 29, 2026 (UTC)

## Scope

Reviewed player observations, turn preloads and tool guidance for withheld facts,
remembered information and uncertain forecasts. The settlement lens change is
implemented separately: explored tiles report native eligibility even when an
undiscovered city causes the restriction. The findings below describe the audit
before the follow-up implementation approved by the user; see the implementation
record at the end.

Comparisons use the official manuals and the selected Unciv checkout. No Civ V or
Civ VI desktop binary was exercised; undocumented, version-specific UI behavior
is not treated as established fact.

## Findings for a decision

### 1. Information revealed outside unit sight can be missing — confirmed gap

On city-state contact, Unciv reveals the capital center with `setExplored` and can
attach its location to the contact notification. Our remembered map records
visible/owned tiles and visibility callbacks. A newly explored, distant capital
can consequently appear with null terrain, owner and city name. Rome's saved
position demonstrated this for Kuala Lumpur at `(1,-10)`.

Both games retain previously revealed terrain under fog. Losing current sight
should not erase a legitimate discovery. This is distinct from learning about
later changes under fog. See the [Civ V manual, p. 22](https://downloads.2kgames.com/civ5/site13/community/feature_manual/Civ_V_Manual_English_v1.0.pdf#page=22)
and [Civ VI manual, pp. 32–33](https://cdn.akamai.steamstatic.com/steam/apps/289070/manuals/CIV_VI_25TH_ONLINE_MANUAL_ENG.pdf).
The specific first-contact reveal is confirmed in Unciv, not independently tested
in either Firaxis game.

**Suggested direction:** record information when a legitimate reveal occurs,
including remote discoveries. Do not refresh arbitrary hidden tiles afterward.

Code: `GameExecutor.observeVisibility`, `captureSettlementKnowledge`,
`MapObservation.observeMap`, and native `DiplomacyFunctions.makeCivilizationsMeet`
and `Tile.setExplored`.

### 2. Expansion happiness estimates have an all-or-nothing visibility gate

`settlementHappiness` returns null if any unowned tile in the proposed city's work
radius is outside current sight. This discards the whole site budget, including
ordinary city/population costs, to avoid evaluating conditional effects against
hidden surroundings. The preload supplies an illustrative home-tile baseline,
so the information is incomplete rather than entirely absent.

Civ V exposes empire happiness and its city/population/resource/building causes
([manual, pp. 107–108](https://downloads.2kgames.com/civ5/site13/community/feature_manual/Civ_V_Manual_English_v1.0.pdf#page=107)).
Civ VI instead uses city amenities and housing
([manual, city details](https://cdn.akamai.steamstatic.com/steam/apps/289070/manuals/CIV_VI_25TH_ONLINE_MANUAL_ENG.pdf)).
Neither comparison establishes an identical pre-founding forecast; the relevant
design distinction is known costs versus uncertain site-dependent effects.

**Suggested direction:** preserve calculable components and qualify only the
effects that need missing information. Keep building and luxury prerequisites.

Code: `ExpansionOverview.settlementHappiness`, `expansionHappinessBaseline`,
and Python `mechanics_overview.settlement_summary`.

### 3. Demographics conceal numbers as well as unmet identities

For an unmet best/worst civilization, `observeVictoryDemographics` returns null
for both identity and value. Own rank and the global average still include that
civilization. This matches the current Unciv demographic UI's shared civilization
widget; it is not an accidental host-only omission.

Civ V's documented demographic screen supplies rank and best/average/worst
figures ([manual, p. 25](https://downloads.2kgames.com/civ5/site13/community/feature_manual/Civ_V_Manual_English_v1.0.pdf#page=25)).
Civ VI's World Rankings is a different interface, not a basis for assuming
identical metric disclosure ([manual, p. 37](https://cdn.akamai.steamstatic.com/steam/apps/289070/manuals/CIV_VI_25TH_ONLINE_MANUAL_ENG.pdf)).

**Suggested direction:** consider anonymous best/worst values while withholding
identity and retaining game settings and the frozen demographic snapshot. This
would be a deliberate disclosure-policy change. Exact patched Civ V behavior for
unmet leaders was not tested here.

Code: `VictoryStatistics.observeVictoryDemographics` and native
`VictoryScreenDemographics` / `VictoryScreenCivGroup`.

### 4. One movement flag covers several different uncertainties

`speculative` becomes true whenever a route crosses any tile outside current
sight. It does not distinguish unexplored terrain from a known road with unknown
occupancy. A legal route can therefore sound as uncertain as an uncharted one.

Both games offer multi-turn movement and stop when newly discovered obstacles
invalidate it ([Civ V, p. 51](https://downloads.2kgames.com/civ5/site13/community/feature_manual/Civ_V_Manual_English_v1.0.pdf#page=51);
[Civ VI, p. 69](https://cdn.akamai.steamstatic.com/steam/apps/289070/manuals/CIV_VI_25TH_ONLINE_MANUAL_ENG.pdf)).
Our ability to issue orders is consistent with that; the concern is presentation.

**Suggested direction:** distinguish route/arrival estimates from unseen occupants
or danger. Retain execution-time checks and native pathfinding.

Code: `MapObservation`, `MovementOrders`, and `SettlementProgress.settlementRoute`.

### 5. Some city-state messages suppress a supplied identity

The same unmet-civilization masking is applied to quest targets, protectors,
allies and contest leaders. Gift/protection requests can name an unmet bully in
the native human quest text, while our structured version calls it unknown.
Knowing a name through a message need not establish diplomatic contact or reveal
the civilization's location.

An initial concern about ordinary `FindPlayer` quests did **not** hold up:
native assignment chooses an already-known civilization whose territory has not
been encountered, so that quest retains its name. Hostile target and denouncement
quests also have contact checks. This is not a general broken-quest finding.

**Suggested direction:** decide whether identities explicitly supplied in native
messages should survive masking. Keep independently hidden identity fields
separate. Unciv's message difference is confirmed by code; precise Civ V/VI
equivalence for these specific requests remains unverified. Civ VI's envoy quests
are not a direct equivalent of these influence requests.

Code: `CityStates.civilization` and `quest`; native `QuestTargetHelpers`,
`QuestImplementation.GiveGold` / `PledgeToProtect`, `AssignedQuest.getDescription`
and `CityStateDiplomacyTable.getQuestTable`.

## Uncertainty that still has a valid purpose

- **Combat and civilian danger:** visible threats and damage ranges do not predict
  enemy decisions or unseen units. Both manuals describe combat previews as
  probable outcomes ([Civ V, combat](https://downloads.2kgames.com/civ5/site13/community/feature_manual/Civ_V_Manual_English_v1.0.pdf);
  [Civ VI, p. 80](https://cdn.akamai.steamstatic.com/steam/apps/289070/manuals/CIV_VI_25TH_ONLINE_MANUAL_ENG.pdf)).
  Current fields distinguish visible support, unseen tiles and capture risk;
  absence of a listed threat is not a guarantee of safety.
- **Air missions:** hidden interceptors and nuclear collateral can change the
  result or final legality. Current air-sweep wording treats all interception
  information as unknown, even though some threats may be visible. Civ V uses
  sweeps/interception; Civ VI also makes deployed fighter patrols visible
  ([V, pp. 121–122](https://downloads.2kgames.com/civ5/site13/community/feature_manual/Civ_V_Manual_English_v1.0.pdf);
  [VI, air combat](https://cdn.akamai.steamstatic.com/steam/apps/289070/manuals/CIV_VI_25TH_ONLINE_MANUAL_ENG.pdf)).
  Consider separating observable threats from unknown outcomes if this surface
  becomes a priority; do not promise hidden victim/interceptor details.
- **Construction and research:** completion times assume current output. Building
  deltas hold population and citizen assignments fixed; completion triggers are
  described separately. These are conditional forecasts. No new visibility gate
  or rules conflict was identified in this pass.
- **Remembered resources and infrastructure:** unseen development stays stale.
  Resource-revealing technologies already update explored resource knowledge;
  this is not another blanket requirement to scout everything again.
- **Victory and rival intelligence:** capital identity/location, rival milestones,
  rankings and charts have distinct disclosure rules. No new blanket requirement
  to see all capitals before planning a war was found. Civ VI has a separate
  diplomatic-visibility/gossip system; copying its extra intelligence would be a
  feature choice rather than correcting a generic fog flag
  ([Firaxis designer explanation](https://store.steampowered.com/news/app/289070/view/2914348127396684246)).

## Settlement change validation

The focused suites covered 35 Kotlin integration tests and 101 Python tests.
Two assertions expecting the old settlement privacy behavior were updated, and
the affected 19-test Kotlin suite then passed. The remaining selected Kotlin
tests passed on the first run. The live-exercise fixture was generated and loaded
successfully, without launching a model.

The coastal regression marks `(-2,-11)` blocked, keeps the undiscovered city
anonymous, offers `(-3,-11)`, and successfully moves and founds there. Hidden city
creation/destruction, hidden ownership changes, remembered identities, unknown
destinations, recommendation filtering and state updates are also covered.

No compatibility aliases or migration were added. Older host checkpoints embed
the previous settlement snapshot layout and cannot be resumed with this build.
No existing game was restarted or edited.

## Approved follow-up implementation

- Explicit remote reveals record permanent map/city facts at the reveal event.
- Expansion happiness estimates use remembered terrain, retain general costs and
  building possibilities under fog, and qualify unresolved site conditions.
- Demographics retain anonymous numeric best/worst values while hiding unmet names.
- Movement routes distinguish unexplored tiles from tiles outside current sight.
- City-state quests retain explicitly supplied names without granting contact,
  location or actionable civilization IDs.
- Air previews describe known collateral and visible interception capabilities,
  while keeping private readiness and actual outcomes unresolved.
- Checkpoint version 7 omits derived turn overviews and observation revisions.
  Restart rebuilds observations; durable receipts still replay completed actions.
  Older formats require a new game, with no compatibility or migration layer.

### Follow-up validation

- All 27 selected native tests pass: explicit reveals and improvement-memory
  ordering, supplied-terrain happiness calculations, visible interception
  capabilities, private readiness, and remembered nuclear ownership warnings.
- The final 55-test host suite passes for communication, air missions,
  city-states, naval movement, observation opportunities, settlement previews and
  victory statistics. Separate recovery reruns also passed interrupted checkpoint
  replacement, completed receipt replay, fresh revisions, notification handles,
  turn reviews, scenario loading and terminal results.
- The Python suite has 890 passing tests after updating its readiness test double;
  two installed-model checks remain opt-in. The final preload/observation subset
  passes all 94 tests. Ruff lint/format checks and Pyrefly pass.

The wider checks caught and fixed an empty planning-map error for defeated players
with no retained terrain, plus terrain-cache initialization for detached remote
reveal estimates. No live game was launched, resumed or edited.
