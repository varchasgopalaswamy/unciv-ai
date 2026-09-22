# Encounters and diplomacy coverage

This inventory follows the selected engine's `AlertType` dispatch and popup
producers. Its reachability notes use the internal [Vanilla duel fixture](../../src/test/kotlin/uncivai/fixtures/DuelSetup.kt):
Vanilla Rome and Greece, two human engine slots, Quick speed, Ancient start,
Domination/Time victories, 330 maximum turns, no city-states, barbarians, ruins or
mods. Human slots do not run built-in civilization AI diplomacy. Religion is
disabled by Vanilla's `ModOptions.json`; espionage is disabled in the game
parameters. An alert that cannot occur in this setup still needs a safe unsupported
response if another fixture or setup introduces it. The launchable
[trade-cycle scenario](../scenarios/Trade-cycle-scenario.md) uses Gods & Kings.

The first-contact slice adds the encounter introduction and acknowledgement.
The subsequent [war and peace slice](War-and-peace.md) adds diplomatic options,
war declarations and peace negotiations. The tables below retain the remaining
implementation backlog; names marked proposed are not available commands.

## First contact and known civilizations

[`DiplomacyFunctions.makeCivilizationsMeet`](../../vendor/Unciv/core/src/com/unciv/logic/civilization/diplomacy/DiplomacyFunctions.kt)
creates both diplomacy records and queues one `FirstContact` alert for each
player. Encounter effects occur there, before either player responds.
[`AlertPopup.addFirstContact`](../../vendor/Unciv/core/src/com/unciv/ui/screens/worldscreen/AlertPopup.kt)
then displays the other leader's introduction with one acknowledgement:
`A pleasure to meet you.` for a major civilization. The city-state variant has a
different message and `Excellent!`; city-states remain outside this host setup.
Music, voice playback and the leader's Civilopedia link are presentation surfaces,
not effects of the acknowledgement.

The detached first-contact details identify the met civilization, show its leader
display name, introduction and acknowledgement label. `ACKNOWLEDGE_ALERT` targets
the issuing player's opaque alert handle. Its only game-state change is removal
of that particular alert from that player's queue. It must not create another
diplomacy record, change war/peace, reveal tiles again or repeat encounter gifts.
The shared engine acknowledgement operation is also used by the desktop popup.

[`LeaderIntroTable`](../../vendor/Unciv/core/src/com/unciv/ui/screens/diplomacyscreen/LeaderIntroTable.kt)
uses `Civilization.getLeaderDisplayName()`. This includes any displayed leader
title and the `Human - Hotseat` suffix in the current two-human setup. The API
should match that displayed caption rather than expose the underlying count of
players, player IDs or other host bookkeeping.

The initial diplomacy slice exposed only each met major civilization's identity
and current `atWar` relationship. The desktop diplomacy list uses
[`DiplomacyFunctions.getKnownCivsSorted`](../../vendor/Unciv/core/src/com/unciv/logic/civilization/diplomacy/DiplomacyFunctions.kt)
and its war indicator uses `isAtWarWith`; see
[`DiplomacyScreen.updateLeftSideTable`](../../vendor/Unciv/core/src/com/unciv/ui/screens/diplomacyscreen/DiplomacyScreen.kt).
Unmet civilizations are absent. Opponent cities, positions, orders, research,
production, stockpiles, pending choices and raw diplomacy managers are absent.
The newer [war and peace view](War-and-peace.md) adds peace treaty status,
confirmation warnings and legal negotiation options. Broader relationship
modifiers and diplomatic systems remain incomplete.

Required checks for this slice cover legal exploration producing both encounter
alerts; matching desktop introduction/response text; one dismissal without other
encounter effects; continued scheduled HTTPS handoffs; checkpoint restart with a
pending alert; exact receipt replay after a lost response; ownership, turn,
revision and pause guards; mixed supported/unsupported queues; and paired worlds
whose hidden opponent state differs while permitted encounter data stays equal.
Checkpoint recovery is a runtime guarantee for newly created matches. Migration
from older checkpoint schemas is not required for this sprint.

Verification is recorded in:

- Engine [`PlayerFirstContactTest`](../../vendor/Unciv/tests/src/com/unciv/logic/civilization/PlayerFirstContactTest.kt):
  shared major/city-state text, owned alert removal, unchanged meeting effects,
  invalid references, active-player and spectator checks, and consequential alerts.
- Host [`DiplomacyProbe`](../../src/test/kotlin/uncivai/DiplomacyProbe.kt): detached
  observations matching UI text, hidden-state and revision checks, mixed queues,
  wrong-player/turn/handle rejection, pause/stale guards and explicit acknowledgement.
- [`EncounterRunIntegrationTest`](../../src/test/kotlin/uncivai/EncounterRunIntegrationTest.kt):
  advertised movement creates contact over HTTPS; two process restarts preserve
  pending handles and an acknowledged receipt; both players then complete four rounds.

The popup and API use the same engine introduction query. These checks do not
claim a visual desktop acceptance run, a completed model duel or broader diplomacy
parity. The next live run is an operator-run fresh match.

## Alerts reachable with today's commands

All handler names below refer to
[`AlertPopup`](../../vendor/Unciv/core/src/com/unciv/ui/screens/worldscreen/AlertPopup.kt).
`Close` or acknowledgement means dismissing only the player's queued popup unless
the row explicitly identifies another effect. Existing generic alert observations
do not imply that all accompanying desktop prose, images and Civilopedia
information have been covered.

| Alert | Producer and desktop text | Buttons and effects | Coverage and next check |
| --- | --- | --- | --- |
| `StartIntro` | [`GameStarter.addPlayerIntros`](../../vendor/Unciv/core/src/com/unciv/logic/GameStarter.kt); `addStartIntro` shows the player's leader and nation `startIntroPart1/2`. | `Let's begin!`: dismiss. | Shared full caption/paragraphs and `ACKNOWLEDGE_ALERT` after observation; tests compare both paragraphs and order. |
| `TechResearched` | [`TechManager.addTechnology`](../../vendor/Unciv/core/src/com/unciv/logic/civilization/managers/TechManager.kt); `addTechResearched` shows technology name, quote and player-context description. | `Close`: dismiss; research is already applied. | Shared name, quote and player-context description; tests compare Rome/Greece unlocks and require fresh observation before acknowledgement. |
| `WonderBuilt` | [`CityConstructions`](../../vendor/Unciv/core/src/com/unciv/logic/city/CityConstructions.kt) queues it on wonder completion; `addWonderBuilt` shows name, quote, short description and portrait/image. | `Close`: dismiss; construction effects are already applied. | Shared name, quote and short description; tested acknowledgement without repeated effects. Images remain a separate presentation surface. |
| `GoldenAge` | [`GoldenAgeManager`](../../vendor/Unciv/core/src/com/unciv/logic/civilization/managers/GoldenAgeManager.kt); `addGoldenAge` shows the Golden Age announcement and explanation. | `Close`: dismiss; Golden Age has already begun. | Shared heading/explanation and observed acknowledgement; tests ensure the Golden Age is not triggered again. |
| `FirstContact` | [`DiplomacyFunctions.meetCiv`](../../vendor/Unciv/core/src/com/unciv/logic/civilization/diplomacy/DiplomacyFunctions.kt); `addFirstContact` shows met leader and introduction. Legal exploration can trigger it. | `A pleasure to meet you.`: dismiss only. | Added in this slice, as described above. |
| `GameHasBeenWon` | [`TurnManager.updateWinningCiv`](../../vendor/Unciv/core/src/com/unciv/logic/civilization/managers/TurnManager.kt) queues this for other human players after victory; `addGameHasBeenWon` identifies winner and victory type. Time victory is reachable without adding combat. | `Victory status`: dismiss and open the victory screen; `Close`: dismiss. Neither undoes victory. | Implemented: shared informational content and player victory/defeat result; final messages and reports delivered over HTTPS after scheduling stops. See [combat](Combat.md) for winner/loser, restart and delivery tests. |

The [informational-popup slice](Informational-popups.md) supplies the common
`content` fields and requires a current public `player_turn` query before
acknowledgement. Agent prompts ask players to read the entire message and record
a brief useful note before dismissal; current histories retain both.

## Diplomacy and combat decisions

These can occur between the two human major civilizations through ordinary
desktop actions, with war declarations now implemented; the remaining actions are future work.
Every proposed response must use currently offered choices, validate the issuing
player's alert and recheck any changing prerequisites. The supervisor must not
choose for a player or silently clear an unsupported queue.

| Alert | Trigger and desktop observation | Buttons and game effects | Proposed API work and meaningful tests |
| --- | --- | --- | --- |
| `WarDeclaration` | [`DeclareWar`](../../vendor/Unciv/core/src/com/unciv/logic/civilization/diplomacy/DeclareWar.kt) queues it when war is declared; `addWarDeclaration` shows declaring leader, declaration header and `nation.declaringWar` when nonempty. | `You'll pay for this!` and `Very well.` both dismiss. War is already active. A defeated declarer makes the desktop skip the popup. | Implemented: validated DECLARE_WAR, shared full declaration content and observed ACKNOWLEDGE_ALERT. Tests cover equivalent dismissal, bilateral effects, treaty restrictions and exact retries. |
| `DeclarationOfFriendship` | [`MajorCivDiplomacyTable.getDeclareFriendshipButton`](../../vendor/Unciv/core/src/com/unciv/ui/screens/diplomacyscreen/MajorCivDiplomacyTable.kt) sends a human offer; `addDeclarationOfFriendship` shows leader, friendship header and national text or fallback. | `Declare Friendship ([30] turns)`: `signDeclarationOfFriendship`. `We are not interested.`: sets the sender's `DeclinedDeclarationOfFriendship` flag for 20 turns. Desktop skips offers from defeated civilizations or civilizations now at war. | Proposed `OFFER_FRIENDSHIP` and `RESPOND_FRIENDSHIP` with accept/decline choices. Test both effects, duplicate offers, war invalidation, expiration and exact response replay. |
| `DemandToStopSettlingCitiesNear` | [`MajorCivDiplomacyTable.getDemandsTable`](../../vendor/Unciv/core/src/com/unciv/ui/screens/diplomacyscreen/MajorCivDiplomacyTable.kt) can send the demand; `addDemand(DoNotSettleNearUs)` uses [`Demand`](../../vendor/Unciv/core/src/com/unciv/logic/civilization/diplomacy/Demand.kt) text: `Please don't settle new cities near us.` | `Very well, we shall look for new lands to settle.` calls `agreeToDemand`; `We shall do as we please.` calls `refuseDemand`. These alter flags/modifiers and notify the demander. | Proposed `ISSUE_DEMAND` and `RESPOND_DEMAND`, with only offered demand kinds. Test agreement/refusal, requester and recipient direction, repeated demands, resulting acknowledgement and shared desktop effects. Human-slot promise enforcement needs the audit described below. |
| `AcceptingDemand` | [`DiplomacyManager.agreeToDemand`](../../vendor/Unciv/core/src/com/unciv/logic/civilization/diplomacy/DiplomacyManager.kt) queues a unique reply; `addAcceptingDemand` displays accepting leader, header and `nation.acceptingDemand` or fallback. | `Very well.`: dismiss. Agreement effects have already happened. | Extend explicit informational acknowledgement when demands ship. Test that duplicate replies are not queued and dismissal does not apply promise/modifier effects again. |
| `RejectingDemand` | [`DiplomacyManager.refuseDemand`](../../vendor/Unciv/core/src/com/unciv/logic/civilization/diplomacy/DiplomacyManager.kt); `addRejectingDemand` chooses neutral/hateful rejection text according to the rejecter's relationship, with national/fallback wording. | `You'll pay for this!` and `Very well.` both dismiss; neither declares war. Refusal effects have already happened. | Extend acknowledgement with the displayed reply. Test both relationship-selected texts and identical dismissal effects; do not expose raw relationship state just to render the text. |
| `Denounced` | [`DiplomacyManager.denounce`](../../vendor/Unciv/core/src/com/unciv/logic/civilization/diplomacy/DiplomacyManager.kt), invoked by the desktop denounce button, queues the target's alert after denunciation effects; `addDenouncement` displays leader, header and neutral/hateful national text or fallback. | Optional `THIS MEANS WAR! (Declare war)` if `canDeclareWar`, or `Very well.`. The first calls `declareWar`, the second only dismisses. | Proposed `DENOUNCE` and `RESPOND_DENOUNCEMENT`; expose legal war response separately from acknowledgement. Test treaty restrictions, changing eligibility and both responses. Never add this whole alert type to a generic informational allowlist. |
| `CityConquered` | [`Battle`](../../vendor/Unciv/core/src/com/unciv/logic/battle/Battle.kt) queues it after a human captures a city; `addCityConquered` names the city and explains choices and disabled reasons. | Conditional `Liberate`, `Destroy`, `Annex`, `Puppet`, `Raze`. Handlers change ownership/puppet state, annex, destroy or start razing. Current ordinary duel permits annex/puppet and eligible raze choices; capitals cannot be razed. Third-party liberation and one-city-challenge destroy branches are outside this setup. | Implemented `RESOLVE_CAPTURED_CITY` with shared desktop choices, full explanations, disabled reasons and an observation requirement. Capture decisions block ordinary commands. Engine tests cover all dispositions; HTTPS tests cover puppet/annex, authority, restart/retry and terminal conquest. |
| `Defeated` | [`Battle`](../../vendor/Unciv/core/src/com/unciv/logic/battle/Battle.kt) queues it for the attacker when another civilization is defeated; `addDefeated` shows defeated leader and `nation.defeated`. | `Farewell.`: dismiss; defeat has already happened. | Implemented shared leader text and acknowledgement. Terminal delivery preserves this message even if no further turn is scheduled; HTTPS tests verify the final Farewell content and separate winner/loser results. |

Peace and trade offers are **not** `PopupAlert` entries. They live in
`tradeRequests` and are shown by
[`TradePopup`](../../vendor/Unciv/core/src/com/unciv/ui/screens/worldscreen/TradePopup.kt).
The player sees both offer columns, amounts/durations, relevant own resource
counts, requesting leader and `nation.tradeRequest`. `Sounds good!` applies
`TradeLogic.acceptTrade`; `Not this time.` declines; `How about something else...`
removes the pending request and opens a counteroffer editor. A peace treaty uses
this mechanism. The [implemented peace slice](War-and-peace.md) covers offered
treaties, gold, GPT and resources with accept/reject/counteroffer/retraction,
changed affordability, obsolete offers, exact replay and bilateral treaty state. Trade acceptance's
`TradeThanksPopup` is a separate informational desktop popup. Covering every
`AlertType` alone does not cover all mandatory decisions.

## Types outside this setup and conditional branches

These are deliberately unsupported. Their presence in a test fixture must not
make them acknowledgeable or allow a turn to pass silently. Revisit the applicable
row when expanding the game setup or introducing its producer.

| Alert type(s) | Why not naturally produced here | Desktop text/buttons/effects and future work |
| --- | --- | --- |
| `CitySettledNearOtherCivDespiteOurPromise` | [`NextTurnAutomation.onDemandViolation`](../../vendor/Unciv/core/src/com/unciv/logic/automation/civilization/NextTurnAutomation.kt) is the sole popup producer and is reached through built-in civilization AI automation. Both duel civilizations use human slots. | `addDemandViolationNoticed(DoNotSettleNearUs)` shows the leader and settlement-promise violation text; `Very well.` dismisses. The producer has already changed promise flags and modifiers. Before promising human-slot enforcement, audit/extract that rule independently of AI decision-making and test actual settling after a promise. |
| `DemandToNotAttackUs` | [`Demand.DoNotAttackUs.show`](../../vendor/Unciv/core/src/com/unciv/logic/civilization/diplomacy/Demand.kt) returns false; the desktop cannot issue it. The military-presence producer is AI automation. | `addDemand` shows the military-presence question. `Do not worry. Our soldiers are merely passing by.` agrees; `Our military stands ready to finish what diplomacy could not! (DECLARE WAR)` refuses and declares war. Future response command must test both promise effects and exactly one war declaration. |
| `AttackedUsDespitePromise` | Like the settlement violation, production is in AI `onDemandViolation`, after a violation flag is set. No AI major runs in this duel. | `addDemandViolationNoticed(DoNotAttackUs)` shows `You have exposed yourself as a treacherous warmonger.`; `Very well.` dismisses. Penalties are producer effects. Cover promise-break consequences and public third-party effects if AI games are introduced. |
| `DemandToStopSpreadingReligion`, `ReligionSpreadDespiteOurPromise` | Vanilla disables religion. Demand visibility also checks `isReligionEnabled`; violation generation needs the relevant religion/automation state. | `addDemand(DoNotSpreadReligion)` uses the religious-spread request and offered agreement/refusal text from `Demand`; replies call `agreeToDemand`/`refuseDemand`. `addDemandViolationNoticed` shows the broken-promise warning and `Very well.`. Add religion observations/actions and tests before enabling these responses. |
| `DemandToStopSpyingOnUs`, `SpyingOnUsDespiteOurPromise` | Espionage is disabled. Demand visibility checks `isEspionageEnabled`. | Both enum cases currently dispatch to `addDemand(DontSpyOnUs)`, including the violation case. The UI therefore offers agreement/refusal, with flags/modifiers and reply popups; it is not a dismissal-only warning. Preserve or independently correct that engine behavior when implementing espionage, with explicit UI/effect tests. |
| `BorderConflict` | [`CityStateFunctions`](../../vendor/Unciv/core/src/com/unciv/logic/civilization/diplomacy/CityStateFunctions.kt) queues it for city-state border incursions; no city-states exist. | `addBorderConflict`: leader plus `Remove your troops in our border immediately!`; `Sorry.` and `Never!` both dismiss without another effect. Future CS coverage should compare both responses and the preexisting influence/relationship consequences. |
| `TilesStolen` | [`UniqueTriggerActivation.OneTimeTakeOverTilesInRadius`](../../vendor/Unciv/core/src/com/unciv/models/ruleset/unique/UniqueTriggerActivation.kt) queues this popup only when the former tile owner is a city-state. Major-civilization territory theft produces notifications instead. | `addTilesStolen`: leader plus `Those lands were not yours to take. This has not gone unnoticed.`; `Close` dismisses. Test ownership/influence changes happen in the tile-taking operation, not acknowledgement, when enabling city-states. |
| `CityTraded` | [`TradeLogic`](../../vendor/Unciv/core/src/com/unciv/logic/trade/TradeLogic.kt) requires a founding civilization different from both giver and recipient to queue this alert. That needs a third civilization, absent here. | `addCityTraded` names the city; offers `Keep it` and, if not at war with founder, `Liberate`. Keeping only dismisses; liberation calls `liberateCity`. General trade support must test eligibility, ownership transfer and both responses. Ordinary two-player city trades do not themselves produce this alert. |
| `DiplomaticMarriage` | [`CityStateDiplomacyTable`](../../vendor/Unciv/core/src/com/unciv/ui/screens/diplomacyscreen/CityStateDiplomacyTable.kt) requires a city-state and marriage ability, absent for Rome/Greece here. | `addDiplomaticMarriage` names city and offers annex/puppet, or destroy in one-city challenge. Annex changes city control; puppet sets `isPuppet` and updates stats; destroy removes the city. Add validated city-state acquisition/disposition operations with eligibility and result tests if the setup expands. |
| `BulliedProtectedMinor`, `AttackedProtectedMinor`, `AttackedAllyMinor` | [`CityStateFunctions`](../../vendor/Unciv/core/src/com/unciv/logic/civilization/diplomacy/CityStateFunctions.kt) requires a protected/allied city-state. | `addBulliedOrAttackedProtectedOrAlliedMinor` names attacker and city-state and selects neutral/hostile text. `THIS MEANS WAR!` (when not already at war) sides with city-state, declares war and adds 20 influence; `You'll pay for this!` sides with city-state; `Very well.` forcibly removes protection and notifies the player. Future responses need all three effect/eligibility tests. |
| `RecapturedCivilian` | [`BattleUnitCapture.captureCivilianUnit`](../../vendor/Unciv/core/src/com/unciv/logic/battle/BattleUnitCapture.kt) queues it only for a civilian recaptured from barbarians with another known living peaceful original owner. Barbarians are disabled. | `addRecapturedCivilian` asks `Return [unit] to [owner]?`. `Yes` destroys/recreates the returned unit near its owner's city and grants influence or the returned-units modifier plus notification; `No` keeps/converts the captive. Implemented through the capture decision API and shared desktop operations; engine tests cover return rewards, conversion, unit identity and missing-unit eligibility. This popup remains unreachable in the current no-barbarian launch setup. |
| `Event` | [`UniqueTriggerActivation`](../../vendor/Unciv/core/src/com/unciv/models/ruleset/unique/UniqueTriggerActivation.kt) queues ruleset events. Vanilla has no event definitions; mods/ruins are excluded. Global alert notifications from some Vanilla uniques are a different surface. | `addEvent` resolves event name and optional unit context, then delegates text, offered choices, conditions and effects to [`RenderEvent`](../../vendor/Unciv/core/src/com/unciv/ui/screens/worldscreen/RenderEvent.kt). This requires a permitted event-choice model and effect-specific tests; never assume a generic close action is valid. |

The enum source is
[`PopupAlert.kt`](../../vendor/Unciv/core/src/com/unciv/logic/civilization/PopupAlert.kt).
Every enum entry is represented above. Some desktop handlers discard obsolete
alerts while opening them, such as an offer from a defeated civilization. Any
future shared operation for that behavior needs its own legality and effect
tests; this inventory does not authorize the scheduler to emulate it.

This inventory covers encounters and alerts. See the
[coverage ledger](Player-api-coverage.md) for other player surfaces,
[mixed-player diplomacy](Mixed-player-diplomacy.md) for native demands and
responses, and the [roadmap](../Roadmap.md) for unfinished work.
