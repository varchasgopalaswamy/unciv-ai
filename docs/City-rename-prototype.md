# Kotlin city-rename prototype

S03 adds the first validated player command over the [Kotlin summary interface](Player-summary-prototype.md). It remains an in-process Kotlin prototype. Each bound session can list its own city names and submit a rename; the host serializes that command with all player observations and runtime shutdown.

## Observe and rename

```kotlin
import uncivai.player.PlayerHost
import uncivai.player.RenameCityRequest

val player = PlayerHost(runtime).bindPlayer("Rome")
val observed = player.cities()
val city = observed.cities.first()
val receipt = player.renameCity(
    RenameCityRequest(
        requestId = "rename-1",
        epoch = observed.epoch,
        cityHandle = city.handle,
        name = "Nova Roma",
    ),
)
```

`cities()` returns an `OwnedCitiesSnapshot` with the loaded runtime's opaque epoch and detached `OwnedCitySummary` entries containing only `handle` and `name`. Handles are assigned within the session and remain stable for an observed owned city. They do not expose raw engine city IDs or a global entity counter. A handle supplied by another session grants no access.

The host resolves each handle against the current owner's actual city collection when executing a new request. Having observed a city previously does not authorize changing it after capture, transfer or removal. Unknown and unavailable city handles have the same player-facing error.

The epoch identifies a particular loaded runtime. It is not a turn number or a revision counter. Reloading a save creates a new epoch, and old requests cannot be applied to the new runtime even if the civilization and city names are the same.

## Validation and engine effects

The bound civilization must remain an ordinary Human major civilization, the city must still belong to it, and it must be that player's turn. Ownership and arguments are checked in the same serialized operation that changes the name.

The name rules follow the pinned engine's [CityRenamePopup](../vendor/Unciv/core/src/com/unciv/ui/screens/pickerscreens/CityRenamePopup.kt), [AskTextPopup](../vendor/Unciv/core/src/com/unciv/ui/popups/AskTextPopup.kt), and single-line text-field behavior:

- The name contains 1–32 UTF-16 code units, matching Kotlin/Java `String.length`.
- The characters `[]{}"\<>` and carriage returns or line feeds are rejected.
- Names are not trimmed. A space-only name, an unchanged name, and a duplicate city name are permitted by this rename contract.
- Font-dependent character availability is not enforced by this headless operation.

The normal rename click uses `CityScreen.canChangeState`, rather than the additional puppet restriction in `canCityBeChanged`; see [CityScreenCityPickerTable](../vendor/Unciv/core/src/com/unciv/ui/screens/cityscreen/CityScreenCityPickerTable.kt). Puppet status, resistance, and free-choice counters are not additional rename predicates. This does not establish a general decision-readiness policy.

After validation the operation assigns `City.name`, the same engine effect as [CityView.tryRenameCity](../vendor/Unciv/core/src/com/unciv/view/CityView.kt). It creates no screen, notification or turn transition and does not save to disk. The receipt contains `requestId`, `epoch`, `cityHandle`, `previousName`, and the applied `name`.

## Receipts and ordering

A successful request is recorded for the lifetime of its session and runtime. Repeating the exact request returns its original receipt. Reusing a successful request ID with different arguments fails. S05 shares this namespace with [great-person selections](Free-great-person-prototype.md), so reusing an ID across command kinds also fails. The receipt describes that operation's result at the time it was applied; it is not a fresh city observation.

Request IDs contain 1–128 printable ASCII characters, including spaces. Rejected requests do not reserve their IDs. This prototype keeps every successful receipt until its session is released; retention limits and persistent retry handling remain transport/lifecycle work.

For example, A renames Rome to Nova Roma and B subsequently renames it to New Rome. Retrying A returns A's receipt while the actual city stays New Rome. An exact retry does not recheck present turn or city ownership to perform a new mutation; it only reports the known historical result. Session/runtime validity, the Human major-civilization binding and the request epoch are still checked.

The runtime uses one synchronized execution path for observations, validation, mutation and receipt recording across sessions and host facades. Concurrent calls cannot interleave those steps. This provides serialization without promising arrival-order fairness. Trusted code holding raw engine state must participate in that coordination before performing concurrent writes.

Receipts and handles are held in memory. This step does not persist receipts across crashes, add reconnection authentication, or provide a player save/reload operation. A new session has its own request-ID namespace.

## Failure handling

Validation failures leave the engine unchanged and return a fixed player-facing error. If an unexpected failure occurs after mutation begins, the runtime retains the cause for trusted diagnostics and rejects further player operations with `GAME_HALTED`. The name may already have changed; rollback is not promised. Closing the halted runtime remains available to the host.

Command validation uses `NOT_YOUR_TURN`, `STALE_EPOCH`, `CITY_UNAVAILABLE`, `INVALID_ARGUMENT`, and `REQUEST_ID_CONFLICT`, alongside the existing `SESSION_CLOSED`, `PLAYER_UNAVAILABLE`, and `QUERY_FAILED` session errors. Messages have no engine exception causes, object IDs, or foreign city names.

The command does not accept arbitrary methods, callbacks or engine property names. Its internal mutation seam exists for tests of failure after a write and is not part of `PlayerSession`.

## Verification

Tests inspect the actual engine city name, the returned receipt and subsequent observations. Rejected commands are checked against raw serialized game state. Tests cover both bundled rulesets, owner/turn/epoch checks, session-scoped handles, capture or removal, the A/B/retry sequence, concurrent duplicate requests, detached results, sanitized errors, and a write followed by an exception that halts the runtime.

```sh
./gradlew --no-daemon --console=plain engineInfo build
```

The remaining player actions, complete decision handling and MCP transport are later steps in the [headless plan](Headless-player-agent-path.md).
