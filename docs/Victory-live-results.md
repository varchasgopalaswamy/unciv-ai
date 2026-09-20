# Live victory acceptance

These are guided, prepared late-game **Civ V - Vanilla** matches with Rome,
Greece and France. Every player turn uses a real `gpt-5.6-luna` model with
`medium` reasoning. The ordinary scheduler owns activation, receipt recovery,
turn archives and terminal result delivery. The launcher supplies objectives;
it does not issue gameplay commands or change the game after initialization.

## Election: passed

Artifacts: `/tmp/unciv-victory-live-20260919-1/election/`.
The read-only verifier passed **29 checks** against the native checkpoint,
agent receipts, model activation records and final player reports.

- At turn 0, Rome voted for Greece, Greece explicitly abstained, and France
  voted for Rome. Rome's native United Nations weight gave Greece two votes;
  the winning threshold was three. No player won.
- At turn 1, all three models read and acknowledged the failed election results.
- All intervening turns used ordinary model-controlled handoffs. The next
  ballots became available on turn 11, without changing the native countdown.
- Rome and France then voted for Greece; Greece abstained again. Greece won
  Diplomatic Victory at turn 12 with three weighted votes.
- All three players received the correct victory/defeat report and the winning
  election results, including players who never got another gameplay turn.

There were **36 model activations**, all with the requested settings. The final
French activation was interrupted by normal terminal scheduling after its
winning handoff; its committed turn archive and all result deliveries passed.
The host and all three agent services stopped after completion.

## Spaceship: passed

Artifacts: `/tmp/unciv-victory-live-20260919-2/spaceship/`.
The fixture supplies fully researched technologies, Apollo, two developed Roman
cities, improvements, railroads, strategic resources, and treasury. It supplies
**zero spaceship units, stored part production, or assembled parts**. Rome chooses
its production allocation and transport routes; Greece and France maintain peace
and take their ordinary turns. The objective is all six parts and native
Scientific Victory, with final result delivery to every player.

Rome won **Scientific Victory on turn 9**, after **28 model activations**. The
read-only verifier passed **22 checks**, including zero initial part production,
six distinct assembly receipts, production orders from both cities, transport,
native completed-part counters, and all three final player reports.

| Turn | Confirmed assembly | Total completed parts |
| --- | --- | --- |
| 3 | Engine and first Booster | 2/6 |
| 6 | Stasis Chamber | 3/6 |
| 7 | Cockpit | 4/6 |
| 8 | Second Booster | 5/6 |
| 9 | Third Booster | 6/6; Scientific Victory |

Rome produced the Engine, Stasis Chamber and second Booster. Antium produced
the first Booster, Cockpit and third Booster; the model transported each of those
three units to Rome through advertised movement options before assembly. All
six production orders and all six assembly receipts match the host checkpoint.

Final assembly ended the game immediately, without an extra END_TURN. Normal
terminal scheduling interrupted the winning activation, archived its committed
actions, and delivered Rome's victory and Greece/France's defeat reports. The
launcher then stopped the host and all three agent services. No in-game operator
intervention or rollback was used.

The first spaceship fixture in the directory ending `-1` failed to reload before
any model activation. A terrain-feature list had a Java singleton-list type that
Unciv's save reader cannot instantiate. The fixture now uses a serializable list,
and generation reloads every checkpoint before declaring it ready. Launcher
readiness also matches the actual ready line rather than startup instructions
that merely mention it. The failed fixture and logs are retained.

Together the two successful matches used **64 model activations** and passed
**51 artifact checks**. All resolved model settings were `gpt-5.6-luna` with
`medium` reasoning. The new fixture generator compiled and passed its checkpoint
reload checks; Kotlin formatting, Python lint/format/type checks and repository
diff checks also passed. The engine change is merged and published on
`experimental-changes`; the [sprint record](Victory-and-turn-blockers.md)
identifies the revision pinned with this implementation and acceptance tooling.

## Repeat

Generate into a **new absolute directory**; existing fixtures are never replaced:

```sh
./gradlew generateVictoryLiveFixtures installDist \
  -PvictoryLiveDir=/tmp/victory-live-new --console=plain

.venv/bin/python tests/live_victory.py \
  --scenario spaceship --state-dir /tmp/victory-live-new/spaceship \
  --port 8793 --max-turns 30 --max-invocations 110 \
  --max-seconds 10800 --turn-timeout 600

.venv/bin/python tests/live_victory.py \
  --scenario election --state-dir /tmp/victory-live-new/election \
  --port 8797 --max-turns 18 --max-invocations 70 \
  --max-seconds 10800 --turn-timeout 600

.venv/bin/python tests/verify_victory_live.py \
  --state-dir /tmp/victory-live-new/spaceship
.venv/bin/python tests/verify_victory_live.py \
  --state-dir /tmp/victory-live-new/election
```

Each launcher uses its chosen host port and the next three ports for agent
services. It fixes all model settings to the values above. `--resume` preserves
the checkpoint, confirmed actions, archives and scheduler budgets. Each match
retains `initial-game.json`, `victory-live-prompts.json`, `exploration.json`,
service logs, per-turn receipts, model histories and `agents/*/result.json`.
The verifier writes `victory-live-verification.json` and exits unsuccessfully if
any check fails. These directories also contain private credentials; do not
publish them wholesale.

The live models encountered ordinary validation rejections for stale memory
assumptions, omitted detail reads and malformed command arguments. They re-observed or
read the requested records and continued without operator gameplay intervention.
These rejections are retained in the command histories; a completed model turn
does not mean every attempted tool call was accepted.

## Limits

This acceptance covers guided late-game decisions and their native consequences.
It does not establish an Ancient-to-victory autonomous match, independent strategic
competence, complete player-action/privacy coverage, or expansion features. The
live games do not inject crashes or test unmet-player election redaction; automated
checks cover those cases. Prerequisite rejection, Gold-purchase rejection, resource
release and invalid assembly locations remain separately covered by the
[scripted production test](Victory-and-turn-blockers.md#spaceship-production-follow-up).
