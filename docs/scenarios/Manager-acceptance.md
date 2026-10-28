# Controlled economy and military acceptance tests

These are two six-round live exercises with actual model players, prepared
native openings, and a stop/restart after round two. They exercise manager
coordination and observable outcomes without waiting for an ordinary game to
reach developed cities or war. They use fresh isolated games, not existing games.

## Run both exercises

From the repository, with the normal Podman/runtime/login setup already ready:

```sh
.venv/bin/civ-agent image build
.venv/bin/civ-agent game test-managers \
  --state-dir ~/.local/state/unciv-host/manager-acceptance-1
```

The command runs economy, then war. Each starts with a two-round budget, waits
for that exact boundary, stops its game services, captures an audit baseline,
resumes the same saved game to round six, stops again, and verifies the result.
There are two agent players (Rome and Greece) and one built-in AI (France).
The default model is `gpt-6-luna` at medium effort; main/economy/military inherit
it. Each game has a two-hour elapsed budget, a 600-second activation timeout,
120 model invocations and four concurrent jobs. These are limits, not cost estimates.
No human controller or mod content is involved.

Use `--model MODEL` to override the agent models, `--image IMAGE` to select a
fresh runtime explicitly, or `--case economy` / `--case war` for one exercise.
The runner refuses existing case directories. Inspect its exact commands without
starting a game or spending model invocations:

```sh
.venv/bin/civ-agent game test-managers \
  --state-dir /tmp/manager-preview --dry-run
```

The runner uses the [economy profile](../../examples/economy-manager-test.toml)
and [military profile](../../examples/military-manager-test.toml). A stale image
fails native scenario validation before model play. Existing games retain their
pinned images. The runner does not restart shared login/Decisions services.

## Economy exercise

Rome and Greece each start with two developed population-four cities, farms,
Colosseums, Libraries, Universities, available specialists, and inherited wonder
queues/growth suppression. Both economy managers must provide persistent city
plans, keep the cities productive and fed, and explain their choices.

Rome starts with 500 Gold and a prepared outgoing native-AI gift offer committing
480 Gold to France. This is fixture history, not a deal the live agents negotiated.
Native AI offers do not hold gameplay, so economy can encounter the reservation:

1. Economy inspects the available/committed treasury and Worker purchase, makes
   one deliberate rejected purchase attempt, and tells main the actual result.
2. Main reads internal messages and waits for confirmed first-pass completion
   before inspecting and withdrawing the prepared offer. It sends an
   urgent economic request for one Worker; economy buys it and main handles any
   remaining unit actions. The failed purchase must never receive confirmation.
3. On round one, main explicitly overrides its frontier city, queues a defensive
   unit, and sends an urgent request involving that city. Economy must respect
   the override and record its request disposition. The override expires normally.
4. After restart, saved city reasons are preloaded; good choices should persist,
   with changes grounded in current constraints rather than a new conversation.

The audit checks the expected native rejection alongside observed commitments,
purchase after release, withdrawn proposal, urgent request, a reopened economy
pass, explicit override, city ownership, confirmed actions, native queues and
population, later plan preload, completion barriers and preserved plan history.
The instructions require main to confirm economy's first-pass completion before
the urgent request, so this exercises reopening rather than relying on timing.
If the models skip that ordering and no second pass occurs, the audit fails it.

## War exercise

Rome and Greece begin in a prepared war that has already served the native
minimum duration. Each has a nearby Spearman and Archer; Rome additionally has a
named Worker (`Road Crew`) and Spearman (`Escort Guard`) together on a safe tile.
France is a peaceful native AI observer. No war flags or gameplay rules are
changed after startup.

1. Main establishes initial war goals; military may defend while that brief is
   pending. Main assigns escort duty and moves the Worker and escort separately.
   Military controls the other combat units and stores an operation assessment.
2. On round one military proposes a substantive goal revision prioritizing army
   preservation before peace. Main responds; agreement or main's documented
   tie-break produces an accepted revision.
3. From round two Rome offers mutual native peace to Greece. Each binding action
   requires a military tactical overview followed by main's exact-terms approval;
   Greece's acceptance exercises the off-turn path.
4. After peace, military retains combat-unit control and assesses peacetime
   readiness; main retains civilians and assigned escorts. Remaining rounds
   exercise peaceful continuation and durable notes following restart.

The audit checks military jobs, discussed goal revisions, operation notes,
main's control of both escort units, escort survival, stored military overviews
and main approvals, native peace, later peacetime military planning and the shared
turn barrier. It reports surviving units for inspection.

This opening tests activation into an existing war, not an offensive declaration
or a mid-turn second war. Those paths retain automated role tests. It also does
not exercise city-state war, nuclear authorization or capture disposition.

## Reports and manual use

The parent `manager-tests.json` summarizes both exercises. Each case keeps:

- `verification/manager-baseline.json`: the first-stage checkpoint hash and
  retained private plan/version fingerprints.
- `verification/manager-scenario.json`: individual checks, native city/unit
  outcomes and pointers to model-authored city plans.
- The normal host, supervisor, service diagnostics and conversation histories.

`PASSED` requires every check. Before round six, unmet checks are `PENDING`;
at round six they produce `FAILED`, and the runner returns a nonzero exit code.
Native/model failures and wrong round boundaries stop the case and remain visible.
The second case can still run and produces its own report.

To inspect a case interactively while it runs:

```sh
.venv/bin/civ-agent match watch \
  --state-dir ~/.local/state/unciv-host/manager-acceptance-1/economy-manager
```

To run a profile manually, use `game start --config PROFILE --state-dir NEW_DIR`.
After its two-round budget, stop the game, run `match verify-manager-scenario
--state-dir DIR --save-baseline`, then `game resume --state-dir DIR --max-turns 6`.
After stopping the completed second stage, run `match verify-manager-scenario
--state-dir DIR` again. Do not replace the first baseline or upgrade images
between stages.

The audit reads native saves, canonical receipts and private role records. It
cannot establish the truth of a model's tactical assessment or the quality of
its explanations. Review those with the preserved observations and transcripts;
completion of these exercises is not a win-rate comparison with built-in AI.

## Checks without paid model calls

```sh
uv run --no-sync pytest tests/test_manager_acceptance.py tests/test_economy_roles.py tests/test_military_roles.py
./gradlew test --tests uncivai.ManagerScenarioIntegrationTest --tests uncivai.HostCliTest
```

The native probes verify prepared openings, gold commitment/release, rejected
and successful purchases, visible armies/escorts, legal off-turn peace and
checkpoint reload. Python tests validate orchestration and reject incomplete or
inconsistent audit evidence. These automated probes do not run live models.
