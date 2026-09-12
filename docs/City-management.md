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

## Prepared scenario

Create a fresh peaceful match with two neighboring cities per civilization:

```sh
./gradlew prepareCityManagementFixture -PcityManagementFixtureDir=/tmp/civ-city-management-1
uv run civ-agent host start --state-dir /tmp/civ-city-management-1 --port 8774
```

Both Rome and Greece receive a capital, a second city, 1,000 gold, specialist
buildings, a shared worked tile and food close to the next population threshold.
The setup uses bundled Vanilla rules. Preparation writes the checkpoint and private
`players.json`, and refuses to overwrite an existing checkpoint. The host creates
operator credentials and TLS configuration when launched. Omit `--new`.

Start each service in a separate terminal, then start scheduling:

```sh
uv run civ-agent agent-server start Rome --state-dir /tmp/civ-city-management-1 --port 8887
uv run civ-agent agent-server start Greece --state-dir /tmp/civ-city-management-1 --port 8888
uv run civ-agent match start --state-dir /tmp/civ-city-management-1 --max-turns 6 --max-invocations 16 --max-seconds 3600 --turn-timeout 300
```

The usual `--model` and `--reasoning-effort` options apply; see
[local commands](Local-launchers.md). Exercise instructions appear only for this
scenario. Each player should change focus, assign/unassign specialists, reset
citizens, transfer a shared tile, sell the capital's University and inspect the
refund and removed slots. Keep the other University's Scientist assigned over
turns to observe accumulated points. Keep Avoid Growth enabled for a round,
then disable it and verify population growth. Models choose their actions; a
scripted test passing does not mean a live run completed these objectives.

Inspect the committed native save with:

```sh
uv run civ-agent host inspect Rome --state-dir /tmp/civ-city-management-1
```

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
Fixture preparation also participates in the credential/overwrite regression test.

```sh
./gradlew build :Unciv:tests:test
```

City border/culture forecasts, full overview tooltips, faith purchases, construction
placement choices, great-person unit abilities and embedded human play remain
outside this slice. The [coverage ledger](Player-api-coverage.md) tracks those gaps.
Visual desktop acceptance and a live-model run of this new scenario remain separate.
