# Kotlin player-summary prototype

S02 is the first read-only observation surface over the [headless runtime](Headless-runtime.md). The host, facade, and scripted clients remain Kotlin code in one JVM. This is a small surface to extend toward the complete [player information and action inventory](Player-agent-API-review.md).

## Binding and use

Trusted host code creates a `PlayerHost` and assigns each session an ordinary Human civilization. The client receives a `PlayerSession`, whose queries cannot choose a viewer. S02 introduced `summary()`; S03 extends the interface with `cities()` and `renameCity(request)`, documented in the [city-rename prototype](City-rename-prototype.md). S05 adds `freeGreatPersonChoice()` and `chooseFreeGreatPerson(request)`, documented in the [choice prototype](Free-great-person-prototype.md).

```kotlin
import uncivai.HeadlessRuntime
import uncivai.player.PlayerHost
import uncivai.player.PlayerSummaryJson

// savePath and dataDirectory are absolute java.nio.file.Path values.
HeadlessRuntime.load(savePath, dataDirectory).use { runtime ->
    val host = PlayerHost(runtime)
    val rome = host.bindPlayer("Rome")
    val greece = host.bindPlayer("Greece")

    println(PlayerSummaryJson.encode(rome.summary()))
    println(PlayerSummaryJson.encode(greece.summary()))
}
```

The names in `bindPlayer` are engine civilization IDs chosen by the host, as in the generated S01 scenarios. They are not client-supplied credentials. The host rejects unknown, non-Human and non-major civilization slots. Read access to a Human player's own summary does not require it to be that player's turn or that player to be alive.

The summary contains copied scalar values, with generated kotlinx.serialization JSON serialization. No `GameInfo`, civilization, manager, collection, view wrapper, or callable engine operation is returned. Retaining a result does not retain a live view of later state.

## Field contract

References below use the pinned engine at `d77a312`. UI references establish the human information surface or the own-player choice flow; production code does not instantiate those screens.

| Field | Meaning and permitted source |
| --- | --- |
| `civilization` | The bound player's own civilization name, from `Civilization.civName`. Only that player's identity is disclosed. |
| `ruleset` | The selected bundled base ruleset name, a public match setting also selected in [GameOptionsTable](../vendor/Unciv/core/src/com/unciv/ui/screens/newgamescreen/GameOptionsTable.kt). No other game parameters or map seed are included. |
| `turn` | The game's turn number, displayed in [WorldScreenTopBarResources](../vendor/Unciv/core/src/com/unciv/ui/screens/worldscreen/topbar/WorldScreenTopBarResources.kt). |
| `isYourTurn` | Whether `GameInfo.currentPlayer` equals the bound civilization ID. The identity of a different current player is omitted. This is not an end-turn readiness result. |
| `treasury` | The bound player's gold balance, also displayed in [WorldScreenTopBarStats](../vendor/Unciv/core/src/com/unciv/ui/screens/worldscreen/topbar/WorldScreenTopBarStats.kt). |
| `cityCount` | The number of cities in the bound player's own collection, which supplies [CityOverviewTab](../vendor/Unciv/core/src/com/unciv/ui/screens/overviewscreen/CityOverviewTab.kt). |
| `unitCount` | The number of units in the bound player's own collection, which supplies [UnitOverviewTab](../vendor/Unciv/core/src/com/unciv/ui/screens/overviewscreen/UnitOverviewTab.kt). |
| `currentResearch` | The first technology in the player's research queue, using `currentTechnologyName()` as in [TechPolicyDiplomacyButtons](../vendor/Unciv/core/src/com/unciv/ui/screens/worldscreen/TechPolicyDiplomacyButtons.kt). JSON `null` means no technology is selected. |
| `freePolicies` | The player's remaining free-policy entitlements, from the counter used by [PolicyPickerScreen](../vendor/Unciv/core/src/com/unciv/ui/screens/pickerscreens/PolicyPickerScreen.kt). |
| `freeTechnologies` | The player's remaining free-technology entitlements, from the counter used by [TechPickerScreen](../vendor/Unciv/core/src/com/unciv/ui/screens/pickerscreens/TechPickerScreen.kt). |
| `freeGreatPeople` | The player's remaining free-great-person entitlements, from the counter consumed by [GreatPersonPickerScreen](../vendor/Unciv/core/src/com/unciv/ui/screens/pickerscreens/GreatPersonPickerScreen.kt). |

The three free-pick counters describe the player's own outstanding entitlements. They are not counts of currently legal options, and do not enumerate all pending decisions. Policy eligibility, research prerequisites and Maya great-person restrictions still apply. S02 cannot resolve any of these choices or authorize ending a turn.

## Lifetime and errors

A session belongs to the runtime from which it was created. A read is synchronized with runtime shutdown and verifies that the same game is still authoritative. Closing the runtime or replacing its game invalidates the session. Loading another runtime never redirects old sessions to the new game. Each query also checks that its assigned civilization is still a Human major civilization.

`PlayerSessionException` has a fixed message and a `PlayerSessionFailure` reason. Summary reads can return `SESSION_CLOSED`, `PLAYER_UNAVAILABLE`, `QUERY_FAILED`, or `GAME_HALTED` after an uncertain mutation. S03 adds the command-specific reasons described in the [city-rename prototype](City-rename-prototype.md). Unexpected engine exceptions are not attached as causes or included in player messages. These are Kotlin errors; a transport error format is deferred to the transport adapter.

The in-process clients are trusted test clients. Kotlin access modifiers and detached data establish the API contract, not an execution sandbox for arbitrary code sharing the JVM. External agents must eventually receive only the permitted serialized surface through their bound transport connections. Raw runtime state, host factories, saves and diagnostics remain trusted host capabilities.

There are no public snapshot revisions or event counters in the summary. Reads return the current own-player summary without advancing the game. S03 serializes reads and validated commands through the runtime; direct trusted engine writes must remain coordinated. Broad map information, further entity detail, history, other choices and MCP transport remain later work.

## Verification

The tests exercise two bound Kotlin clients against both bundled rulesets, compare serialized summaries across paired states with different unrelated hidden facts, verify that reads preserve game state, and check detachment, schema fields, rejected bindings and session expiry. The existing S01 process lifecycle tests continue to run.

```sh
./gradlew --no-daemon --console=plain engineInfo build
```
