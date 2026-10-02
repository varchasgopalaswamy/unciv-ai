# Victory and turn decisions

Scope: close the remaining victory decisions in the bundled **Civ V - Vanilla**
player flow. The ordinary Rome–Greece–France launcher enables Scientific,
Cultural, Domination, Diplomatic and Time victories. This is not an unrestricted
full-game or expansion-feature acceptance claim.

## Player interface

| Decision | Observation | Command |
| --- | --- | --- |
| Assemble a spaceship part | `unit_details.spaceshipAssembly`: title, availability, reasons, consumption | `ADD_SPACESHIP_PART`, `target=unit handle` |
| Choose a world leader | `player_turn.victory.ballot`: own ballot status, countdown, legal candidates | `CAST_DIPLOMATIC_VOTE`, `selection=civilizationId` |
| Abstain | The same ballot, with `canVote=true` | `ABSTAIN_DIPLOMATIC_VOTE`, no target or selection |
| Read election results | `player_turn.victory.votingResults`: native threshold, winner and permitted ballot rows | `ACKNOWLEDGE_DIPLOMATIC_RESULTS`, no target or selection |

The compact turn overview retains the entire ballot/results decision and flags
units that can assemble a part. Assembly requires a unit detail read; voting
and acknowledgement use the full decision already delivered in the overview.
Assembly and voting require strategic intent, like other consequential choices.
All four commands require a freshly observed player revision. They use the
ordinary durable command receipts and checkpoint transaction.

`receipt.victory` records the actual assembled count, vote/abstention, or the
acknowledged results. Final `player_result` reports also retain election results,
including for players whose next turn will never begin because the election won
the game. Terminal reports are read-only; no acknowledgement is needed to finish.

## Victory Status observations

`victory_progress` defaults to `sections=["our_status","global_status","rankings"]`.
It is already prepared at gameplay startup. It returns
the turn limit, native victory milestones, permitted rival next milestones,
and current rankings with short historical comparisons. Current policy-branch
and capital objectives supplement the existing spaceship-part counts.
Policy-branch milestones include `objectiveRequirement` with `rule: "AT_LEAST"`,
`requiredCount` and `completedCount`. Listed branches are alternatives for meeting
that count, not a requirement to finish every branch.

Optional selectors:

- `names`: enabled victory names for own/global status.
- `sections`: any of `our_status`, `global_status`, `rankings`, `charts`,
  `demographics`. `availability` reports why a section is inaccessible.
- `metrics`: available native metric names, in returned canonical column order.
- `civilizations`: disclosed names for global status, rankings and charts.
  Filtering a ranking does not change the population against which ranks are
  calculated. Demographics always retains its native aggregate scope.
- `fromTurn`, `toTurn`: inclusive chart range. Both require `charts`; the
  default is the last 20 turns including the current turn. A page contains at
  most 50 turns and 2,000 history/live numeric cells. Continue with
  `nextFromTurn` and retain `requestedToTurn` as `toTurn`.

```json
{"sections":["charts"],"metrics":["Production","Population","Technologies"],"civilizations":["Rome","Egypt"],"fromTurn":60,"toTurn":95}
```

`metrics` defines the columns shared by ranking `values`, `ranks`, trend
`changes`, and each chart series. Charts return a `turns` axis and a `history`
matrix for each civilization. Missing observations are `null`, never zero or
interpolated. `currentValues` is a separate live point at `currentTurn`;
recorded history is retained even if a record already exists for that turn.
Ranking trends use the latest completed-turn record and the earliest available
record within ten turns before it; each row reports its actual endpoints.
Trends are absent when charts are unavailable or two suitable records do not
exist. Default responses contain no chart history.

Metric meanings follow the engine: `Technologies` counts researched technologies,
`Culture` counts adopted policies excluding branch completion, `Gold` is treasury,
`Growth` is net food per turn, and `Force` is the native military rating including
its treasury adjustment. `Population`, `Production`, `Territory`, and
`Happiness` retain their native meanings. `TilesExplored` is available only when
the game's additional-ranking-types setting enables it. These fields are not
forecasts or strategic instructions.

Total score is excluded from player observations, including the startup preload,
strategic reviews, rankings, charts and demographics. Explicit `Score` requests
are invalid. Native scoring and Time-victory rules remain unchanged. Scores remain
available in operator telemetry and the dashboard, with breakdowns in telemetry.

Visibility is enforced before ranking, sorting, or reading history. Unmet
civilizations are omitted from named rankings and charts; their raw series and
UI ordering are not exposed through anonymous handles. Native statistics/tab
settings apply, including the postgame/spectator exceptions. Hidden opponent
statistics also suppress comparative demographics. Defeated ranking rows have
no current value/rank; permitted chart history remains readable. Rankings are
among disclosed living civilizations, with native alphabetical tie ordering.

When the Demographics tab is enabled, its own rank/value, best/average/worst
statistics use the native recorded snapshots. Best/worst numeric values remain
available when their civilization is unmet; the identity is concealed. Named
rankings and chart histories retain their contact restrictions. Repeated mid-turn
reads cannot track changes to opponents' treasuries
through those aggregates when the corresponding recorded values exist. Before
a metric has any recorded value, the native live fallback applies. These
observations do not establish that a popup was read, mutate the
game, alter victory conditions, or require any particular strategy.

## Engine behavior and desktop callers

`PlayerVictoryOperations` is shared with `UnitActionsFromUniques`,
`DiplomaticVotePickerScreen` and `DiplomaticVoteResultScreen`.

- Assembly requires the owned part's native `AddInCapital` unique and its owner's
  capital city center. Native assembly does **not** require remaining movement.
  It increments `currentsSpaceshipParts` and destroys the unit. The host adds no
  resource, technology, treaty or movement rule.
- Voting uses the desktop's known, living **other major civilizations**. A null
  native ballot is explicit abstention and resolves the choice. No self-voting,
  unknown candidates, duplicate voting or off-turn mutations are accepted.
- Acknowledging results sets the native `ShowDiplomaticVotingResults` flag to -1.
  It does not clear ballots, rerun voting, or advance the turn.
- Native `VictoryManager` supplies weighted totals, the threshold and winner.
  The host does not implement its own election formula.
- Election reset now clears `diplomaticVictoryVotesProcessed` alongside ballots;
  cloning preserves the flag. Previously, a game advanced in place could skip
  every election after its first processed result, while desktop cloning
  accidentally reset the flag each turn.

Ballots are private until native results are due. The host redacts unmet voter
and candidate identities even though the desktop results screen enumerates all
civilizations. Known rows voting for an unmet civilization report
`choice=UNMET_CIVILIZATION` with no identifier. An unmet winner is indicated by
`unmetWinner=true`; raw native winner text is not serialized. Native public vote
counts and the threshold remain visible at results time.

## Turn blocker audit

| Ordinary turn choice | Status |
| --- | --- |
| Free Great Person, including Liberty completion | Existing selection and placement handling |
| Empty city construction queue | Existing construction queue controls; Utopia and spaceship units use ordinary production |
| Research/free technology | Existing research queue and free technology selection |
| Policy/free policy | Existing adoption and native picker dismissal |
| Diplomatic vote and voting results | Implemented in this slice |
| Informational alerts, capture/liberation, player diplomatic demands and responses | Existing alert/capture/native diplomacy operations |
| Native AI offers and shared player negotiations | Existing mixed-player bridge and trade flow |
| Idle units | Existing reviewed end turn with explicit reason for deliberate idling |
| Religion, espionage, unsupported city-state or event decisions | Still explicit unsupported decisions; never automatically skipped |

The explicit desktop context-menu Next Turn bypass and automation timing remain
separate coverage gaps. This slice implements decisions in the ordinary turn
button flow; it does not add a force-end command.

## Validation

Focused automated checks pass:

- Eight engine tests: pure observations, native desktop assembly parity at zero
  movement, stale callback rejection, candidates, abstention, authority, results
  acknowledgement, native weighting/ties, and a later successful election.
- Seven host scenarios: full spaceship production/transport/assembly, winning
  final-part assembly, cultural production, Time
  victory, a failed election followed by a successful one through HTTPS and
  restart, privacy/unsupported-choice checks, and all four commands interrupted
  before/after atomic checkpoint replacement. Exact retries do not repeat effects.
- Python checks cover strict command shapes, compact decision delivery, required
  part details, forwarding a vote, and allowlisted terminal result persistence.
- Existing domination/capture and scheduler terminal-delivery tests remain the
  regression coverage for the fifth victory path and terminal activations.

The final default source build against published engine commit
`31524759eb456734da1048cdec2eecd8131ed9d6` passed all 87 host tests, including
the all-six-parts production scenario. The earlier broad regression run also
passed 174 related engine tests, and the focused victory/progress/domination
regression passed all 11 tests run before the production scenario was added.
The Python suite passed 429 tests and 90 subtests, with one prompt-length failure;
the corrected prompt passed its focused rerun. Together these cover all 430
Python tests. Ruff, formatting and Pyrefly checks passed. No Python distribution
build was used.

Rendered desktop acceptance passed on September 19, 2026. All eight screenshots
were visually inspected in `/tmp/unciv-victory-desktop-20260919-3/`:

1. Native Vote for World Leader turn prompt.
2. France/Greece choices and Abstain, with no self-vote.
3. Native prompt cleared after Rome votes for Greece.
4. Explicit Abstain confirmation.
5. Native results showing Rome's two-vote weight, a three-vote threshold and no
   winner in the prepared result.
6. Continue dismisses the results without deleting any ballots.
7. Enabled Add to The Spaceship button on an owned booster at **0/2 movement**.
8. Scientific victory with Apollo and all six assembled parts complete.

The desktop probe clicked the actual controls and checked their native effects.
The result screen used prepared opponent ballots; the HTTPS scenario exercises
actual native timing through both elections. The probe uses isolated exported
fixtures, not an embedded desktop connected to the live host. Earlier probe
attempts exposed only fixture first-contact popups and icon-aware label matching;
their outputs remain in directories ending `-1` and `-2`. The successful probe
exited and its desktop is stopped.

To repeat with a new output directory:

```sh
./gradlew test --tests uncivai.VictoryDecisionsIntegrationTest
./gradlew :Unciv:tests:test --tests com.unciv.logic.civilization.PlayerVictoryOperationsTest
./gradlew runVictoryDesktop -PvictoryDesktopDir=/tmp/victory-desktop-new
```

The [live victory exercises](../archive/reports/Victory-live-results.md) passed: 28 model
activations produced, transported and assembled all six parts for Rome's
Scientific Victory on turn 9; 36 activations completed a failed election and a
later Greek Diplomatic Victory on turn 12. All 51 artifact checks passed, with
correct final results delivered to every player. Every game agent used
`gpt-5.6-luna` with medium reasoning; the hosts and services are stopped.
Engine commit `31524759eb456734da1048cdec2eecd8131ed9d6` is merged and
published on `experimental-changes`. The host pins that exact revision;
the implementation, tests and private acceptance tooling are committed here.

This work does not establish an Ancient-to-victory autonomous game, full privacy
parity, city-state play, religion, espionage, naval/air combat, resignation or
One More Turn support.

## Spaceship production follow-up

The `spaceship-production` scenario prepares a late-game Vanilla position with
Rome's capital and a second industrial city, researched technologies, native
production buildings, improvements, railroads and strategic resources. It supplies
**no parts, stored part production or assembled-part counters**.

It then uses the HTTPS player API to queue and produce three SS Boosters, one
SS Cockpit, one SS Engine and one SS Stasis Chamber, advance ordinary turns until
each unit appears, move each from the second city to the capital, and assemble it.
Only the final assembly wins. Checks cover:

- Parts unavailable before the technology/Apollo/Aluminum prerequisites.
- Vanilla parts cannot be bought with Gold.
- Each produced unit consumes one available Aluminum; assembly releases it.
- No further part can be queued while that only spare Aluminum is in use.
- Assembly rejected in the production city; allowed in the capital.
- Assembled boosters count toward the native limit of three.
- All six units disappear through assembly and produce native Scientific victory.

This exercises normal production from zero rather than inserting completed units
or speeding up production counters. The earlier final-part/recovery fixtures remain
separate tests. The six construction/assembly milestones are retained in the JUnit
test output as `SPACE-PRODUCTION` lines.

The verified production sequence took 7, 6 and 6 rounds for the boosters, then
9 rounds each for the cockpit, engine and stasis chamber. Each was transported
and assembled before the next was queued. These timings describe the prepared
industrial city, not a general production-cost promise. The final rerun also
rejected Gold purchases for every part and passed formatting checks.
