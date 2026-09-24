# Basic combat

The player API now supports ordinary land melee/ranged attacks and city bombardment,
tactical orders, promotions and capture choices. The engine shares these operations
with the desktop controls. The [economy slice](Economy-and-unit-actions.md) adds land-unit
upgrades, pillaging and disbanding. Naval combat, air missions, nuclear attacks
and the remaining special unit actions still need their own implementations.
This is a bounded combat slice, not full game coverage.

## Observe and act

`player_turn.map.units[].combat` includes base strength, range, attacks remaining,
current orders and healing, available/disabled tactical orders, promotion experience
and prerequisites, and legal attacks. `cities[].attacks` lists bombardment targets.
Attack previews contain the target and origin coordinates, path, movement cost,
rounded displayed strengths, modifiers and health-capped damage ranges. Execution
and previews share the engine's damage-exchange calculation. Both damage rolls
apply together. If both units would die, the unit with less total damage (existing
wounds plus the uncapped incoming roll) survives at 1 HP; ties favor the defender.
A melee attack that reduces a city to its defeat threshold preserves the attacker
at 1 HP when necessary. This follows the lethal-exchange rules in
[Civ V's SDK combat source](https://github.com/dmnd/CvGameCoreSource/blob/master/CvGameCoreDLL_Expansion1/CvUnitCombat.cpp),
with Unciv's defeated-city representation of 1 HP.

Previews vary both damage rolls independently and never expose the random values
that will resolve an attack. Each combatant's range describes its own possible
damage: the two minima or maxima need not happen together. For example, equally
wounded units can each show 0–1 remaining HP because either may survive.
The `consequences` summary puts current and damage-based remaining HP together,
flags possible destruction/capture, and shows visible context around the attack
origin and possible melee advance. See [action consequences](Tactical-review.md#action-consequences)
for the limits of those estimates.

Agent services add a [tactical review](Tactical-review.md) and copyable `command`
arguments to combat, order and promotion offers. It highlights available city shots
even if no units are idle. City commands omit the preview's `attackFrom`; unit
attacks require it. Pydantic validates each command's argument shape before dispatch.

| Command | Choice source | Desktop source |
| --- | --- | --- |
| `ATTACK` | Owned unit's `combat.attacks`; copy both `destination` and `attackFrom` | BattleTable |
| `CITY_ATTACK` | Owned city's `attacks`; copy `destination` | BattleTable |
| `SET_UNIT_ORDER` | Available name in `combat.orders` | UnitActions |
| `PROMOTE_UNIT` | Available name in `combat.promotions.choices` | PromotionPickerScreen |
| `RESOLVE_CAPTURED_CITY` | Capture alert's available `capture.options` | AlertPopup |
| `MANAGE_CAPTURED_CITY` | Owned city's available `captureOptions` | CityView |

Every command uses the normal `requestId`, `epoch` and latest `revision`. Orders are
`FORTIFY`, `FORTIFY_UNTIL_HEALED`, `SLEEP`, `SLEEP_UNTIL_HEALED`, `SKIP` and `WAKE`.
Unavailable reasons explain restrictions. Promotion availability includes movement,
experience, prerequisites and the desktop rule against promotion after attacking.

Attacks target visible enemies already at war. Hidden targets, foreign handles and
unoffered origins are rejected. Planned routes ignore hidden obstacles; actual
movement may stop on discovery before attacking. Origins requiring unexplored
terrain for a damage calculation are omitted: move there first, then observe again.
The command rechecks the same target after movement. It never declares war as a
side effect. Read `receipt.combat.attacked`, actual damage, remaining health and
capture/destruction flags, then fetch another snapshot. A captured Settler that
becomes a Worker counts as a capture, not a casualty.

Exact command retries return the original receipt without applying damage, spending
movement or awarding experience again. Turn reviews record actual attack outcomes.
This also works over player HTTPS credentials after a terminal host restart while
the host remains in scheduled mode. Only that player's exact completed request is
treated as a receipt read; new or changed commands remain subject to the scheduler.
Interrupted turns restore their starting game, receipts, experience and combat-report
history using the existing operator rollback protocol.

## Capture decisions

A human player's conquered city retains its previous owner until the player reads
and resolves the capture alert, matching the desktop popup. The snapshot contains
its complete title, explanations and offered choices; the host requires a fresh
observation before accepting a decision. Choose among the offered annex, puppet,
raze, liberate or one-city-challenge destruction options. Capital, holy-city and
civilization restrictions remain enforced. Later city management can offer annex,
raze or stop razing.

`RESOLVE_CAPTURED_CITY` also handles recaptured civilian alerts: read the original
owner and return explanation, then select `ReturnCivilian` or `KeepCivilian` when
offered. The scheduler does not make these choices. Unsupported consequential
alerts continue to block end turn.

## Reports and game results

`combatReports` contains detached reports filtered by Unciv's attack-time visibility,
with the same text as its notifications. IDs persist across cloning and saving and
do not reveal counts of hidden events. The host collects reports before engine
history expires, keeps them in the checkpoint, and presents reports since that
player's last completed turn. Reading is nondestructive. Detailed turn transcripts
retain previously delivered reports; opponent reports never enter player archives.
The acting player also receives the immediate command's combat result.

Victory and defeat use shared engine text. `player_result` remains readable after
scheduling stops. A final capture choice commits the victory before another turn
is needed. The supervisor preserves that terminal turn, delivers each player's own
result and pending reports and full informational messages over HTTPS to
`result.json`/`result.md` in their workspace,
and retries interrupted deliveries without replaying gameplay. This does not start
an additional model conversation. Scores, victory progress, resignation and
postgame map exploration remain outstanding.

## Verification

Engine tests cover legal melee/ranged/city attacks, siege setup, spent movement,
wrong ownership/turn, spectators, hidden/invisible enemies, hidden routes and line
of sight, withdrawal privacy, civilian conversion and city conquest. Shared tactical
and promotion tests cover healing, wake/skip behavior, experience costs and stale
choices. Capture tests cover dispositions, capital/holy-city protection, liberation,
civilian returns and terminal result text. Combat-history regressions check stacked
captures, stable identities, persistence and permitted report filtering.

`CombatRunIntegrationTest` drives authenticated HTTPS against real saved games in
separate processes. It checks actual hit points and experience, rollback and exact
retry, bombardment and counterattack, healing and promotion, peace and continued
handoffs, capture choices, terminal results and restart. A raw desktop save containing a
winning position without recorded victory is normalized on load before queries. Existing diplomacy and
scheduler regressions remain part of the full build. Python service/scheduler tests
exercise final delivery, wrong bindings, lost acknowledgements and terminal turns
without a fabricated handoff. These scripted checks do not replace a live-model
acceptance game or a complete UI coverage audit.

Run the checks with:

```sh
./gradlew build
./gradlew :Unciv:tests:test
uv run pytest
```

Existing launchers pick up these tools after rebuilding. Use a new save for this
version; backward checkpoint compatibility is not part of this work. To test combat
manually, instruct the players to establish cities, meet, declare war, move a land
unit into range, inspect attack previews and execute an offered attack, then read
reports, heal/promote when eligible, and negotiate peace. Inspect the exported save
with `civ-agent host inspect` to verify the resulting position independently.

## Combat regression fixtures

Battlefield, conquest and victory states remain in the automated combat tests.
They are test data builders without standalone gameplay launchers or scenario
instructions. The current gameplay exercise is [trade-cycle](../scenarios/Trade-cycle-scenario.md).

## Lethal-exchange regression

The 2026-09-23 correction replaces early termination of the ordinary damage
exchange with Civ V's simultaneous damage and survivor adjustment. Desktop
previews, player-operation previews and host HP projections use the same engine
calculation. The default attack origin also now matches the attacker's tile, so
execution applies the river penalty shown in previews.

The saved Rome turn-38 attack was replayed from an isolated copy of
`build/notebook-evaluation-20260923/stalled-city/round-checkpoints/g31tlqkdxzlnr4-round-38.json`.
The 1-HP Warrior attacks the 12-HP Brute at (-8, -7) from (-9, -7):

| Observation | Before correction | After correction |
| --- | --- | --- |
| Previewed damage to Warrior | 1 | 0–1 |
| Previewed damage to Brute | 12 | 11–12 |
| Actual Warrior HP | 0 | 0 |
| Actual Brute HP | 11 | 1 |

The corrected receipt reports 11 damage to the Brute, `defenderDestroyed: false`
and `resolution: Completed`. Its remaining HP is hidden from Rome after losing
visibility; the operator's native-state check confirms 1 HP. The replay script,
input save and results are retained in `build/combat-fix-replay-20260924/`.
This is a deterministic attack replay, not a new model playthrough or completion
of Rome's interrupted five-turn evaluation.

Validation: 288 engine tests and 18 host integration tests passed, plus host
formatting checks. Coverage includes both sole-death directions, mutually lethal
rolls with unequal prior wounds, defender-favored ties, city capture at 1 HP,
ranged/city bombardment, independent preview bounds, read-only and hidden-roll
checks, river penalties, combat-history visibility, air/naval missions and exact
receipt replay. The combat-history boundary test's allowlist was also updated
to the existing `attackWithResult`/`nukeWithResult` entry-point names; the stale
names failed against code already present before this correction.
