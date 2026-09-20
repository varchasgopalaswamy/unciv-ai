# Air warfare live acceptance

The September 20, 2026 Rome/Greece/France exercise passed all **30 artifact
checks**. The prepared Vanilla game completed three rounds and nine handoffs,
including a planned stop and restart after round one. It is paused at its turn
budget. All four service ports are closed and all 15 job permits were released.

All **15 model jobs** used **gpt-5.6-luna, medium reasoning**: nine completed
gameplay turns, five completed communication jobs and one interrupted
communication job. No gameplay activation failed.

## Observed results

| Player | Native result |
| --- | --- |
| Rome | Rebased Carrier wing onto the carrier at `(-3,-2)`, then moved both to `(0,-2)` with a previewed destination order |
| Rome | Paid 70 Gold to upgrade Reserve wing to Jet Fighter; disbanded Retired bomber for a 40 Gold refund |
| Rome | Bombed the Greek infantry at `(3,0)` for 28 damage; interception and combat left the bomber at 1 HP |
| Rome | Swept `(3,0)`; its fighter and the Greek interceptor each lost 31 HP |
| Rome | Struck the infantry at `(3,1)` with a guided missile for 44 damage; the missile was consumed |
| Rome | After restart, attacked from the carrier for 23 damage; the fighter survived at 35 HP and subsequently rebased to Antium at `(-1,0)`, with transport cleared |
| France | Paradropped Drop team from Paris to `(-3,6)` and retained its nuclear missile through restart |
| France | Read a fresh preview and explicitly confirmed one nuclear launch at Sparta; the missile was consumed and the native action declared war on Greece |
| Greece | Retained both automatic interceptors at Athens; received the nuclear report, lost the marked infantry at Sparta, and observed fallout and a surviving city |

The snapshot immediately after the nuclear launch shows Sparta at population 3.
France's launch receipt reports `Completed`, consumption and the new war; Greece
receives the victim's nuclear notification. The firing player is not given that
private victim report.

The wrapper supplied objectives and managed processes; all gameplay commands came
from the real model services. Each major started with 20,000 Gold and connected
strategic resources. Native preflight checks verified rebasing, the paid upgrade
and Uranium availability. Players also discussed a future peace treaty through
structured chat. No treaty was enacted during the exercise.

## Restart and recovery

`before-restart.json` records the carrier and its transported fighter together,
the upgraded Jet Fighter, the completed paradrop and France's unused nuclear
missile. The resumed run retains the same match epoch and every prior receipt.
Fresh previews authorize later missions; an old preview is not restored as
permission to act.

A Rome communication job is recorded as interrupted across the planned stop and
restart. Its record remains in the model-job count. All gameplay turns completed,
and all job permits were released. During later turns, stale plan assumptions and
a stale detail read were rejected without applying the attempted action. The
agents refreshed the affected evidence and continued without duplicated actions.

## Evidence and verifier

Local artifacts are in `/tmp/unciv-air-live-20260920-1`:

- `initial-game.json`, `before-restart.json`, `after-nuclear.json` and final
  `exploration.json` contain the native states.
- `air-verification.json` records all 30 passing checks;
  `verifier-challenges.json` records three successful negative checks.
- `agents/*/turn_*` contains observations, decisions and actual command receipts.
- `agent-services/*/activations` and `chat-jobs` contain effective model settings,
  conversation IDs and completion records.
- `runner/run.json`, `run-before-restart.json` and history retain handoffs, limits
  and the planned restart. `air-live-prompts.json` contains the guided roles.

The verifier compares archived receipts with the host's durable records, then
checks native units, transport, payments, war state, casualties and fallout. It
also checks model settings, completed turns, receipt preservation and the final
turn-budget stop. Isolated altered copies were rejected when the carried fighter
was detached, the consumed nuclear missile was restored, or the upgrade payment
was removed from both matching receipt copies. The original evidence stayed
unchanged and still passes.

## Desktop and automated comparison

`/tmp/unciv-air-desktop-20260920-2` contains 16 rendered screenshots and a
`COMPLETE` marker; all screenshots were inspected. Actual desktop clicks exercise
rebasing, carrier travel, paid upgrade, bombing/interception, sweeps, guided
missiles, paradrops and nuclear launch. The screenshots show the native preview
controls and resulting movement, damage, consumption and war declaration.

All 493 selected native regressions and all 103 host tests pass. The Python suite
passes 447 tests and 90 subtests, with formatting, lint and type checks passing.
Automated tests separately cover full city/carrier capacity, carrier loss,
affordability, promotions, fatal interception, hidden-state pairs and interrupted
checkpoint writes before and after replacement for all four special missions.

Reproduction commands and API details are in [Air gameplay](Air-gameplay.md).
The exercises used the local engine tree now published as
`f2408cde26543e404ea2844b18c1e648fa97691d` on `experimental-changes`.
The host pins that revision, and the default build passes against it.

## Scope

This is a prepared Vanilla military exercise, not an unrestricted full game.
The small prepared cities produced starvation notices; long-term economic
planning was not an acceptance objective. Native carrier destruction and fatal
nuclear interception have automated tests, but were not live-model milestones.
Expansion mechanics, every unique combination, mod content and an embedded human
desktop controller remain outside this acceptance run.
