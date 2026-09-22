# Tactical and strategic review

`player_turn` includes compact factual reviews derived from the permitted snapshot.
They inform decisions without imposing strategic priorities.

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

`strategyReview` lists only active plans with review flags, by ID and reason.
Players choose their own plan categories and may supply a future `reviewAfterTurn`;
the service does not invent review deadlines or require coverage of named topics.
Confirmed losses and city losses trigger relevant reviews earlier. See
[structured memory](../architecture/Structured-memory.md). The service does not assess freeform
success criteria or automatically decide that a second city or a war is desirable.

Before `END_TURN`, native pending decisions must be resolved and the current
`turnReview.id` must have been observed. Idle units, available bombardments and
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
These are deterministic interface/engine regressions, not a model performance
benchmark or proof that the agents will consistently choose good tactics.
