# Basic combat

The player API now supports ordinary land melee/ranged attacks and city bombardment,
tactical orders, promotions and capture choices. The engine shares these operations
with the desktop controls. Naval combat, air missions, nuclear attacks, pillaging,
upgrades and the remaining special unit actions still need their own implementations.
This is a bounded combat slice, not full game coverage.

## Observe and act

`player_turn.map.units[].combat` includes base strength, range, attacks remaining,
current orders and healing, available/disabled tactical orders, promotion experience
and prerequisites, and legal attacks. `cities[].attacks` lists bombardment targets.
Attack previews contain the target and origin coordinates, path, movement cost,
rounded displayed strengths, modifiers and health-capped damage ranges. They do not
expose the random value that will resolve an attack.

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
python3 -B -m unittest discover -s scripts/tests -p 'test_*.py'
```

Existing launchers pick up these tools after rebuilding. Use a new save for this
version; backward checkpoint compatibility is not part of this work. To test combat
manually, instruct the players to establish cities, meet, declare war, move a land
unit into range, inspect attack previews and execute an offered attack, then read
reports, heal/promote when eligible, and negotiate peace. Inspect the exported save
with `scripts/inspect_game` to verify the resulting position independently.

## Ready-to-play combat scenario

Create a fresh exercise with `--combat-scenario`:

```sh
./scripts/launch_host --new --combat-scenario --state-dir /tmp/my-combat-match --port 8767
./scripts/launch_agent_service Rome --state-dir /tmp/my-combat-match --port 8867
./scripts/launch_agent_service Greece --state-dir /tmp/my-combat-match --port 8868
./scripts/run_agents --state-dir /tmp/my-combat-match --max-turns 12 --max-invocations 28 --max-seconds 5400 --turn-timeout 300
```

Run these in separate terminals, waiting for the host and both services to report
ready before starting the scheduler. Model and reasoning flags work as in ordinary
matches. The scenario flag is new-game-only and cannot be combined with
`--close-starts`; omit it when resuming.

The fixed 127-tile grassland map contains Rome/Athens capitals and the weakened
Antium/Sparta outposts. Each civilization has three Warriors and one Archer,
including an injured reserve and a veteran eligible for promotion. Both are already
at war; research and production queues are initialized. Ordinary fog and all normal
combat rules apply after setup.

Only this scenario's snapshots contain combat exercise instructions. Agents are
asked to promote, heal and wake units, use bombardment/ranged/melee attacks, puppet
then annex an outpost, and fight for the capitals. The host validates actions but
does not force this sequence or guarantee a model completes every check. Read the
saved receipts and inspect the game to verify what happened:

```sh
./scripts/run_agents status --state-dir /tmp/my-combat-match
./scripts/inspect_game Rome --state-dir /tmp/my-combat-match
./scripts/run_agents stop --state-dir /tmp/my-combat-match
```

For a focused capture/victory test, the test harness can prepare a separate
checkpoint with two Roman Warriors beside Sparta and Athens, both at 1 HP:

```sh
./gradlew prepareConquestFixture -PcombatFixtureDir=/tmp/my-conquest-fixture
```

To isolate terminal delivery from siege tactics, prepare a single final capital at
1 HP, with a healthy Roman Warrior beside it and no defending units:

```sh
./gradlew prepareVictoryFixture -PcombatFixtureDir=/tmp/my-victory-fixture
```

These prepare `exploration.json` without issuing gameplay commands and refuse to
overwrite an existing checkpoint. Each includes instructions for its specific
exercise and is intended for operator test harnesses; provision fresh player
credentials before using the normal checkpoint launcher. A victory in this prepared
position verifies the terminal flow, not the outcome of a longer ongoing match.
