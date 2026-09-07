# Kotlin free-great-person prototype

S05 connects the [shared engine selection operation](Free-great-person-selection.md) to the bound `PlayerSession`. A Kotlin client can discover its own current choice and select an offered great person. The engine performs placement and consumes the entitlement. The host checks authority, tracks choice identity and records successful requests.

## Discover and select

```kotlin
import uncivai.player.ChooseFreeGreatPersonRequest
import uncivai.player.PlayerHost

val player = PlayerHost(runtime).bindPlayer("Rome")
val snapshot = player.freeGreatPersonChoice()
val decision = requireNotNull(snapshot.decision)
// A client chooses any entry from decision.options.
val selected = "Great Scientist"
require(selected in decision.options)
val receipt = player.chooseFreeGreatPerson(
    ChooseFreeGreatPersonRequest(
        requestId = "great-person-1",
        epoch = snapshot.epoch,
        decisionHandle = decision.handle,
        unitName = selected,
    ),
)
```

`FreeGreatPersonSnapshot` contains the runtime `epoch` and a nullable `decision`. No outstanding entitlement means `decision = null`, encoded as an explicit JSON null. A positive entitlement produces a decision even when no option is currently available, such as a player without a city or a Maya choice with an empty pool.

The decision contains an opaque session `handle`, `remainingChoices`, `mayaLimited`, and a sorted, detached, unmodifiable list of option names. Names come directly from `GreatPersonManager.getFreeGreatPersonOptions()`: they include civilization equivalents, such as Mongolia's Khan, and respect the current game settings and Maya restrictions. These are the names accepted by the engine; translated display labels are not command identifiers. Being offered does not guarantee a unit can be placed.

Reads remain available out of turn. They do not select an option, advance a turn or create a popup. Engine units, definitions, managers, raw entity IDs and foreign player identities do not cross this interface. `PlayerCommandJson` supplies generated serializers for snapshots, requests and receipts.

## Choice identity and validation

Each handle belongs to one session and one observed pending choice. Discovery keeps that handle stable while the choice remains current. A shared runtime tracker coordinates the same civilization across sessions and separate `PlayerHost` instances. A successful selection expires the old choice for every session. Another selection requires a fresh discovery and handle, even if the same unit type remains available.

The tracker also replaces the choice when it observes a change in the player's free-choice counters, permitted option names, game turn or whether it is that player's turn. It exposes neither a global mutation count nor another player's current identity. Unrelated hidden foreign changes cannot invalidate the handle. This prototype coordinates the supported facade operations; it cannot detect an arbitrary trusted engine edit that changes state and restores it between observations. Trusted raw writes must use the runtime's serialization and respect that boundary.

A new selection validates runtime lifetime, epoch, printable-ASCII request ID, the assigned Human major civilization, current turn, live decision handle and selected option. Both the current player ID and the actual current civilization must match the session. Unknown, foreign, expired and already consumed handles produce the same `DECISION_UNAVAILABLE` error. A name outside the live offered set produces `OPTION_UNAVAILABLE`.

All validation, engine mutation and receipt recording share the same runtime lock as city commands, observations and shutdown. Resolving the same choice concurrently cannot spend the next entitlement. No command accepts a viewer, arbitrary engine method, callback or placement coordinate.

## Receipts and failure behavior

The receipt records `requestId`, `epoch`, `decisionHandle`, `unitName` and the remaining free-choice count immediately after success. It reports a historical operation result; use discovery or the summary for current state. The engine's placed `MapUnit` remains trusted host state.

Successful request IDs share one namespace across city renames and great-person selections within a session. An exact repeat returns the original receipt without placing another unit, even after a later choice or a turn change. Reusing the ID with a different payload or command kind produces `REQUEST_ID_CONFLICT`. Runtime, epoch and player binding are still checked before replay. Rejected requests do not reserve IDs. Receipts remain in memory for the session lifetime, with no cross-session or cross-crash retry guarantee.

If the engine cannot place an offered unit, selection returns sanitized `PLACEMENT_FAILED`. The entitlement and Maya pool remain available, no receipt is recorded, and the same request may be tried after space becomes available. Existing engine placement may allocate an internal unit ID before failing; this is not a promise that raw serialized game state is unchanged. There is no automatic retry loop.

An unexpected exception after entering the engine mutation halts all player operations with `GAME_HALTED`. The unit might already exist and the choice might already have been consumed. The runtime retains the original exception for trusted diagnostics and can still be closed; it does not roll back the game or encourage retrying uncertain work. Public errors carry fixed messages and no engine exception causes.

## Verification and limits

The S05 integration probe uses the real headless runtime and inspects placed units and the actual engine counters and Maya pool. It selects every offered ordinary option in both bundled rulesets, including Mongolia's Khan and a Great Admiral in a derived coastal save. It also covers Maya selections, blocked placement, empty choices, invalid and stale requests, cross-session competition, historical receipts, hidden-state independence, lifecycle checks and failure after placement. DTO tests check JSON and public type whitelists.

```sh
./gradlew --no-daemon --console=plain engineInfo build
```

The [S04 UI comparison saves and commands](Free-great-person-selection.md) remain available for reviewing the underlying engine behavior. S05 adds no engine changes. The facade and scripted clients remain Kotlin code in one JVM. MCP transport is S06; other pending decisions, complete player information/action coverage and end-turn readiness remain required later work.
