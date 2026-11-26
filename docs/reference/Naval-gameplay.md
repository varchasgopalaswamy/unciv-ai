# Naval gameplay and destination orders

This sprint completes ordinary Vanilla naval actions and adds native multi-turn
destination orders. Unciv remains authoritative for movement, combat, costs,
resources, healing and unit consumption. Host checks additionally protect player
visibility and ownership. Social promises remain advisory.

## Scope and acceptance

- [x] Ship combat and coastal bombardment through shared native operations, with
  permitted previews, movement and actual outcomes.
- [x] Ship upgrades, pillage and disband; verify existing healing and promotions.
- [x] Native Work Boat resource improvements with consumption and resource effects.
- [x] Embarkation, ocean restrictions, landings and overseas settlement.
- [x] Distant destination previews, native persistent movement, progress and
  cancellation/replacement; no manual tile-by-tile routing by the agent.
- [x] Engine and HTTPS checks for native legality, affordability, visibility,
  exact retries, restart and interrupted checkpoint writes.
- [x] Rendered desktop comparison of the same operations.
- [x] Guided live Rome/Greece/France coastal exercise with funded, achievable
  objectives and a restart. Every game agent uses `gpt-5.6-luna`, medium reasoning.

## Starting inventory

Before this sprint, `PlayerUnitOperations` exposed engine-generated current-turn
paths and destination movement for non-air units. Persistent desktop destinations used
the native long-distance pathfinder. This sprint extends its existing speculative
planning mode to distant paths and native human-order continuation, protecting
hidden terrain and unseen units while retaining native movement effects.

`PlayerCombatOperations` and `PlayerUnitEconomyOperations` previously filtered out
water units. `PlayerUnitOrders` already supplied tactical orders and promotions
across unit types. Native Work Boats use `CreateWaterImprovements`, separate from
worker jobs and Great Person abilities.

Air missions, carrier aircraft commands, nuclear attacks, expansion mechanics
and mod content remain separate work. Ordinary native consequences of encountered
units must still be respected.

## Evidence

The default build against the published engine revision passes all 99 host tests,
including the five new HTTPS/recovery probes. All 198 native player-operation tests pass.
Movement, pathfinding, formations, history and overlay regression adds 186 passing
cases and six pre-existing skips. The full Python suite passes 442 tests and 90
subtests; formatting, Ruff and Pyrefly checks pass.

Scoped acceptance is complete. Engine commits `ddbb694c9` (destination orders)
and `b89aea260` (naval operations) are merged and published on
`experimental-changes`. The host records
`b89aea26089ccc0efbcaca8a63b76896352cbbe7` as its engine dependency.

## Player interface

1. Read `player_turn` and choose an owned unit and destination.
2. Call `movement_preview` with `target` and `destination: {x, y}`. It returns
   adjacent paths grouped by estimated turn, costs, `unexploredTiles` and `tilesOutsideCurrentSight`. The
   native planner treats unexplored terrain speculatively and ignores unseen units.
   Founding units also receive [settlement diagnostics](Exploration-and-settlement.md#settlement-diagnostics)
   for that destination. These distinguish route availability from city-site
   eligibility, including the native distance rule for different continents.
   Each stop's `afterMove` estimates remaining movement and action availability,
   with visible nearby enemies, owned military and visibility gaps. These
   [tactical consequences](Tactical-review.md#action-consequences) include
   `captureRisk` when a visible hostile could reach an unguarded civilian's stop.
   Changing terrain knowledge or occupancy can interrupt the planned route.
3. Issue `SET_UNIT_DESTINATION` with the same unit and destination. Both the host
   and agent gateway require the preview to match the current revision. The engine
   executes the first segment and stores its normal `moveTo` action. If that
   segment has `captureRisk`, explicitly accept it with `confirmAction: true` or
   choose a protected move. An unconfirmed request moves nothing and returns
   `CIVILIAN_CAPTURE_RISK`. Same-tile escort formations require no override.
4. Inspect `unit.movementOrder` and `receipt.movement`. Future movement executes
   through native turn automation. `CANCEL_UNIT_MOVEMENT` clears the order without
   spending movement; another destination replaces it. `MOVE_UNIT` remains the
   explicit current-turn operation and clears any previous destination.

Routes are estimates, not guaranteed arrivals. The engine replans each turn and
can stop for visible enemies, newly discovered obstacles, unavailable routes or
arrival. No hidden-unit route or stop query is exposed. A restart preserves the
native order and completed receipts; uncommitted previews must be read again.
Civilian routes check each future stopping tile for capture exposure and clear
the order with a notification before a risky segment. An earlier confirmation
does not authorize later risky segments.
Native order continuation is part of the checkpointed turn handoff, including
recovery before/after checkpoint replacement.

For an agent session, after reading `player_turn`:

```json
{"target":"unit3","destination":{"x":8,"y":-3}}
```

Send that to `movement_preview`, inspect its available route, then send this to
`player_command`:

```json
{"kind":"SET_UNIT_DESTINATION","target":"unit3","destination":{"x":8,"y":-3},"intent":{"reason":"Sail toward the chosen island"}}
```

The service supplies request IDs and revisions. Cancellation uses
`{"kind":"CANCEL_UNIT_MOVEMENT","target":"unit3"}` after a fresh overview and
`unit_details` read. A unit with no movement left can still cancel its order.

The headless runtime selects Unciv's existing **automated units move on turn
start** setting. Imported native automated orders use that same timing; this does
not add a new movement rule.

### Escort formations

One owned civilian and one owned military unit on the same tile can form an
escort using native `ESCORT` / `STOP_ESCORT` choices in
`unit_details(sections=["orders"])`. Air units cannot join a formation. The unit
roster and movement/order details show `escort: {partner, active}`, including a
co-located candidate before pairing. Each available choice has a copyable
`SET_UNIT_ORDER` command. Pairing and separating cost no movement.

After pairing, read a fresh `movement_preview` and issue `SET_UNIT_DESTINATION`
for either member. The engine plans and moves both together, including on later
turns. A direct movement instruction replaces both members' independent travel
and automation orders. Each preview stop includes the selected unit's
`afterMove` and the partner's `afterMove.escort`: both share the stop, but pay
their own terrain costs. Future stops use each unit's full movement allowance.
Discoveries can stop the pair early if either member exhausts its movement.

Both members show the shared `movementOrder`. Either can cancel it through
`CANCEL_UNIT_MOVEMENT` without separating the formation. `STOP_ESCORT` separates
them; any destination retained by a member then applies only to that unit.
Formation and destination state survive native saves. Normal visibility and
obstacle handling still apply. An escort can be defeated; pairing is not a
safety assessment, and a nearby military unit does not prevent civilian capture.

Work Boats and eligible Samurai advertise `unit.waterImprovement` at their current
tile. `CREATE_WATER_IMPROVEMENT` takes `target` and
`selection=waterImprovement.name`. Work Boats are consumed; Samurai use their
native instant Fishing Boats action, spending one movement and retaining the
unit. Military owns this action for Samurai; Main retains ownership for Work Boats
and units assigned to escort duty. Read `consumesUnit` on the offer. The desktop and API revalidate the same
native operation, including tile, technology, movement and ownership requirements.
`receipt.waterImprovement` records the improvement, actual unit consumption,
displayed movement before/after and relevant available-resource totals. Exact request replay
returns the same receipt without spending movement again. Bonus resources have no
empire stockpile. Existing `ATTACK`, `UPGRADE_UNIT`, `PILLAGE`, `DISBAND_UNIT`,
orders and promotions also accept ordinary ships, with native requirements.
Vanilla's ranged ships cannot capture a coastal city by bombardment alone.

## Reproduce acceptance

```sh
./gradlew test --tests 'uncivai.NavalIntegrationTest'
./gradlew :Unciv:tests:test --tests 'com.unciv.logic.civilization.PlayerNavalOperationsTest' --tests 'com.unciv.logic.civilization.PlayerMovementOrdersTest'
.venv/bin/pytest tests/test_movement_orders.py
./gradlew runNavalDesktop -PnavalDesktopDir=/tmp/naval-desktop-new
./gradlew generateNavalLiveFixture -PnavalLiveDir=/tmp/naval-live-new
.venv/bin/python -m tests.live_naval --state-dir /tmp/naval-live-new --port 8951 --max-turns 1 --max-invocations 30 --max-seconds 7200 --turn-timeout 600 --max-concurrent-jobs 3 --jobs-per-civilization 1
.venv/bin/python -m tests.live_naval --state-dir /tmp/naval-live-new --port 8951 --resume --max-turns 4
.venv/bin/python -m tests.verify_naval_live /tmp/naval-live-new
```

Each directory must be new for fixture creation. Both start and resume use
`gpt-5.6-luna` with medium reasoning for every gameplay and communication job.
The wrapper saves `before-restart.json` at the first-round stop, owns process
cleanup and does not issue gameplay commands. Resume preserves confirmed actions
when a model job was interrupted. Run Gradle tasks sequentially when they share
the same checkout; live host startup also invokes Gradle.

These checks cover the stated Vanilla actions. They do not establish every
civilization-specific unique, complete naval warfare strategy, aircraft/carrier
commands, expansion mechanics or an unrestricted Ancient-to-victory game.
