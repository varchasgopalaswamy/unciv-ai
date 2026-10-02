# Player API coverage ledger

Audited September 30, 2026 against host `2c2b539b0b155848791c44c2dfa324c573568154`
and pinned engine `16bbd4b0ba05d6da57c48723cf29d84bcdb99857`.
Updated with the September 30 working-tree implementation and regressions below.

This source-backed inventory supersedes the remaining-work lists in older feature
slices. Implementation, regression evidence and full-game acceptance are separate
claims. An implemented command does not establish every ruleset combination,
complete UI parity, or an exhaustive privacy boundary.

## Scope and method

The audit starts with native UI controls, not just APIs already implemented:

- All **43 unit-action types**, additional movement/combat and promotion controls.
- All **11 ordinary turn requirements**, **30 popup types**, turn menus and autoplay.
- City/construction, technology, policy and Great Person pickers and their information.
- All **13 trade categories**, major/minor diplomacy, **10 city-state operation kinds**,
  and **18 quest kinds including `None`**.
- All **9 empire overview tabs**, **7 victory tabs**, map inspection and Civilopedia.
- Setup, save/load/Undo, multiplayer, desktop integration and service boundaries.
- All **61 host command kinds**, Python command schemas, selective reads, preload,
  communication routes and existing regression sources.

Host and Python command-kind sets match. Every enumerated entry above is
accounted for below; this is not a percentage-of-gameplay score. Bulk shortcuts
that existing commands can reproduce are distinguished from missing operations.

Baseline: bundled Vanilla, with Gods & Kings gaps identified separately. Mod-only
mechanics, map editing, cheats, audiovisual settings and reproducing GUI widgets
are outside current scope. Generic engine extension points are recorded so they
are not mistaken for supported bundled gameplay. The pinned checkout's code and
assets were inspected together. No running game was modified.

### Status and evidence

| Status | Meaning |
| --- | --- |
| Available | Host and model-facing route exists for the stated behavior; limits and evidence still apply. |
| Partial | Some choices, information, or a required follow-up are missing. |
| Missing | No player route for the native control/information was found. |
| Composed | Repeating existing commands performs the workflow; no bulk shortcut. |
| Engine/UI | Automatic effect or presentation/navigation, not a separate mutation. Underlying information is audited separately. |
| Extension | Unsupported generic engine capability not found in bundled content; mods remain out of scope. |

Evidence codes refer to the [verification inventory](#verification-inventory).
They identify reviewed implementation/test sources, not fresh test results or a
claim that every row has full positive, negative, privacy and desktop coverage.
Native-only tests do not close a missing host route or mandatory follow-up.

## Main findings

### Controls completed in this pass

- `SWAP_UNITS`, with both directions' civilian capture-risk advice and explicit override.
- Persistent `CONNECT_ROAD`, inspected through `player_inspect` subject `ROAD`;
  stop through the existing `STOP_AUTOMATION` order.
- Independent `SET_UP` and `GUARD` orders, unit naming, city/unit default promotions,
  and gifts to eligible major civilizations as well as city-states.
- Explicit ordinary-picker deferral through `END_TURN(deferChoices=true)` and
  `MOVE_AUTOMATED_UNITS` when the native timing setting permits it.
- `AUTOPLAY(ALL|MILITARY|CIVILIANS|ECONOMY)` automates the current turn. It does not
  advance the turn: inspect resulting decisions, then end it. Repeated autoplay
  and end-turn commands compose continuous autoplay; there is no independent
  scheduler that silently advances player turns.
- Native relocating/domain-changing upgrades, including a disclosed destination
  and relocation confirmation. Unexplored destination identities remain hidden.

### Information completed in this pass

Compact city border progress, resistance/razing/luxury/celebration status, temporary
unit effects and civilization-specific improvements are part of the existing
observation and model projections. Wonder estimates remain under
`wonderOpportunities` in the overview.

The bounded `player_inspect` read supplies the larger on-demand surfaces:

| Subject | Information |
| --- | --- |
| `CIVILOPEDIA` | Search/category index and named mechanical reference entries; flavor prose/quotes excluded. |
| `TILE` | Visible tile yield breakdown, defense percentage and fresh water; optional own-city context. |
| `UNIT` | Owned or currently visible unit reference, stats, promotions and temporary effects; no foreign orders. |
| `POLITICS` | Disclosed players' eras, policy-branch counts and third-party wars/friendships/pacts/denunciations. |
| `WONDERS` | Native built/known/unlocated status, with permitted owners and locations. |
| `NOTIFICATIONS` | The player's retained native notification history, optionally by turn. |
| `REPLAY` | Native territorial history within the player's currently explored map, from turn 5. |
| `ROAD` | Native road/rail path, per-tile jobs, worker-turns and current maintenance/connection effects. |

Paged lists accept `offset` and `limit` (maximum 100). Civilopedia named reads accept
up to 16 entries. Road work excludes travel/interruptions; complete numerical
estimates require the route to be currently visible. These larger reads are not
included wholesale in every turn preload.

Construction estimates now separate passive city-yield comparisons from `oneTime`
completion grants: stored yields, free choices/units/buildings, population and
Golden Age turns. Native completion runs in a detached world; estimates describe
completion now, not future decisions or guaranteed eventual outcomes.

### Broken or incomplete expansion flows

- Religion, espionage and faith purchases remain unsupported.
- **Diplomatic marriage is repaired.** Elimination is represented explicitly in
  the receipt, without requiring the defeated minor in the after-state. Transferred
  cities receive the native annex/puppet/destroy decision. Native tests cover the
  dispositions, and the host test covers replay and recovery with the choice pending.
- Generic `Event` choices are unsupported. Bundled event entries are floating
  tutorial tasks; no bundled gameplay-choice events were found.

### Integration and acceptance

Embedded desktop-human play, broader setup/controller replacement, OS/filesystem/
network isolation and hard monetary spending caps remain unfinished. One More Turn
and native multiplayer resignation have no player API. Ancient-to-victory live
acceptance remains open in the [roadmap](../Roadmap.md).

## 1. Identity, API layers and turn flow

Sources: [requirements][requirements], [turn/menu actions][next-actions],
[Next Turn menu][next-menu], [autoplay][autoplay], [session][session],
[command contract][commands], [MCP tools][tools], [gateway][gateway],
[Python schemas][schemas], [startup][startup]. Evidence: **E-BOUNDARY**, **E-TURN**.

| Surface | Status | Route and limits |
| --- | --- | --- |
| Own-major-player identity | Available | Authenticated binding, opaque handles, observed revisions, activation fencing/revocation. General controller replacement is separate. |
| Public match rules | Partial | Ruleset, difficulty, speed, victories and turn limit; not every native match setting. |
| Turn, decisions, owned entities, terminal result | Available | `player_turn`, `player_result`; model overview/selective reads and final delivery without another turn. |
| Capability/tool discovery | Available | `player_capabilities`, compact tool index/schema discovery and copyable commands; `completeGameplay=false`. |
| Native helper reads | Available | `player_summary`, `owned_cities`, `free_great_person_choice`; redundant helpers are omitted from the model gateway. |
| Receipts and updates | Available | `player_receipt`, exact-request outcomes, `stateUpdate`, invalidated details. Service bookkeeping does not require model action. |
| Ordinary end turn | Available | `END_TURN`; required choices/capture blockers and review of deliberate unit idling. |
| Explicit native menu advancement | Available | `END_TURN(deferChoices=true)` bypasses ordinary deferrable pickers when offered; modal decisions remain blocking. |
| Move automated units now | Available | `MOVE_AUTOMATED_UNITS` follows the native timing setting and once-per-turn flag. |
| Autoplay all/military/civilians/economy | Available | `AUTOPLAY` applies the selected native automation to the current turn. Continuous autoplay is composed with explicit end-turn commands. |
| Cycle/select entities and locations | Engine/UI | Handles/coordinates/rosters replace graphical selection/cycling. |

### Every ordinary turn requirement

| Native `Kind` | Status | Resolution |
| --- | --- | --- |
| `FreeGreatPerson` | Available | `CHOOSE_FREE_GREAT_PERSON`; native placement/options and Maya limitations. |
| `Construction` | Available | `QUEUE_CONSTRUCTION` and queue editing. |
| `Research` | Available | `SET_RESEARCH_QUEUE`, `CHOOSE_FREE_TECHNOLOGY`. |
| `Policy` | Available | `ADOPT_POLICY`, `DISMISS_POLICY_PICKER`. |
| `MoveSpies` | Missing | No assignment or native prompt-dismissal operation. |
| `FoundPantheon` | Missing | No picker. |
| `ExpandPantheon` | Missing | No additional pantheon-belief picker. |
| `FoundReligion` | Missing | No founding/name/symbol/belief picker. |
| `EnhanceReligion` | Missing | No enhancement picker. |
| `ReformReligion` | Missing | No free/reformation-belief picker. |
| `DiplomaticVote` | Available | `CAST_DIPLOMATIC_VOTE`, `ABSTAIN_DIPLOMATIC_VOTE`. |

Popup alerts, trade requests and diplomatic results are additional pending items.
Their support is inventoried below; unsupported items are not silently cleared.

## 2. Units, movement and combat

Sources: [entire action enum][unit-enum], [UI factories][unit-ui],
[unique-action factories][unique-ui], [promotion picker][promotion-ui],
[unit orders][orders], [combat operations][combat-ops]. Evidence: **E-MOVE**,
**E-ORDER**, **E-WORK**, **E-COMBAT**, **E-ECONOMY**, **E-GP**, **E-NAVAL**, **E-AIR**.

### Every native unit-action type

| `UnitActionType` | Status | Player route or gap |
| --- | --- | --- |
| `StopEscortFormation` | Available | `SET_UNIT_ORDER`: `STOP_ESCORT`. |
| `EscortFormation` | Available | `SET_UNIT_ORDER`: `ESCORT`; same-tile civilian/military pairing and shared travel. |
| `SwapUnits` | Available | `SWAP_UNITS`, advertised partners/destinations, native movement and civilian capture-risk confirmation. |
| `Automate` | Available | `SET_UNIT_ORDER`: `AUTOMATE`, including workers. |
| `ConnectRoad` | Available | `player_inspect(ROAD)` then `CONNECT_ROAD`; native road/rail automation persists and can be stopped. |
| `StopAutomation` | Available | `SET_UNIT_ORDER`: `STOP_AUTOMATION`. |
| `StopMovement` | Available | `CANCEL_UNIT_MOVEMENT`. |
| `ShowUnitDestination` | Engine/UI | Destination exposed in `movementOrder`; camera recentering is presentation. |
| `Sleep` | Available | `SET_UNIT_ORDER`: `SLEEP`. |
| `SleepUntilHealed` | Available | `SET_UNIT_ORDER`: `SLEEP_UNTIL_HEALED`. |
| `Wake` | Available | `SET_UNIT_ORDER`: `WAKE` for offered sleeping/fortified/skipped/guarding states. |
| `Fortify` | Available | `SET_UNIT_ORDER`: `FORTIFY`; native defense/healing. |
| `FortifyUntilHealed` | Available | `SET_UNIT_ORDER`: `FORTIFY_UNTIL_HEALED`. |
| `Guard` | Available | `SET_UNIT_ORDER`: `GUARD`, for native eligible withdrawal units. |
| `Explore` | Available | `SET_UNIT_ORDER`: `EXPLORE`; persistent, can schedule without movement. |
| `StopExploration` | Available | `SET_UNIT_ORDER`: `STOP_EXPLORATION`. |
| `Promote` | Available | `PROMOTE_UNIT`; prerequisites, XP, effects, instant healing; paths composed step by step. |
| `Upgrade` | Available | `UPGRADE_UNIT` uses native placement for in-place and relocating/domain-changing targets; cost, destination and confirmation are inspected first. |
| `Transform` | Extension | No generic `CanTransform` route; no bundled use found. |
| `Pillage` | Available | `PILLAGE`; improvement/road, cost/healing, confirmation and native result. |
| `Paradrop` | Available | `mission_preview` + `PARADROP`. |
| `AirSweep` | Available | `mission_preview` + `AIR_SWEEP`. |
| `SetUp` | Available | `SET_UNIT_ORDER`: `SET_UP`; `ATTACK` also sets up as necessary. |
| `FoundCity` | Available | `FOUND_CITY`; native eligibility and nonbinding promise advice. |
| `ConstructImprovement` | Available | `START_WORKER_JOB`; ordinary picker, clearing/road/rail and prerequisite queues. |
| `Repair` | Available | `START_WORKER_JOB` with offered repair choice. |
| `CreateImprovement` | Partial | GP improvements through `ACTIVATE_GREAT_PERSON`; Work Boats through `CREATE_WATER_IMPROVEMENT`. Other non-GP instant uniques lack a general route. |
| `HurryResearch` | Available | `ACTIVATE_GREAT_PERSON`; native science/free-tech behavior and subsequent choices. |
| `HurryPolicy` | Available | `ACTIVATE_GREAT_PERSON` where offered; native culture grant. |
| `HurryWonder` | Available | `ACTIVATE_GREAT_PERSON`; native production/consumption. |
| `HurryBuilding` | Available | `ACTIVATE_GREAT_PERSON`; native non-wonder hurry constraints. |
| `ConductTradeMission` | Available | `ACTIVATE_GREAT_PERSON`; city-state gold/influence. |
| `FoundReligion` | Missing | Religious action and subsequent picker. |
| `TriggerUnique` | Partial | Consuming GP Golden Age abilities work; no arbitrary triggered-action route. |
| `SpreadReligion` | Missing | Conversion action/uses/targeting. |
| `RemoveHeresy` | Missing | Inquisitor action. |
| `EnhanceReligion` | Missing | Religious action and subsequent picker. |
| `DisbandUnit` | Available | `DISBAND_UNIT`; refund and passenger-evacuation confirmation. |
| `GiftUnit` | Available | `GIFT_UNIT` to eligible known major/minor recipients. `unit.gift` describes native opinion/influence, availability and consumption. |
| `Skip` | Available | `SET_UNIT_ORDER`: `SKIP`, changing due state rather than spending movement. |
| `ShowAdditionalActions` | Engine/UI | API has no action-page restriction. |
| `HideAdditionalActions` | Engine/UI | Presentation only. |
| `AddInCapital` | Available | `ADD_SPACESHIP_PART`; native capital requirements/consumption. |

### Controls outside that enum

| Surface | Status | Route and limits |
| --- | --- | --- |
| Current-turn destination movement | Available | `MOVE_UNIT`; engine pathfinding/offered paths, not mandatory tile-by-tile movement. |
| Multi-turn destination | Available | `movement_preview` + `SET_UNIT_DESTINATION`, persisted native order/continuation and discovery/blocker reporting. |
| Formation travel | Available | Shared stops, individual movement costs, pairing/unpairing/partner loss. |
| Civilian danger override | Available | Known visible capture risk needs per-action `confirmAction`; optional override, not a permanent rule. Camps/fog/sightings remain advisory. |
| Embark/disembark, coast/ocean | Available | Native movement and technology/promotion restrictions; no separate embark command needed. |
| Aircraft rebase/carrier travel | Available | `REBASE_UNIT`, native capacities and own bases/carriers. No unspecified “transport variants” asserted without a concrete case. |
| Melee/ranged/air/missile combat | Available | `ATTACK`; visible origins/targets, paths, modifiers, damage ranges/interception warnings and actual outcomes. |
| City bombardment | Available | `CITY_ATTACK`; range, health, targets and remaining attacks. |
| Nuclear strike | Available | `mission_preview` + `NUCLEAR_STRIKE`; explicit confirmation and filtered blast/war results. |
| Interception | Engine/UI | Automatic; air details report readiness, range, chance and remaining interceptions. |
| Worker job cancellation | Available | `STOP_WORKER_JOB`; native eligibility/progress. |
| Rename unit | Available | `RENAME_UNIT`; empty selection clears the custom name. |
| Default city/unit-type promotions | Available | `SAVE_UNIT_PROMOTIONS`, `SET_DEFAULT_PROMOTIONS`, unit controls and city production details. |
| Temporary statuses | Available | Names, mechanical effects and remaining turns in the unit overview, promotion details and `player_inspect(UNIT)`. |
| Batch upgrade/multi-unit move/promotion path | Composed | Repeated inspected commands; no API bulk shortcut. Recheck eligibility/cost after each. |
| Ruins/prizes/free-upgrade triggers | Engine/UI | Native automatic effects execute. Subsequent ordinary decisions/popups need their own coverage. |

## 3. Cities, construction, research and policies

Sources: [city screen][city-ui], [citizen controls][citizen-ui],
[construction UI][construction-ui], [statistics][city-stats-ui], [tile controls][city-tile-ui],
[purchases][buy-ui], [technology][tech-ui], [policies][policy-ui],
[host development][city-development], [management][city-management],
[construction estimates][estimates], [decision details][decisions], [selective sections][sections].
Evidence: **E-CITY**, **E-ECONOMY**, **E-GP**, **E-OVERVIEW**, **E-CAPTURE**.

| Surface | Status | Route and limits |
| --- | --- | --- |
| Rename city | Available | `RENAME_CITY`; native name validation. |
| Work/unwork/lock/unlock tiles | Available | `SET_WORKED_TILE`, `SET_TILE_LOCKED`; population, visibility, blockade/ownership. |
| Transfer workable tile between own cities | Available | `REASSIGN_CITY_TILE`; worker/lock changes, not territorial transfer. |
| Focus, growth, reset | Available | `SET_CITY_FOCUS`, `SET_AVOID_GROWTH`, `RESET_CITIZENS`; offered native focus types. |
| Specialists | Available | `SET_MANUAL_SPECIALISTS`, `ASSIGN_SPECIALIST`, `UNASSIGN_SPECIALIST`; capacity/yields/GP points. |
| Construction discovery | Available | `construction_details`; named rules/requirements/effects, catalogue/known names, progress/costs. Unknown and blocked names distinguished. |
| Queue append/remove/reorder | Available | `QUEUE_CONSTRUCTION`, `REMOVE_CONSTRUCTION`, `MOVE_CONSTRUCTION`; indexed duplicates/stored work. |
| Queue top/all-city shortcuts | Composed | Append/reorder/remove per city; no bulk command. |
| Perpetual conversions | Available | Ordinary queue choices and projected native Science/Gold output. |
| Buy units/buildings | Available | `BUY_CONSTRUCTION` with gold, native availability/placement/price/confirmation. |
| Buy tiles | Available | `BUY_TILE`; contiguity/price/ownership. Buy-ring shortcut is composable. |
| Sell buildings | Available | `SELL_BUILDING`; refund/restrictions/specialist effects/confirmation. |
| Faith/other-currency purchases | Missing | Operations select `Stat.Gold`. |
| Construction improvement-placement picker | Extension | `CreatesOneImprovement` selection absent; no matching bundled construction found. |
| City-specific estimates | Available | Ongoing yields, maintenance, specialist slots/GP points, growth, completion time, queue-switch effects and separate numerical one-time completion grants. No future-decision simulation. |
| Options unlocked since current selection | Available | `constructionChoice`; wonder estimates stay in `wonderOpportunities`. |
| Built buildings | Available | `city_details` economy/all; omitted from compact city preload. Access exists, but presentation contributed to repeated recommendations. |
| Population/food/yields/citizens | Available | Growth/starvation/storage/focus and detailed yield sources. |
| Border expansion | Available | Stored culture, native cost, rate, ETA and permitted next tile in city development/overview. |
| Occupation/razing status | Available | Puppet/resistance state, explicit razing status, resistance duration and native management choices. |
| City luxury demand/WLTKD | Available | City demand and remaining celebration turns, plus empire resource counts. |
| City-local/stockpiled resources | Extension | No full native city-resource display; not bundled Vanilla resource gameplay. |
| City strength/health/garrison | Available | Health/strength/attacks; garrison identifiable from own unit on center tile. |
| Captured-city disposition | Available | `RESOLVE_CAPTURED_CITY`: native annex/puppet/raze/liberate/destroy options. |
| Later annex/raze/stop razing | Available | `MANAGE_CAPTURED_CITY`. |
| Traded-city disposition | Available | `CityTraded` keep/liberate through capture API, without replaying conquest. |
| Recaptured civilian return/keep | Available | Capture API supplies native unit type/rewards; current recapture facts also in tactical overview. |
| Marriage disposition | Available | Native annex/puppet/destroy choice without replaying conquest effects; pending choice survives recovery. |
| Technology tree/details | Available | `research_details`: eras, prerequisites, costs, effects, native ordered paths. |
| Research progress/unlocks | Available | Current tech, science/overflow/stored progress and unlock summaries. |
| Research queue/free choice | Available | `SET_RESEARCH_QUEUE`, `CHOOSE_FREE_TECHNOLOGY`; explicit replacement, native prerequisites/repeatables. |
| Policies | Available | `policy_details`, `ADOPT_POLICY`; era/prerequisites/effects/costs/branch completion. |
| Policy progress/postponement | Available | Empire culture/rate/ETA/branches; `DISMISS_POLICY_PICKER`. |
| Free Great Person | Available | `CHOOSE_FREE_GREAT_PERSON`; native choices, Maya limits, placement failure/retry. |
| GP accumulation | Available | Empire rates/thresholds and city specialist/source details; no missing religious decisions implied. |

## 4. Diplomacy, trade and city-states

Sources: [major UI][major-ui], [minor UI][minor-ui], [demands][demands],
[trade categories][trade-enum], [native generation][trade-logic],
[host diplomacy][diplomacy], [native response projection][native-diplomacy],
[trade allowlist][trade-terms], [AI bridge][native-trade], [communication][communication],
[model schemas][tool-schemas], [minor observations][minors]. Evidence: **E-DIPLOMACY**,
**E-TRADE**, **E-MINOR**.

| Surface | Status | Route and limits |
| --- | --- | --- |
| Own relationships | Available | Known majors, war/peace/treaty turns, own agreements, available actions and declaration warnings. |
| Declare war | Available | `DECLARE_WAR`; certain native ultimatum/incident responses also declare war, explicitly marked. |
| Peace/economic negotiations | Available | `trade_options`, `trade_read`, `trade_offer`, `trade_decide`; trade-menu terms use this flow. |
| Friendship | Available | `friendship_offer`, native AI bridge and `RESPOND_FRIENDSHIP` where a native popup is offered. |
| Denunciation | Available | `chat_send.nativeAction` = `DENOUNCE`; native effects/eligibility. |
| Issue demands | Available | `ISSUE_DEMAND` for settling/religion/spying when native UI permits. Native `DoNotAttackUs` is AI-initiated only, not a missing outgoing human action. |
| Respond to demands/incidents | Available | `RESPOND_DIPLOMACY` through chat for majors; `RESPOND_CITY_STATE_ALERT` for minor senders. Mechanical consequences preserved. |
| Native opinions/promises | Available | Built-in-AI modifiers/relationship, promise/denunciation durations; social commitments remain nonbinding statements. |
| Agent–agent / agent–human dialogue | Available | Public/private chat, explicit participants, versioned commitments and shared trade; human CLI exists. |
| Agent–built-in-AI offers | Available | Native bridge through trade tools, handled on gameplay turns; native AI resolves on its turns. Unsupported/private terms decline-only. |
| Counter/decline/withdraw pending offers | Available | Versioned choices; native request handling and stale-term protection. |
| Enacted agreements | Available | `agreement_list`, `agreement_read`; native expiry/war/resource-loss endings and history. |
| Voluntarily cancel enacted fixed-term deal | Engine/UI | No ordinary native cancellation control found; not a missing host rule/action. |
| Third-party relations | Available | `player_inspect(POLITICS)` for disclosed endpoints; no unmet identities or private offers/modifiers. |

### Every native trade category

| `TradeOfferType` | Status | Coverage |
| --- | --- | --- |
| `Embassy` | Available | Native embassy term where rules/technology permit. |
| `Gold` | Available | Immediate gold, including one-sided gifts. |
| `Gold_Per_Turn` | Available | Native amount/duration. |
| `Treaty` | Available | Peace Treaty, Research Agreement, Defensive Pact; not arbitrary generic treaty names. |
| `Agreement` | Available | Open Borders; not arbitrary generic names. |
| `Luxury_Resource` | Available | Native quantities/supply/duration/consent. |
| `Strategic_Resource` | Available | Native quantities/supply/duration/consent. |
| `Stockpiled_Resource` | Extension | Rejected/decline-only; no bundled stockpiled trade found. |
| `Technology` | Extension | Rejected/decline-only; native enum/acceptance branch exists, ordinary offer generation does not offer it. |
| `Introduction` | Extension | Rejected/decline-only; native offer generation gated by a mod-option unique absent from bundled rules. |
| `WarDeclaration` | Available | Paid/joint third-party war with permitted target identities/native consequences. |
| `PeaceProposal` | Available | Native-permitted mediated peace with built-in-AI third party; no consent invented for human/agent third parties. |
| `City` | Available | Native eligibility/effects and subsequent disposition; hidden raw IDs not exposed. |

### Every city-state operation kind

| `PlayerCityStateOperations.Kind` | Status | Route |
| --- | --- | --- |
| `GiftGold` | Available | `CITY_STATE_ACTION`; native 250/500/1000 gifts, current funds/influence. |
| `GiftImprovement` | Available | `CITY_STATE_ACTION`; offered resource improvement, cost/prerequisites. |
| `PledgeProtection` | Available | `CITY_STATE_ACTION`; native cooldown. |
| `RevokeProtection` | Available | `CITY_STATE_ACTION`; native cooldown/consequences. |
| `GoldTribute` | Available | `CITY_STATE_ACTION`; willingness/modifiers/current payout. |
| `WorkerTribute` | Available | `CITY_STATE_ACTION`; willingness/population/placement. |
| `MakePeace` | Available | `CITY_STATE_ACTION`; war/ally/treaty restrictions. |
| `DeclareWar` | Available | `CITY_STATE_ACTION`; native consequences. |
| `DiplomaticMarriage` | Available | Native transfer, eliminated-minor receipt, and mandatory annex/puppet/destroy follow-up; replay/recovery covered. |
| `GiftUnit` | Available | `GIFT_UNIT` to eligible known major/minor recipients. `unit.gift` describes native opinion/influence, availability and consumption. |

Minor observations include influence/resting point/decay, ally/protectors with unmet
identities redacted, bonuses/resources/unique unit, tribute modifiers, own quests,
contest standings and defense requests. Not every quest completion or civilization
bonus has end-to-end host acceptance evidence.

### Every quest kind

Recognized quests have structured objectives, rewards, timing and permitted target
data. Status describes ability to pursue the quest, not a completed exercise for
every case. `None` retains its original description instead of an invented objective.

| `QuestName` | Status | Completion path / gap |
| --- | --- | --- |
| `None` | Partial | Unrecognized quest; original text, no completion guarantee. |
| `Route` | Composed | Native connection through road segments/automation; connect-road order missing. |
| `ClearBarbarianCamp` | Available | Movement/combat at specified camp. |
| `ConstructWonder` | Available | Named wonder construction. |
| `ConnectResource` | Available | Resource supply through improvement or trade. |
| `GreatPerson` | Available | Acquire required type/replacement. |
| `ConquerCityState` | Available | Native war/combat/elimination. |
| `FindPlayer` | Available | Discover target territory; contact alone is insufficient. |
| `FindNaturalWonder` | Available | Exploration. |
| `GiveGold` | Available | Affordable gift to issuer; named bully is not recipient. |
| `PledgeToProtect` | Available | Pledge to issuer, not named bully. |
| `ContestCulture` | Available | Native generation and standings. |
| `ContestFaith` | Partial | Objective/standings; faith may accrue, but religious gameplay/purchases absent. |
| `ContestTech` | Available | Research and standings. |
| `Invest` | Available | Gift-influence bonus; not a separate completion action. |
| `BullyCityState` | Available | Tribute from named target. |
| `DenounceCiv` | Available | Native target denunciation. |
| `SpreadReligion` | Missing | Objective exposed; no religious unit action. |

## 5. Alerts and notifications

All [30 native popup types][alert-enum] are listed. Sources: [host dispatch][session],
[informational operations][popup-ops], [capture operations][capture-ops],
[diplomatic responses][diplomatic-ops], [desktop popups][alert-ui].
Evidence: **E-ALERT**, **E-CAPTURE**, **E-DIPLOMACY**, **E-MINOR**.

| `AlertType` | Status | Handler |
| --- | --- | --- |
| `Defeated` | Available | Informational/terminal content and final result. |
| `WonderBuilt` | Available | Effects and `ACKNOWLEDGE_ALERT`. |
| `TechResearched` | Available | Civilization-specific effects and acknowledgement. |
| `WarDeclaration` | Available | Known sender/event and acknowledgement. |
| `FirstContact` | Available | Permitted identity and acknowledgement. |
| `CityConquered` | Available | `RESOLVE_CAPTURED_CITY`. |
| `CityTraded` | Available | Keep/liberate through capture API. |
| `BorderConflict` | Available | Native response, including minor sender route. |
| `TilesStolen` | Available | Native response, including minor sender route. |
| `DemandToStopSettlingCitiesNear` | Available | Native accept/refuse. |
| `CitySettledNearOtherCivDespiteOurPromise` | Available | Native response/acknowledgement. |
| `DemandToStopSpreadingReligion` | Available | Response implemented; religious gameplay absent. |
| `ReligionSpreadDespiteOurPromise` | Available | Native response. |
| `DemandToStopSpyingOnUs` | Available | Response implemented; espionage gameplay absent. |
| `SpyingOnUsDespiteOurPromise` | Available | Native response. |
| `DemandToNotAttackUs` | Available | Accept/refuse; refusal explicitly declares war. |
| `AttackedUsDespitePromise` | Available | Native response. |
| `AcceptingDemand` | Available | Native acknowledgement. |
| `RejectingDemand` | Available | Native acknowledgement. |
| `GoldenAge` | Available | Acknowledgement; duration/progress in economy. |
| `DeclarationOfFriendship` | Available | `RESPOND_FRIENDSHIP` or bridge-owned proposal flow. |
| `StartIntro` | Available | Acknowledgement and mechanical civilization profile. |
| `DiplomaticMarriage` | Available | Native transfer, eliminated-minor receipt, and mandatory annex/puppet/destroy follow-up; replay/recovery covered. |
| `BulliedProtectedMinor` | Available | Native incident response, unmet minor redaction, explicit consequences. |
| `AttackedProtectedMinor` | Available | Native response/possible war. |
| `AttackedAllyMinor` | Available | Native response/possible war. |
| `RecapturedCivilian` | Available | Return/keep, native unit type and exact reward operation. |
| `GameHasBeenWon` | Available | Terminal content/results. |
| `Event` | Extension | Generic event-choice UI has no API; bundled floating tutorial tasks do not add gameplay choices. |
| `Denounced` | Available | Native acknowledgement. |

Ordinary notifications use [PlayerNotification][notifications] and
`DISMISS_NOTIFICATIONS`: owned text/category/icons/event locations. Model projection
removes audited stock narrative/quotes but preserves mechanics and player-written
chat. `player_inspect(NOTIFICATIONS)` reads retained native history; arbitrary
UI-link execution remains unsupported. Host `history_read`/chat history store
different records. Evidence:
**E-NOTIFY**, **E-MODEL**.

## 6. Information, overviews and Civilopedia

Sources: [overview tabs][overview-enum], [global politics][politics-ui],
[wonders][wonders-ui], [tile descriptions][tile-description], [yield popup][tile-ui],
[Civilopedia categories][pedia], [map DTO][map], [economy][economy],
[mechanics][mechanics], [civilization profile][profile], [model projection][observations]
and [selective sections][sections]. Evidence: **E-MAP**, **E-OVERVIEW**, **E-MODEL**.

### Every empire overview tab

| Native tab | Status | Available / missing |
| --- | --- | --- |
| `Cities` | Available | Roster, population/yields/production/health/citizens, border progress and celebration/resistance/razing status. |
| `Stats` | Partial | Gold/happiness sources, supply, Golden Age, research/culture/GP progress; not full empire multi-stat source table/tooltips. |
| `Trades` | Available | Own agreements, bridged offers and shared history; unsupported generic categories remain explicit. |
| `Units` | Available | Own roster, movement/health/XP/promotions/upgrades/transport; naming, promotion defaults and temporary statuses. |
| `Politics` | Available | Own relations/minors plus `player_inspect(POLITICS)` for disclosed third-party relations, rival eras and policy-branch counts. |
| `Resources` | Partial | Ordinary supply/use/import/export/offers/source rows/unimproved sources and aggregate luxury demands; stockpile/local variants absent. |
| `Religion` | Missing | Religion/belief/follower/pressure overview. |
| `Wonders` | Available | `wonderOpportunities` for construction estimates; `player_inspect(WONDERS)` for native ownership/status/location. |
| `Notifications` | Available | Current messages/dismissal and paged retained native history via `player_inspect(NOTIFICATIONS)`. |

### Other information surfaces

| Surface | Status | Available / missing |
| --- | --- | --- |
| Explored map/local reads | Available | `map_region`, map index; explored terrain/rivers, revealed resources, ownership/cities, remembered improvements and dates. Unknown tile data omitted. |
| Fog/known cities | Available | Last observations persist; current city health/population/defense requires visibility. Original-major-capital marker exposed. |
| Settlement lens/recommendations | Available | Native eligibility on explored tiles including hidden-city spacing without identities; scores/crowded-map diagnostics before owning a Settler. Limited eligibility disclosure is intentional. |
| Expansion costs/benefits | Available | Known site resources/improvement techs, distinct luxuries, illustrative city costs and happiness-building options; not guaranteed future yields/availability. |
| Military/tactical context | Available | Own forces, visible threats, losses/captures, camps/dated sightings and individual settler protection. No claim of hidden enemy plans or exhaustive threat prediction. |
| Map wrap/routes | Available | Axial coordinates/wrap offset, native paths and estimated turn stops/uncertainty. |
| Full tile tooltip | Available | Map facts plus `player_inspect(TILE)` for visible yield sources, defense percentage and fresh water. Fogged live statistics are withheld. |
| Enemy unit inspection | Available | `player_inspect(UNIT,destination)` for visible stats, mechanical reference, promotions and statuses; private orders/XP excluded. |
| Civilization identity | Available | Ability, unique units/buildings/improvements, replacement differences, inherent promotions and technology/obsolescence conditions. |
| Unbuilt wonder comparisons | Available | `wonderOpportunities` is the canonical location for benefits/requirements/city estimates. |
| Unimproved-resource work | Available | Worker vs Work Boat, clearing/tech requirements and current jobs. |
| Road/rail connectivity | Available | Unlocks/unconnected cities and `player_inspect(ROAD)` for native route/work/maintenance/connection effects, followed by persistent `CONNECT_ROAD`. |
| General Civilopedia | Available | `player_inspect(CIVILOPEDIA)` searches all native reference categories and reads named mechanical entries without flavor text. |
| Sorting/filtering/camera/overlays | Engine/UI | Underlying facts matter; reproducing graphical widgets does not. Missing facts listed above. |

## 7. Religion, espionage and victories

[Religious unit actions][religious-ui] and pickers lack an API: pantheon selection,
religion name/symbol/beliefs, enhancement/additional beliefs, conversion/remove-heresy,
remaining uses, followers/pressure/holy-city detail and faith purchases. Automatic
faith/pressure does not establish player control.

[Espionage][espionage-ui] lacks a player API: spy roster/rank/activity/timers,
assignment/recall, stealing choices, counter-espionage, city-state election work,
coups and permitted intelligence. Diplomatic responses about these systems are
implemented separately.

All five Vanilla victory paths have native research/production/policy/combat routes.
Scientific victory includes producing, transporting and assembling every part;
diplomatic voting/abstention/results are implemented. Cultural Utopia, domination
and Time victory use native conditions. Prepared endgame tests are not an
Ancient-to-victory autonomous acceptance run. Evidence: **E-VICTORY**, **E-CAPTURE**.

### Every victory-screen tab and terminal control

Sources: [VictoryScreen][victory-ui], [progress][victory-progress],
[statistics][victory-statistics], [decisions][victory-decisions].

| Native surface | Status | Route / gap |
| --- | --- | --- |
| `OurStatus` | Available | `victory_progress`: paths/milestones, policy counts, capital objectives, spaceship parts. |
| `Global` | Available | Permitted known rivals/native next milestones; not arbitrary hidden progress. |
| `Illustration` | Engine/UI | Artwork is presentation; scientific assembly counts represented. Custom illustrations outside scope. |
| `Demographics` | Available | Own rank/value, native aggregates/permitted identities; settings enforced. Total score intentionally excluded. |
| `Rankings` | Available | Known living players/native metrics/short trends; Force includes native treasury adjustment. Total score intentionally excluded. |
| `Charts` | Available | Bounded/paginated history/current values with settings/visibility enforced. Total score intentionally excluded. |
| `Replay` | Available | `player_inspect(REPLAY,turn)` pages territorial history inside the currently explored map; undisclosed owners remain unnamed. |
| Vote/abstain/results | Available | `CAST_DIPLOMATIC_VOTE`, `ABSTAIN_DIPLOMATIC_VOTE`, `ACKNOWLEDGE_DIPLOMATIC_RESULTS`; private ballots/permitted results. |
| Win/lose result | Available | `player_result`, terminal delivery, host winner/all-major-player scores. |
| One More Turn | Missing | Native continuation flag/control absent. |
| Postgame exploration/spectating | Missing | No general player transition to unrestricted postgame exploration. |
| Resign/controller replacement | Missing | Native online-multiplayer resignation/AI replacement not integrated; not a generic Vanilla victory-screen button. |

## 8. Lifecycle, human play and services

Sources: [setup][setup], [game CLI][game-cli], [operator][operator],
[recovery](../architecture/Turn-recovery.md), [context](../architecture/Agent-context.md),
[background reviews](../architecture/Background-turn-memory.md),
[monitoring](../architecture/Monitoring.md). Evidence: **E-LIFECYCLE**, **E-RECOVERY**,
**E-MODEL**, **E-TELEMETRY**.

| Surface | Status | Route and limits |
| --- | --- | --- |
| Generated-game launch | Available | Seed, agent/built-in-AI civilization lists, map size/radius, city-state count; no longer restricted to Rome/Greece/France. |
| General setup | Partial | Launcher fixes Vanilla/Prince/Quick/Ancient/Pangaea; no complete ruleset/map-type/difficulty/speed/era/scenario/multiplayer setup UI. |
| Save/load/pause/resume | Available | Operator persistence/coordinated `game resume`; active-turn rewind and affected communication/private state. |
| Save/load/Undo as player commands | Missing | Native controls exist; agents lack arbitrary rollback/file access. Operator recovery is not an agent Undo API. |
| Lost action response | Available | Durable receipts/exact-request lookup; no blind repeat mutation. |
| Crash/timeout/interrupted turn | Available | Stop and operator resume at active-turn start; obsolete conversation-checkpoint continuation removed. |
| Interrupted background review | Available | Durable inputs/retry limits; other civs proceed, same civ waits for required review. |
| Opening/periodic strategy review | Available | Asynchronous reports, short/mid/long-term advice, preload/TUI display; not a native game rule. |
| Private memory/chat/action history | Available | Scoped records/contributions/provenance and database history; not native notification log. |
| Model configuration | Available | Per-civ model/reasoning, requested service priority, retained resume settings. |
| Human chat/trade | Available | CLI/shared communication and negotiation. |
| Embedded desktop-human gameplay | Missing | Inspection loads a saved copy, not a connected authoritative human player. |
| Native online multiplayer/controller lifecycle | Missing | Lobby/reconnect/hotseat/controller replacement/resignation integration incomplete. |
| Telemetry/dashboard | Available | Per-turn/job token kinds, API calls/errors, durations, model/strategy/diplomacy progress, final scores/winner. Database-backed views; explicit exports where retained. |
| Time/turn/invocation limits | Available | Scheduler/launcher limits and stop diagnostics. |
| Hard monetary cap | Missing | Token accounting is not an enforced dollar budget. |
| OS/filesystem/network isolation | Missing | API authority does not provide process/container isolation. |
| Mod manager/editor/cheats/audiovisual settings | Engine/UI | Outside ordinary strategic player API scope. |

## Verification inventory

These are regression sources for each area, not a claim that every named test was
rerun during this audit. Many host integration tests launch a corresponding probe
in an isolated engine process; a probe's presence alone is not live acceptance.
The fresh checks for this audit are recorded separately below.

| Evidence | Located implementation/regression evidence | What it supports / remaining limit |
| --- | --- | --- |
| **E-BOUNDARY** | [S06McpIntegrationTest](../../src/test/kotlin/uncivai/S06McpIntegrationTest.kt), [PlayerIdentifiersIntegrationTest](../../src/test/kotlin/uncivai/PlayerIdentifiersIntegrationTest.kt), [schema tests](../../tests/test_schemas.py) | Binding, request validation, opaque entity/alert IDs and per-player revisions; not a proof of every information boundary. |
| **E-TURN** | [PlayerOperationsTest](../../vendor/Unciv/tests/src/com/unciv/logic/civilization/PlayerOperationsTest.kt), [PlayerTurnIntegrationTest](../../src/test/kotlin/uncivai/PlayerTurnIntegrationTest.kt), [TurnReviewIntegrationTest](../../src/test/kotlin/uncivai/TurnReviewIntegrationTest.kt) | Native requirement handling, successful/stale/blocked commands and intentional idling. Unsupported requirements remain unsupported. |
| **E-MOVE** | [PlayerUnitOperationsTest](../../vendor/Unciv/tests/src/com/unciv/logic/civilization/PlayerUnitOperationsTest.kt), [PlayerMovementOrdersTest](../../vendor/Unciv/tests/src/com/unciv/logic/civilization/PlayerMovementOrdersTest.kt), [SettlementPreviewIntegrationTest](../../src/test/kotlin/uncivai/SettlementPreviewIntegrationTest.kt) | Native moves/founding, destination continuation and settlement eligibility; not swapping or all possible terrain/unique combinations. |
| **E-ORDER** | [PlayerUnitOrdersTest](../../vendor/Unciv/tests/src/com/unciv/logic/civilization/PlayerUnitOrdersTest.kt), [UnitAutomationProbe](../../src/test/kotlin/uncivai/UnitAutomationProbe.kt), [TacticalConsequencesIntegrationTest](../../src/test/kotlin/uncivai/TacticalConsequencesIntegrationTest.kt) | Standing orders, automation, escorts and cancellation; Guard, setup, swaps, road orders and empire automation now have additional E-COMPLETION coverage. |
| **E-WORK** | [PlayerWorkerOperationsTest](../../vendor/Unciv/tests/src/com/unciv/logic/civilization/PlayerWorkerOperationsTest.kt), [WorkerDevelopmentIntegrationTest](../../src/test/kotlin/uncivai/WorkerDevelopmentIntegrationTest.kt) | Offered jobs, clearing/prerequisites, work progress/cancellation; no whole-route planner. |
| **E-COMBAT** | [PlayerCombatOperationsTest](../../vendor/Unciv/tests/src/com/unciv/logic/civilization/PlayerCombatOperationsTest.kt), [AttackEventsViewBoundaryTest](../../vendor/Unciv/tests/src/com/unciv/view/AttackEventsViewBoundaryTest.kt), [CombatScenarioIntegrationTest](../../src/test/kotlin/uncivai/CombatScenarioIntegrationTest.kt) | Preview/execution, native consequences and event filtering; added setup and unit inspection are covered under E-COMPLETION. |
| **E-CITY** | [PlayerCityOperationsTest](../../vendor/Unciv/tests/src/com/unciv/logic/civilization/PlayerCityOperationsTest.kt), [PlayerCityManagementOperationsTest](../../vendor/Unciv/tests/src/com/unciv/logic/civilization/PlayerCityManagementOperationsTest.kt), [CityManagementRunIntegrationTest](../../src/test/kotlin/uncivai/CityManagementRunIntegrationTest.kt) | Research/construction/policy choices and city management; does not fill missing city timers/border information. |
| **E-ECONOMY** | [PlayerPurchaseOperationsTest](../../vendor/Unciv/tests/src/com/unciv/logic/civilization/PlayerPurchaseOperationsTest.kt), [PlayerUnitEconomyOperationsTest](../../vendor/Unciv/tests/src/com/unciv/logic/civilization/PlayerUnitEconomyOperationsTest.kt), [EconomyRunIntegrationTest](../../src/test/kotlin/uncivai/EconomyRunIntegrationTest.kt) | Gold purchases, sales, upgrades, disbanding and consequences; faith purchases remain absent; relocating upgrades are covered. |
| **E-GP** | [PlayerGreatPersonOperationsTest](../../vendor/Unciv/tests/src/com/unciv/logic/civilization/PlayerGreatPersonOperationsTest.kt), [GreatPersonSelectionTest](../../vendor/Unciv/tests/src/com/unciv/logic/civilization/GreatPersonSelectionTest.kt), [GreatPeopleRunIntegrationTest](../../src/test/kotlin/uncivai/GreatPeopleRunIntegrationTest.kt) | Native GP selection/placement/consumption and ordinary actions; religious GP actions are separate missing work. |
| **E-NAVAL** | [PlayerNavalOperationsTest](../../vendor/Unciv/tests/src/com/unciv/logic/civilization/PlayerNavalOperationsTest.kt), [NavalIntegrationTest](../../src/test/kotlin/uncivai/NavalIntegrationTest.kt), [feature reference](Naval-gameplay.md) | Embarkation, coastal/ocean access, boats and naval combat cases; not every unique. |
| **E-AIR** | [PlayerAirOperationsTest](../../vendor/Unciv/tests/src/com/unciv/logic/civilization/PlayerAirOperationsTest.kt), [AirIntegrationTest](../../src/test/kotlin/uncivai/AirIntegrationTest.kt), [feature reference](Air-gameplay.md) | Rebase/capacity, carriers, missions, interception and nuclear cases; not arbitrary transport extensions. |
| **E-CAPTURE** | [PlayerCaptureOperationsTest](../../vendor/Unciv/tests/src/com/unciv/logic/civilization/PlayerCaptureOperationsTest.kt), [CombatScenarioIntegrationTest](../../src/test/kotlin/uncivai/CombatScenarioIntegrationTest.kt), [TacticalConsequencesIntegrationTest](../../src/test/kotlin/uncivai/TacticalConsequencesIntegrationTest.kt) | Conquest/traded cities, recaptured civilians and surfaced consequences; including native marriage disposition (also E-COMPLETION). |
| **E-DIPLOMACY** | [PlayerDiplomaticCommunicationOperationsTest](../../vendor/Unciv/tests/src/com/unciv/logic/civilization/PlayerDiplomaticCommunicationOperationsTest.kt), [MixedDiplomacyProbe](../../src/test/kotlin/uncivai/MixedDiplomacyProbe.kt), [model diplomacy tests](../../tests/test_native_diplomacy.py) | Native demands/responses/war warnings and mixed controller routes; global politics inspection is covered under E-COMPLETION. |
| **E-TRADE** | [TradeCycleProbe](../../src/test/kotlin/uncivai/TradeCycleProbe.kt), [ThirdPartyTradeProbe](../../src/test/kotlin/uncivai/ThirdPartyTradeProbe.kt), [AgreementLifecycleProbe](../../src/test/kotlin/uncivai/AgreementLifecycleProbe.kt), [gateway tests](../../tests/test_trade_gateway.py) | Negotiation, consent, native terms, lifecycle and bridge behavior; unsupported generic categories stay explicit. |
| **E-MINOR** | [PlayerCityStateOperationsTest](../../vendor/Unciv/tests/src/com/unciv/logic/civilization/PlayerCityStateOperationsTest.kt), [CityStateIntegrationTest](../../src/test/kotlin/uncivai/CityStateIntegrationTest.kt), [quest/model tests](../../tests/test_city_states.py) | Operations, affordability, visibility and structured quest objectives. Host marriage coverage includes elimination, the pending choice, receipt replay and recovery; not every quest completion is exercised. |
| **E-ALERT** | [PlayerInformationalPopupTest](../../vendor/Unciv/tests/src/com/unciv/logic/civilization/PlayerInformationalPopupTest.kt), [InformationalAlertsIntegrationTest](../../src/test/kotlin/uncivai/InformationalAlertsIntegrationTest.kt) | Supported informational popup content/acknowledgement; not all 30 popup types. Other handlers have separate evidence. |
| **E-NOTIFY** | [NotificationsIntegrationTest](../../src/test/kotlin/uncivai/NotificationsIntegrationTest.kt), [notification tests](../../tests/test_notifications.py), [model projection tests](../../tests/test_model_view.py) | Current notifications, filtering/dismissal and narrative redaction; historical browsing is covered by E-COMPLETION. |
| **E-MAP** | [MapKnowledgeProbe](../../src/test/kotlin/uncivai/MapKnowledgeProbe.kt), [ObservationOpportunitiesIntegrationTest](../../src/test/kotlin/uncivai/ObservationOpportunitiesIntegrationTest.kt), [SettlementPreviewIntegrationTest](../../src/test/kotlin/uncivai/SettlementPreviewIntegrationTest.kt) | Explored/visible/remembered facts, settlement and development summaries; no universal visibility proof. |
| **E-OVERVIEW** | [ObservationOpportunitiesIntegrationTest](../../src/test/kotlin/uncivai/ObservationOpportunitiesIntegrationTest.kt), [BuildingEstimatesProbe](../../src/test/kotlin/uncivai/BuildingEstimatesProbe.kt), [BuildingYieldPreviewTest](../../vendor/Unciv/tests/src/com/unciv/logic/city/BuildingYieldPreviewTest.kt), [preload tests](../../tests/test_mechanics_preload.py) | Mechanical summaries and city-specific estimates; not every native overview tab or future strategy outcome. |
| **E-VICTORY** | [VictoryDecisionsIntegrationTest](../../src/test/kotlin/uncivai/VictoryDecisionsIntegrationTest.kt), [VictoryProgressIntegrationTest](../../src/test/kotlin/uncivai/VictoryProgressIntegrationTest.kt), [VictoryStatisticsIntegrationTest](../../src/test/kotlin/uncivai/VictoryStatisticsIntegrationTest.kt), [feature reference](Victory-and-turn-blockers.md) | Prepared victory conditions/decisions, parts, ballots, rankings/chart privacy; no full Ancient-to-victory acceptance claim. |
| **E-COMPLETION** | [CoverageCompletionProbe](../../src/test/kotlin/uncivai/CoverageCompletionProbe.kt), [CivilizationProfileProbe](../../src/test/kotlin/uncivai/CivilizationProfileProbe.kt), [model inspection tests](../../tests/test_player_inspections.py), [PlayerUnitManagementOperationsTest](../../vendor/Unciv/tests/src/com/unciv/logic/civilization/PlayerUnitManagementOperationsTest.kt), [BuildingCompletionPreviewTest](../../vendor/Unciv/tests/src/com/unciv/logic/city/BuildingCompletionPreviewTest.kt) | Additional controls, bounded information reads, unknown identity/location filtering, read-only estimates and native one-time grants. Not full-game or desktop visual acceptance. |
| **E-MODEL** | [startup tests](../../tests/test_startup_context.py), [API consistency tests](../../tests/test_api_consistency.py), [selective action tests](../../tests/test_selective_actions.py), [state update tests](../../tests/test_state_updates.py) | Tool projection, preload, copyable commands and freshness/deltas; mock-based tests do not prove engine behavior. |
| **E-LIFECYCLE** | [GameLaunchIntegrationTest](../../src/test/kotlin/uncivai/GameLaunchIntegrationTest.kt), [OperatorControlIntegrationTest](../../src/test/kotlin/uncivai/OperatorControlIntegrationTest.kt), [launcher tests](../../tests/test_run_vanilla_game.py) | Configured startup/persistence/operator access; not full native multiplayer or embedded desktop play. |
| **E-RECOVERY** | [TurnRecoveryIntegrationTest](../../src/test/kotlin/uncivai/TurnRecoveryIntegrationTest.kt), [match recovery tests](../../tests/test_match_recovery.py), [gateway recovery tests](../../tests/test_gateway_recovery.py), [background tests](../../tests/test_background_memory.py) | Coordinated rewind, receipts and durable background inputs/retries; no legacy conversation continuation claim. |
| **E-TELEMETRY** | [TelemetryIntegrationTest](../../src/test/kotlin/uncivai/TelemetryIntegrationTest.kt), [telemetry tests](../../tests/test_telemetry.py), [strategy TUI tests](../../tests/test_strategy_watch.py), [diplomacy TUI tests](../../tests/test_diplomacy_watch.py) | Usage, outcomes and progress display; accounting is not a spending cap or gameplay feature. |

### Architecture corrections from the previous ledger

- Engine views remain internal helpers; the host constructs explicit DTOs and the
  Python adapter projects them for the model. The old statement that the facade
  exposes raw mutable wrappers did not describe this serialized player boundary.
  The remaining information gaps above must still be assessed field by field.
- Public revisions and opaque identifiers are scoped to the player and have
  regression coverage. An internal cache's broader invalidation fingerprint is
  not itself a player-visible revision. The old blanket global-counter leakage
  claim was not supported by this audit.
- Prototype mutation entrypoints and conversation-checkpoint continuation are no
  longer the recovery design. A few native helper reads remain intentionally
  outside the model tool set. Existing code should be audited by its current
  behavior, not by historical feature-slice labels.
- Shared native operations and desktop inspection probes do not establish a
  connected human desktop client. That integration remains open.
- Settlement eligibility deliberately includes hidden-city spacing on explored
  tiles without revealing the city. That approved disclosure is distinct from
  hidden unit/resource/identity leakage. Other reads may be stricter than native
  UI visibility; parity never requires exposing private raw state.
- Automatic native effects, composable bulk shortcuts, GUI navigation and unused
  generic extension points are recorded separately from missing Vanilla actions.

### Checks performed for this audit

- Compared the enumerated controls listed under Scope against this ledger and
  checked the original 54 host command kinds against the Python schema's command-kind set.
- Checked local source/test/document link targets and evidence references.
- Ran the following existing tests with the requested `codex-log` helper:

  ```sh
  /home/vgopalas/.local/bin/codex-log .venv/bin/pytest \
    tests/test_api_consistency.py tests/test_schemas.py \
    tests/test_selective_actions.py tests/test_mechanics_preload.py \
    tests/test_model_view.py
  ```

  **102 passed.** These validate schema/model projection behavior, not all native
  gameplay or the source-traced marriage failure.

### Implementation validation

The implementation adds seven command kinds (61 total), eight on-demand inspection
subjects and new compact overview fields. All pytest and Gradle runs used
`codex-log`.

- Broad runs exercised **150 host JVM tests**, **1,251 engine tests** (15 skipped),
  and the Python suite (**979 passed, 2 skipped, 76 subtests** before correcting
  one schema-example failure). The JVM runs identified three outdated host
  assertions and two incomplete engine fixtures. Each was corrected and its
  affected tests rerun successfully; the entire broad runs were not repeated.
- Focused native/host checks cover marriage disposition and recovery, relocating
  upgrades, unit controls (including a formation swap exposing the returned
  civilian), road repair/rail estimates, explicit turn deferral,
  bounded reference/history reads, unique improvements and mechanical Civilopedia
  notes without flavor. The final focused Python run passed **98 tests**, including
  command/schema projection and inspection invalidation.
- Preview purity compares serialized native state before and after reads. These
  tests exposed shared mutable explored-map bounds in native cloning; the clone
  now copies those coordinates, and free-unit completion previews leave the
  source unchanged.
- Ruff, Python formatting and Pyrefly passed. Local links in the updated coverage
  and protocol documents resolve.

No fresh live LLM game or desktop visual acceptance was performed for this pass.
The ledger's enumeration and automated checks do not establish exhaustive runtime
correctness, every ruleset combination, or a universal privacy proof.

### Maintaining this ledger

When adding a feature, update the native-control row, host/model route, required
follow-ups and test evidence together. Before calling a flow complete, exercise
success, rejection without mutation, applicable visibility restrictions and every
mandatory follow-up. Keep prepared scenarios, desktop checks and full-game live
acceptance separate. Update the audited revisions when rechecking the inventory.

[alert-enum]: ../../vendor/Unciv/core/src/com/unciv/logic/civilization/PopupAlert.kt
[alert-ui]: ../../vendor/Unciv/core/src/com/unciv/ui/screens/worldscreen/AlertPopup.kt
[autoplay]: ../../vendor/Unciv/core/src/com/unciv/ui/screens/worldscreen/status/AutoPlayMenu.kt
[buy-ui]: ../../vendor/Unciv/core/src/com/unciv/ui/screens/cityscreen/BuyButtonFactory.kt
[capture-ops]: ../../vendor/Unciv/core/src/com/unciv/logic/civilization/PlayerCaptureOperations.kt
[citizen-ui]: ../../vendor/Unciv/core/src/com/unciv/ui/screens/cityscreen/CitizenManagementTable.kt
[city-development]: ../../src/main/kotlin/uncivai/player/CityDevelopment.kt
[city-management]: ../../src/main/kotlin/uncivai/player/CityManagement.kt
[city-stats-ui]: ../../vendor/Unciv/core/src/com/unciv/ui/screens/cityscreen/CityStatsTable.kt
[city-tile-ui]: ../../vendor/Unciv/core/src/com/unciv/ui/screens/cityscreen/CityScreenTileTable.kt
[city-ui]: ../../vendor/Unciv/core/src/com/unciv/ui/screens/cityscreen/CityScreen.kt
[civilization]: ../../vendor/Unciv/core/src/com/unciv/logic/civilization/Civilization.kt
[combat-ops]: ../../vendor/Unciv/core/src/com/unciv/logic/civilization/PlayerCombatOperations.kt
[commands]: ../../src/main/kotlin/uncivai/player/TurnCommands.kt
[communication]: ../../src/main/kotlin/uncivai/Communication.kt
[construction-ui]: ../../vendor/Unciv/core/src/com/unciv/ui/screens/cityscreen/CityConstructionsTable.kt
[decisions]: ../../src/main/kotlin/uncivai/player/DecisionDetails.kt
[demands]: ../../vendor/Unciv/core/src/com/unciv/logic/civilization/diplomacy/Demand.kt
[diplomacy]: ../../src/main/kotlin/uncivai/player/DiplomacyObservation.kt
[diplomatic-ops]: ../../vendor/Unciv/core/src/com/unciv/logic/civilization/PlayerDiplomaticCommunicationOperations.kt
[economy]: ../../src/main/kotlin/uncivai/player/EconomyObservation.kt
[espionage-ui]: ../../vendor/Unciv/core/src/com/unciv/ui/screens/overviewscreen/EspionageOverviewScreen.kt
[estimates]: ../../src/main/kotlin/uncivai/player/ConstructionEstimates.kt
[game-cli]: ../../src/civ_agent/commands/game.py
[gateway]: ../../src/civ_agent/proposal_gateway.py
[major-ui]: ../../vendor/Unciv/core/src/com/unciv/ui/screens/diplomacyscreen/MajorCivDiplomacyTable.kt
[map]: ../../src/main/kotlin/uncivai/player/MapObservation.kt
[marriage-native]: ../../vendor/Unciv/core/src/com/unciv/logic/civilization/diplomacy/CityStateFunctions.kt
[mechanics]: ../../src/main/kotlin/uncivai/player/MechanicsOverview.kt
[minor-ui]: ../../vendor/Unciv/core/src/com/unciv/ui/screens/diplomacyscreen/CityStateDiplomacyTable.kt
[minors]: ../../src/main/kotlin/uncivai/player/CityStates.kt
[native-diplomacy]: ../../src/main/kotlin/uncivai/player/NativeDiplomacyObservation.kt
[native-trade]: ../../src/main/kotlin/uncivai/NativeTradeBridge.kt
[next-actions]: ../../vendor/Unciv/core/src/com/unciv/ui/screens/worldscreen/status/NextTurnAction.kt
[next-menu]: ../../vendor/Unciv/core/src/com/unciv/ui/screens/worldscreen/status/NextTurnMenu.kt
[notifications]: ../../src/main/kotlin/uncivai/player/PlayerNotification.kt
[observations]: ../../src/civ_agent/observations.py
[operator]: ../../src/main/kotlin/uncivai/OperatorControl.kt
[orders]: ../../vendor/Unciv/core/src/com/unciv/logic/civilization/PlayerUnitOrders.kt
[overview-enum]: ../../vendor/Unciv/core/src/com/unciv/ui/screens/overviewscreen/EmpireOverviewCategories.kt
[pedia]: ../../vendor/Unciv/core/src/com/unciv/ui/screens/civilopediascreen/CivilopediaCategories.kt
[policy-ui]: ../../vendor/Unciv/core/src/com/unciv/ui/screens/pickerscreens/PolicyPickerScreen.kt
[politics-ui]: ../../vendor/Unciv/core/src/com/unciv/ui/screens/overviewscreen/GlobalPoliticsOverviewTable.kt
[popup-ops]: ../../vendor/Unciv/core/src/com/unciv/logic/civilization/PlayerOperations.kt
[profile]: ../../src/main/kotlin/uncivai/player/CivilizationProfile.kt
[promotion-ui]: ../../vendor/Unciv/core/src/com/unciv/ui/screens/pickerscreens/PromotionPickerScreen.kt
[religious-ui]: ../../vendor/Unciv/core/src/com/unciv/ui/screens/worldscreen/unit/actions/UnitActionsReligion.kt
[requirements]: ../../vendor/Unciv/core/src/com/unciv/logic/civilization/PlayerTurnRequirements.kt
[schemas]: ../../src/civ_agent/game_commands.py
[sections]: ../../src/civ_agent/entity_sections.py
[session]: ../../src/main/kotlin/uncivai/player/TurnSession.kt
[setup]: ../../src/main/kotlin/uncivai/VanillaGameSetup.kt
[startup]: ../../src/civ_agent/startup_context.py
[tech-ui]: ../../vendor/Unciv/core/src/com/unciv/ui/screens/pickerscreens/TechPickerScreen.kt
[tile-description]: ../../vendor/Unciv/core/src/com/unciv/ui/objectdescriptions/TileDescription.kt
[tile-ui]: ../../vendor/Unciv/core/src/com/unciv/ui/screens/worldscreen/bottombar/TileInfoTable.kt
[tool-schemas]: ../../src/civ_agent/tool_schemas.py
[tools]: ../../src/main/kotlin/uncivai/mcp/PlayerTools.kt
[trade-enum]: ../../vendor/Unciv/core/src/com/unciv/logic/trade/TradeOfferType.kt
[trade-logic]: ../../vendor/Unciv/core/src/com/unciv/logic/trade/TradeLogic.kt
[trade-terms]: ../../src/main/kotlin/uncivai/BilateralTradeTerms.kt
[unique-ui]: ../../vendor/Unciv/core/src/com/unciv/ui/screens/worldscreen/unit/actions/UnitActionsFromUniques.kt
[unit-enum]: ../../vendor/Unciv/core/src/com/unciv/models/UnitAction.kt
[unit-ui]: ../../vendor/Unciv/core/src/com/unciv/ui/screens/worldscreen/unit/actions/UnitActions.kt
[victory-decisions]: ../../src/main/kotlin/uncivai/player/VictoryDecisions.kt
[victory-progress]: ../../src/main/kotlin/uncivai/player/VictoryProgress.kt
[victory-statistics]: ../../src/main/kotlin/uncivai/player/VictoryStatistics.kt
[victory-ui]: ../../vendor/Unciv/core/src/com/unciv/ui/screens/victoryscreen/VictoryScreen.kt
[wonders-ui]: ../../vendor/Unciv/core/src/com/unciv/ui/screens/overviewscreen/WonderOverviewTab.kt
