# Free great-person selection

S04 extracts the free great-person choice into the engine's `GreatPersonManager`. The human picker and automated selection use the same operation. The [S05 Kotlin prototype](Free-great-person-prototype.md) projects the options through the bound player session and adds command validation and receipts.

## Engine contract

`getFreeGreatPersonOptions()` returns currently selectable `BaseUnit` definitions. A civilization needs a positive free-choice count and a city. Candidates reuse `getGreatPeople()`, including civilization equivalents and game-setting restrictions. When `mayaLimitedFreeGP` is positive, only names remaining in `longCountGPPool` are selectable. Options are candidates, not a guarantee that the map has room for a unit.

`chooseFreeGreatPerson(unitName)` resolves the exact selected name against fresh options and invokes ordinary unit creation near the capital, using the existing first-city fallback. A successful call returns the actual `MapUnit`, decrements the free-choice count, and, for a Maya-limited choice, decrements that count and removes the selected name from the pool. Ordinary free choices leave the stored Maya pool alone. Pool replenishment remains part of `triggerMayanGreatPerson()`.

The operation returns `null` for an unavailable selection, exhausted choices, a civilization without cities, or failed placement. Those outcomes retain the free-choice counters and Maya pool. Previously the human picker ignored failed placement and consumed the choice anyway.

The existing placement machinery supplies normal unit initialization, birth notification, quests and unit-gain effects. An attempted placement can allocate an internal unit ID before finding that no tile is available; failure does not promise byte-identical game state. Unexpected exceptions propagate, and arbitrary effects are not rolled back. S05 applies the host's existing policy of halting after uncertain mutation failures.

This is an engine operation with trusted engine types. It does not authenticate a player, enforce session epochs, serialize concurrent callers, or cache request receipts. Those remain the host's responsibilities. A second successful call can spend a second available choice; S05 recognizes an exact request retry before calling it again.

## Human picker

The picker continues to display all great-person candidates and disables choices excluded by the shared option query. Confirmation revalidates eligibility and calls the engine operation. Successful choices close the picker; failed placement retains the choice and gives feedback. The picker no longer captures a Maya restriction flag at construction time for later bookkeeping.

After failed placement, a stale selection, or when there are no available options, the picker permits returning to the map. A flag local to that `WorldScreen` prevents immediate reopening. The next-turn button still requires the pending great-person choice and reopens the picker with fresh options, allowing the player to clear space first without bypassing the choice. Successful selection clears the flag. This is desktop behavior; no player end-turn API is added to the host.

Automated selection retains its seeded selection and preference for a Great Scientist, while delegating the actual grant and bookkeeping. It also retains its entitlement if placement fails.

## Manual before/after comparison

From the host repository root, these commands automatically load the blocked-placement save:

```sh
./scripts/s04-ui before blocked-placement
./scripts/s04-ui after blocked-placement
```

Close one run before starting the next. The `before` command uses a separate local checkout of the original engine revision under `build/s04-ui/before-engine`; it does not switch the working engine branch. Both runs use copies of their selected engine's assets and separate settings/save directories under `build/s04-ui`. Each launch loads the generated original scenario, so the comparison starts afresh.

The save files are generated under `build/generated/s04-ui` and can also be opened through the ordinary Load Game screen. Regenerate them with `./gradlew generateS04UiScenarios`.

| Scenario argument / save name | What to try | Before | After |
| --- | --- | --- | --- |
| `blocked-placement` | Choose Great Scientist. The only city is an island with a Worker occupying its civilian slot. | The picker closes, no Scientist appears, and the free choice is lost. | Placement error retains the choice. Close the picker, disband the Worker, then use the great-person turn button and choose again; the Scientist appears. |
| `maya` | Choose Scientist or Engineer for the first of two free picks. | Only the remaining Maya pool is selectable first; the second ordinary pick is unrestricted. | Same successful choices and bookkeeping through the shared operation. |
| `ordinary` | Choose a free great person in Gods & Kings. | One unit appears and the choice is consumed. | Same successful result. |
| `ordinary-vanilla` | Choose the Khan as Mongolia in Vanilla. | The civilization-specific equivalent is granted. | Same successful result. |
| `no-city` | Try the free choice before founding a city. | Selection cannot place a unit, but consumes the choice. | Options are unavailable and the choice is retained. Close, found a city with the Settler, and reopen via the turn button. |
| `empty-maya-pool` | Inspect the disabled choices and try returning to the map. | All candidates are disabled and the picker cannot be closed. | The picker explains that no option is available and permits returning to the map; the turn button continues to require the unresolved choice. |

`empty-maya-pool` deliberately represents an inconsistent pending-choice state for recovery testing; ordinary Maya triggers refill an empty pool before awarding a new choice. Stale requests, invalid unit names and automated selection are covered by engine tests because those inputs are not ordinary picker interactions.

The saves have one Human player, avoiding an extra hotseat-ready screen. No menu navigation is required: the private development launcher waits for ordinary desktop initialization, then invokes the existing save loader and opens the game. The engine's regular desktop entry point has no save-file startup argument, so this small launcher remains in the host's test tooling.

## Repository boundary

The engine contribution is committed as `df4f61112b89f3e6b1bd9efc299175b48261ea5c` and published on the maintained fork's `experimental-changes` and `free-great-person-selection` branches. The host records that commit in `vendor/Unciv`. The before comparison stays at its parent, `d77a312ed6ba0d127fe4efdfbcdc9f4918c0b5fa`.

S04 verification passed eight engine tests and 13 host tests. All six saves passed loader/placement checks. Real desktop smoke checks also verified automatic loading into `GreatPersonPickerScreen` for `blocked-placement` on both the original and changed engine. Those smoke checks verify startup, not the full manual click sequence above.

Engine code and tests belong to the engine Git repository; private launch tooling and integration notes remain here. Public contribution text must describe the shared selection behavior, the existing picker and validation without private project context. See the [development workflow](Development-workflow.md).
