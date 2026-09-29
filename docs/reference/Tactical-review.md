# Tactical and strategic review

`player_turn` includes compact factual reviews derived from the permitted snapshot.
They inform decisions without imposing strategic priorities.

`tacticalReview` shows:

- Military counts; the full owned roster, health, strength, roles, orders and
  attack counts are provided once in `player_turn.units`.
- Visible hostile military units and owned civilians or military units within
  two hexes of them. The Kotlin host uses engine visibility, unit classification,
  diplomatic status and map distances, including wrapping maps. Enemy civilians
  are not classified as attacking threats.
  `militaryNearHostiles` includes healthy units as well as injured ones.
- `capturableCivilians`: currently visible foreign-owned capturable civilians,
  with position, current owner, war status, visible military guard (if any), and
  `capture.unitIfKept`. `capture.isOurCapturedUnit` identifies recovery of this
  player's own unit. Recovering your own Settler preserves it; keeping another
  player's Settler converts it to the native Worker replacement. A Worker stays
  a Worker. This describes the result of a successful legal capture and keeping
  the unit, not whether an owned unit can capture it this turn. Clearing a guard
  with ranged fire alone does not capture the civilian. Native uncapturable
  units are excluded. Other players' original ownership is not disclosed.
  Entries also appear in `map_region` unit observations as `capture`, and
  disappear when the civilian leaves visibility, is destroyed, or joins the
  player's roster.
- The return/keep popup in `alerts` includes each choice's `civilianOutcome`
  (recipient and resulting unit type), an explanation of any conversion, and a
  copyable `RESOLVE_CAPTURED_CITY` command. For a rescued foreign Settler, return
  gives its original owner a Settler; keep gives this player a Worker. The
  temporarily owned Settler in the roster is still awaiting that choice.
- Every currently offered city bombardment, damage ranges, copyable arguments and
  its required `city_details(sections=["combat"])` inspection.
- Confirmed losses from the delivered combat reports, city count, Settlers and
  cities currently producing Settlers. A Settler later in the queue is not current
  production. Treasury, income and happiness appear once in `economy`.

Proximity is a warning, not an enemy movement prediction. It ignores hidden units
and does not establish that a destination is safe. A military unit on the same
tile is reported as `otherMilitaryOnSameTile`, excluding the subject unit itself,
without promising that it can protect a civilian. Damage ranges
are engine previews; receipts and later observations establish actual results.
The unit roster also shows co-located escort partners and active formations.
[Escort orders](Naval-gameplay.md#escort-formations) let a civilian and military
unit use one native destination order, with shared stops and separate movement
estimates, instead of requiring the player to coordinate two moves.

## Action consequences

Detailed unit/city combat offers include `consequences`: current HP alongside
damage-based remaining HP ranges, `destructionPossible`, and capture possibility.
They show visible hostile units, other owned military units and unseen-tile counts
within two hexes of the attack origin and possible melee advance destination.
The attacker itself is excluded from the friendly-presence list. Enemy positions
are the positions visible at inspection time, including the target; this is not a
simulated board after combat. Native damage bounds include the lethal-exchange
adjustment that leaves one survivor when both damage rolls would kill. The
remaining-HP ranges are separate bounds; their endpoints need not occur together.
Withdrawal, aircraft interception and triggered effects such as healing can still
change the outcome. City HP bottoms out at one; capture remains separate from
destruction, and a captured city's later HP is not its pre-capture damage projection.
`attackerConsumedOnAttack` separately identifies native self-destructing attacks,
such as a Guided Missile, even when the damage-based HP range stays positive.

Movement options and each turn stop in `movement_preview` include `afterMove`:
remaining movement, attack availability, founding availability for settlers, and
the same endpoint context. These derive from the native planned path and costs.
`NO_MOVEMENT` makes a move-then-attack/found mistake visible before committing.
`VISIBLE_TARGET_OFFERED` means a current native attack offer uses this origin;
`NO_VISIBLE_ATTACK_OFFERED` does not prohibit attacks against newly revealed targets.
Future action opportunities and uncertain settlement sites say `RECHECK_ON_ARRIVAL`.
Discoveries or changing occupancy can interrupt the path. Friendly proximity is
not a guarantee of protection, and unseen tiles do not imply hidden enemies exist.

These details live in action inspections; the compact unit summary still contains
counts of movement and attack options. Combat risks remain advisory. Civilian
movement uses the explicit confirmation described below; native-legal movement
remains possible with an override.

### Civilian capture confirmation

Single-turn move offers and every `movement_preview` stop can include
`afterMove.captureRisk`. It lists currently visible hostile units that could reach
an unguarded civilian's stopping tile with a fresh movement budget. The engine
uses movement costs, terrain, roads and zone of control rather than a fixed
proximity radius. Unseen terrain is speculative; enemy exploration, hidden units
and private open-border agreements do not determine the visible route estimate.
This is potential capture exposure, not a prediction of the enemy's chosen action
or a guarantee of safety when omitted.

`MOVE_UNIT` and the current segment of `SET_UNIT_DESTINATION` require
`confirmAction: true` if that stop has capture risk. Otherwise the command returns
`CIVILIAN_CAPTURE_RISK`, `applied: false`, the risk details and a copyable command
with `confirmationField: "confirmAction"`. No movement or order change occurs.
Templates never prefill consent. The player may choose another destination,
arrange an escort, or deliberately accept the risk with their own intent.

A move that keeps a linked civilian/military formation together succeeds without
confirmation, whether issued through the civilian or the military unit. Moving
onto a friendly military unit or friendly city is also protected against immediate
unopposed capture. Nearby military units do not qualify. Protection does not mean
the escort or city will survive combat.

Confirmation covers the current move only. Each subsequent automatic route
segment rechecks danger. If its stopping tile exposes the civilian, the engine
clears the destination order before moving or spending movement and records a
notification. The next decision can arrange protection, choose a different route,
or inspect and explicitly confirm another risky move. No persistent override is
stored in the save.

## Strategy and turn review

`strategyReview.assessments` lists active strategy assessments with advisory
notices, by ID and reason. There are no scheduled review dates or required topics.
Confirmed losses and changes in city count or war status notify the strategy even
without subject tags. See [structured memory](../architecture/Structured-memory.md).
The service does not judge an assessment or decide which response is desirable.

Before `END_TURN`, native pending decisions must be resolved and the current
native review must have been observed by the service, which supplies its ID.
The model reviews `units` and pending decisions; `turnReview` contains only the
confirmed-action count. Idle units, available bombardments and
nearby threats do not block the turn. A player may optionally supply `deferReason`
as audit context; neither the gateway nor host requires an explanation.

## Command arguments

`game_commands.py` defines Pydantic command variants for every currently exposed
host command. Their advertised schemas and Python validation share that definition;
the Kotlin host still validates legality and permission before execution. Invalid
field combinations return `INVALID_COMMAND_ARGUMENTS`, required/allowed fields and
recovery guidance before a request or intent is dispatched.

Combat, unit-order and promotion offers contain a `command`. Copy it after reading
the offer, and add your own intent for consequential choices:

```json
{"kind":"CITY_ATTACK","target":"city2","destination":{"x":-6,"y":-1},"intent":{"reason":"Damage the Brute threatening the Worker"}}
```

City previews contain `attackFrom` for display, but `CITY_ATTACK` must omit it.
Unit `ATTACK` commands require the exact offered `attackFrom`. Credentials, request
IDs and revision tracking remain code-owned. Apply the returned state update after
each mutation; obtain a full overview only when needed to resynchronize.

## Verification

`tests/fixtures/france_defense.json` contains selected permitted observations from
France's failed turns 25, 35 and 60. Python regressions verify loss visibility, the
exposed Worker, Orleans's unused bombardment, copyable command arguments, strategic
review, legal unexplained idling and loss retention in short summaries.

`TacticalReviewIntegrationTest` reconstructs the Worker/Brute/city situation in the
engine, rejects the malformed city command without mutation, fires the offered
shot, moves the Worker into the city and advances through the barbarian turn. It
checks native ownership/position and the persisted receipt after checkpoint
reload. It also tests hostile classification, fog filtering and a wrapping-map seam.
The captured-civilian case checks a guarded Settler, an unguarded Worker, other
players' civilians and native uncapturable units. It recaptures the Settler through
a legal melee attack, verifies that it remains a Settler, and checks removal of
recapture information after recovery, destruction and loss of visibility. Python
regressions check turn-start preloading, map details and command-update removal.
These are deterministic interface/engine regressions, not a model performance
benchmark or proof that the agents will consistently choose good tactics.

`TacticalConsequencesIntegrationTest` checks lethal HP projections, a 1-HP Warrior
against a 12-HP Brute, uncertain survivors, confirmed results after loss of visibility,
exact receipt replay, advisory legal
attacks, exhausted movement before attack/founding, friendly stacking, future-route
uncertainty, read-only previews and unchanged answers when a hidden enemy is added.
The [saved positions](../scenarios/Strategic-positions.md) separately evaluate model
choices and their outcomes over several turns.
