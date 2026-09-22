# Tactical and strategic review

`player_turn` keeps the complete permitted snapshot and adds two compact reviews
ahead of its detailed fields. Agents should read these before routine construction
and worker decisions. Restart the host and agent services to load this change.

`tacticalReview` shows:

- The owned military roster, health, base melee/ranged strength, current orders and
  offered attack counts, including units already fortified or asleep.
- Visible hostile military units and civilians or injured military units within
  two hexes of them. The Kotlin host uses engine visibility, unit classification,
  diplomatic status and map distances, including wrapping maps. Enemy civilians
  are not classified as attacking threats.
- Every currently offered city bombardment, damage ranges and copyable arguments.
- Confirmed losses from the delivered combat reports, city count, Settlers and
  queued Settlers, treasury, income and happiness.

Proximity is a warning, not an enemy movement prediction. It ignores hidden units
and does not establish that a destination is safe. A military unit on the same
tile is reported without promising that it can protect a civilian. Damage ranges
are engine previews; receipts and later observations establish actual results.

`strategyReview` lists active plans, their review reasons and missing `defense`,
`expansion` or `economy` coverage. Plans have measurable success criteria and a
future `reviewAfterTurn`, defaulting to five turns after creation or revision.
Confirmed losses and city losses trigger relevant reviews earlier. See
[structured memory](../architecture/Structured-memory.md). The service does not assess freeform
success criteria or automatically decide that a second city or a war is desirable.

Before `END_TURN`, available bombardments, civilians near hostiles and injured
military units near hostiles require action or a short `deferReason`. The service
returns `TACTICAL_REVIEW_REQUIRED` without dispatching anything if this review is
missing. The existing idle-unit review still applies. The deferral is passed to
the host and retained with the turn review.

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
IDs and revision tracking remain code-owned. Reobserve after each mutation.

## Verification

`tests/fixtures/france_defense.json` contains selected permitted observations from
France's failed turns 25, 35 and 60. Python regressions verify loss visibility, the
exposed Worker, Orleans's unused bombardment, copyable command arguments, strategic
review, explicit tactical deferral and loss retention in short summaries.

`TacticalReviewIntegrationTest` reconstructs the Worker/Brute/city situation in the
engine, rejects the malformed city command without mutation, fires the offered
shot, moves the Worker into the city and advances through the barbarian turn. It
checks native ownership/position and the persisted receipt after checkpoint
reload. It also tests hostile classification, fog filtering and a wrapping-map seam.
These are deterministic interface/engine regressions, not a model performance
benchmark or proof that the agents will consistently choose good tactics.
