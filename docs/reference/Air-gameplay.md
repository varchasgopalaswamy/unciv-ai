# Air warfare and special military actions

This sprint adds the missing Vanilla air, missile, nuclear and paradrop actions.
Unciv owns legality, costs, capacity, combat and diplomatic consequences. Queries
use player-visible information; execution revalidates against the native state.

## Scope and acceptance

- [x] Shared aircraft rebasing and city/carrier capacity, including transported
  aircraft moving with their carrier and native carrier-loss consequences.
- [x] Aircraft and guided-missile attacks through existing combat operations;
  air sweeps, automatic interception readiness, previews and filtered reports.
- [x] Nuclear targeting with public blast geometry, known affected parties,
  explicit confirmation and native diplomatic/combat outcomes.
- [x] Paradrop previews and execution, sharing native terrain, range and timing.
- [x] Aircraft upgrades/disband; reuse existing construction and promotions.
- [x] Host commands, compact observations, scoped preview memory and retry,
  visibility, checkpoint and restart checks.
- [x] Native desktop comparison using the same operations.
- [x] Guided live Rome/Greece/France acceptance with achievable objectives and a
  restart; all game jobs use `gpt-5.6-luna` with medium reasoning.

## Native inventory and implementation constraints

`UnitMovement` already relocates aircraft and paradropping units. City air slots,
carrier filter/capacity and doubled aircraft rebase range are native rules.
`UnitActionsFromUniques` supplies paradrop preparation and air-sweep selection.
The shared operations expose these actions without requiring a UI selection
mode. Carrier movement and destruction already affect transported units.

`TargetHelper`, `Battle`, `AirInterception` and `Nuke` already resolve the missions.
Interception is automatic: an eligible unit retains native interception readiness
when it has not used the necessary movement/attack allowance. No additional
intercept order is needed. Sweeps do not identify an unseen interceptor in advance.

`Nuke.mayUseNuke` checks hidden victims as part of native execution legality.
Planning must inspect only permitted information and mark uncertainty, then use
the full native check at execution. Previews must never enumerate hidden units,
unknown civilizations or private interception readiness. Outcomes use the existing
player-filtered combat history, not raw attack events.

Existing production/research, promotions and combat-report persistence are reused.
Prepared scenarios verify this scope, not unrestricted full-game play.

## Player interface

`unit_details.air` supplies mission ranges and availability, owned city/carrier
bases with remaining capacity, transport relationships and the unit's native
interception readiness. The compact roster lists mission names, transported
aircraft and interception readiness. Enemy interceptor readiness is never queried.

| Action | Preview | Command and outcome |
| --- | --- | --- |
| Rebase aircraft | `mission_preview`, mission `REBASE` | `REBASE_UNIT`; native city/carrier placement and movement expenditure |
| Paradrop | `mission_preview`, mission `PARADROP` | `PARADROP`; native range, visibility, terrain, occupancy and timing |
| Air sweep | `mission_preview`, mission `AIR_SWEEP` | `AIR_SWEEP`; native interception exchange, or attack expenditure if unopposed |
| Nuclear launch | `mission_preview`, mission `NUCLEAR_STRIKE` | `NUCLEAR_STRIKE` with `confirmAction=true`; native wars, interception and detonation |
| Air or guided-missile attack | `unit_details.combat.attacks` | Existing `ATTACK`; actual damage, resolution and unit consumption |
| Aircraft economy | Existing unit/construction details | Existing purchase, promotion, upgrade and disband commands |
| Carrier travel | `movement_preview` | Existing destination orders; native transport moves the aircraft |

After reading `player_turn`, select the unit and destination. For example:

```json
{"target":"unit3","mission":"REBASE","destination":{"x":-3,"y":-2}}
```

Send that to `mission_preview`, inspect the result, then send this through the
agent's `player_command` tool:

```json
{"kind":"REBASE_UNIT","target":"unit3","destination":{"x":-3,"y":-2},"intent":{"reason":"Embark the fighter on our carrier"}}
```

The service supplies request IDs and revisions. Each special mission requires a
matching available preview at the current revision; it must be read again after
restart or any observed state change. The host revalidates execution with Unciv.
Completed receipts replay exactly across retries and restarts. Injected failures
before and after checkpoint replacement cover all four mission types.

`receipt.mission` records actual movement, health, transport, consumption, newly
observed wars and permitted reports. Combat and nuclear receipts report the native
resolution (`Completed` or `Intercepted`). A completed sweep may have no opponent.
An intercepted nuclear bomber can still have started wars: the native declaration
precedes interception. Ordinary air damage estimates exclude interception; actual
receipts include it. Unciv does not give the firing player the victim's nuclear
notification, so the explicit launch resolution confirms the firing outcome.

Nuclear previews expose explored blast tiles, visible victims and warnings about
affected parties based on current or remembered ownership. Hidden ownership changes
do not refresh those warnings. Destination exploration/visibility and counts of
unexplored blast tiles and blast tiles outside current sight describe what is known.
Unseen victims can still change native legality or effects; an unavailable result
does not identify a hidden victim.

Sweep and interceptable nuclear previews list visible units with interception
capability in range. They do not inspect private movement or attacks spent, select
the eventual interceptor, or predict its damage. Hidden interceptors remain hidden.
These restrictions also apply to player revisions and persisted reports.

## Automated and desktop evidence

All 493 selected native player-operation, combat, carrier, movement and overlay
regressions pass, including 18 new air-operation cases. All 103 host tests pass,
including four new air HTTPS/recovery probes and updated API contract assertions.
The default build and formatting checks pass against the published engine pin.
The Python suite passes 447 tests and
90 subtests; Ruff formatting, lint and Pyrefly checks pass.

`/tmp/unciv-air-desktop-20260920-2` contains 16 rendered screenshots and a
`COMPLETE` marker. All were inspected. Actual desktop clicks exercise rebasing,
carrier movement, paid aircraft upgrade, intercepted bombing, sweep combat,
guided-missile consumption, paradrop and nuclear attack. Shared operations supply
the same native effects to desktop and HTTPS callers. Carrier destruction,
full-base upgrades, affordability, promotions, invalid targets, hidden-state pairs
and checkpoint failure recovery have separate automated checks.

The [live exercise](../archive/reports/Air-live-results.md) completed three rounds and nine handoffs
with all 30 artifact checks passing. All 15 model jobs used Luna with medium
reasoning. The planned restart preserved transport, units and receipts; subsequent
carrier combat, nuclear launch and rebasing succeeded. All services are stopped.
Scoped acceptance is complete. Engine commit
`f2408cde26543e404ea2844b18c1e648fa97691d` is merged and published on
`experimental-changes`; the host records that exact dependency revision.

## Reproduce acceptance

These commands use the recorded engine checkout for both code and assets:

```sh
./gradlew test --tests 'uncivai.AirIntegrationTest'
./gradlew :Unciv:tests:test --tests 'com.unciv.logic.civilization.PlayerAirOperationsTest'
.venv/bin/pytest tests/test_air_missions.py
./gradlew runAirDesktop -PairDesktopDir=/tmp/air-desktop-new
./gradlew generateAirLiveFixture -PairLiveDir=/tmp/air-live-new
.venv/bin/python -m tests.live_air --state-dir /tmp/air-live-new --port 8961 --max-turns 1 --max-invocations 40 --max-seconds 7200 --turn-timeout 1200 --max-concurrent-jobs 3 --jobs-per-civilization 1
.venv/bin/python -m tests.live_air --state-dir /tmp/air-live-new --port 8961 --resume --max-turns 3
.venv/bin/python -m tests.verify_air_live /tmp/air-live-new
```

Fixture directories must be new. The live wrapper selects the same pinned engine,
supplies objectives and manages service cleanup; it issues no gameplay commands.
It saves `before-restart.json` at the first-round stop. Every gameplay and chat
job uses `gpt-5.6-luna` with medium reasoning. Run Gradle tasks sequentially;
live host startup also invokes Gradle.

This prepared Vanilla position does not establish unrestricted full-game play,
every civilization-specific unique, expansion mechanics, mod content or embedded
human desktop control. The [coverage ledger](Player-api-coverage.md) retains those
broader gaps.
