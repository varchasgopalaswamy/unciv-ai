# City management

Agents can change focus and growth settings, manage specialists, sell buildings,
and transfer worked tiles between their own cities. These commands use shared
engine operations also called by `CityView` and the desktop city screen.

## Observations and commands

Read `cities[].management` alongside the existing `development` yields, population,
food, focus, growth setting and worked/locked tiles. Each management action reports
`available` and `unavailableReasons`. Waiting or paused players can inspect their
cities but cannot change them. Puppets retain their ordinary management restrictions.

| Command | Fields beyond request ID, epoch and revision | Effect |
| --- | --- | --- |
| `SET_CITY_FOCUS` | City `target`, `selection` from `management.focusOptions` | Applies the selected UI focus and reassigns citizens; preserves locked tiles |
| `SET_AVOID_GROWTH` | City `target`, `enabled` | Changes the native growth cap and reassigns citizens |
| `SET_MANUAL_SPECIALISTS` | City `target`, `enabled` | Enabling preserves assignments; disabling reassigns citizens automatically |
| `ASSIGN_SPECIALIST`, `UNASSIGN_SPECIALIST` | City `target`, specialist `selection` | Adds/removes one citizen and enables manual specialists; adding requires an unused slot and a free citizen |
| `RESET_CITIZENS` | City `target` | Clears worked tiles and locks, then performs native automatic allocation; manual specialists remain assigned |
| `SELL_BUILDING` | City `target`, building `selection`, `confirmAction: true` | Removes the offered building for its native refund and reallocates citizens whose specialist slots disappear |
| `REASSIGN_CITY_TILE` | Receiving city `target`, offered shared-tile `destination` | Releases the donor's worker and lock, then assigns a free recipient citizen to the tile; ownership stays unchanged |

Every command requires a fresh `player_turn` read at the submitted revision. A
receipt's new revision alone does not acknowledge new options. Sales also require
reading the full offered confirmation. Ordinary sale restrictions apply: no wonders,
unsellable or free buildings, and at most one sale per city per turn unless the
game's god mode permits more.

The manual/automatic toggle is available only when the city has specialist slots,
matching the desktop panel. Specialists expose assigned citizens, slots, UI-precision yields, base great-person
points and legal add/remove actions. `greatPeople` lists city points per turn,
empire accumulated points, the next threshold, and base/percentage source rows.
The accumulated counter is empire-wide even though the contribution is from one
city. The query does not initialize saved counters. `buildingSales` contains the
refund, base maintenance, description, confirmation and eligibility. Base maintenance
is the building's ruleset value; re-observe empire maintenance for the actual effect.

Shared tiles identify the current working city using its player-scoped handle.
Transfers need a free recipient citizen, a visible, productive, unblocked tile
within range, and a donor that can release it. Unwork another tile first if needed.
The desktop city tile panel offers the same transfer operation.

Read `receipt.cityManagement` for actual affected city assignments, settings, yields,
removed buildings and gold change. Sales also populate `receipt.economy`. Re-observe
for current eligibility, growth and maintenance. Exact retries return the same
receipt without repeating a sale or moving another citizen.

`player_inspect(subject="CITY_FOCUS", target=city_handle)` compares the available
focus operations without changing the live city. Each alternative runs the native
operation on a fresh detached game copy, preserving locked tiles, growth settings
and manual specialist assignments. It reports displayed yields, food, growth and
starvation estimates, current-construction timing and resulting assignments. A
named focus can reassign citizens even when it matches the current setting;
keeping current assignments requires no command. Previews hold population and
the current queue fixed, and exclude future growth, improvements and enemy actions.

## Regression fixtures

City management is exercised by internal two-city-per-player fixtures under
`src/test/kotlin/uncivai/fixtures`. They retain specialist buildings, shared tiles
and near-growth food stocks for scripted command verification. The current
launchable gameplay scenario is [trade-cycle](../scenarios/Trade-cycle-scenario.md).

## Verification and limits

`PlayerCityManagementOperationsTest` exercises UI delegation, free citizens,
specialist capacity, focus/locks, growth, refunds, lost specialist slots, per-turn
sale limits, free/unsellable buildings, ownership and inactive/puppet/spectator
guards, shared-tile eligibility, immutable options and read-only queries.
`GreatPersonThresholdTest` checks pure queries and the first generated person's
threshold increase without a preceding UI query.

`CityManagementRunIntegrationTest` runs separate JVMs and authenticated HTTPS clients.
It checks own-city observation precision and foreign-state independence, plays the
controls for both civilizations, and asserts native city state, gold, growth and
great-person points. It exercises unread options/confirmations, invalid fields,
hidden coordinates, foreign handles, stale requests, exact receipts, interrupted-turn
rollback and process restarts. Replaying receipts must preserve checkpoint bytes.

`NativeAdviceIntegrationTest` also compares focus previews with native execution,
checks unchanged live save bytes and repeated results, preserves tile locks and
manual specialists, and checks independence from changed private foreign gold and
population. Unknown city handles are rejected and inactive players receive no
available focus operations.

```sh
./gradlew build :Unciv:tests:test
```

City border/culture forecasts, full overview tooltips, faith purchases, construction
placement choices, great-person unit abilities and embedded human play remain
outside this slice. The [coverage ledger](Player-api-coverage.md) tracks those gaps.
Visual desktop acceptance and a live-model run of this new scenario remain separate.
