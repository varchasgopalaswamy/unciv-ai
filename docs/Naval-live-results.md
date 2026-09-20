# Naval live acceptance

The September 20, 2026 exercise passed. Rome, Greece and France completed four
rounds in a prepared Vanilla coastal position. All **27 artifact checks** passed.
The run reached native turn 4, handoff 12, then stopped at its configured turn
budget. All services are stopped and no job permits remain active.

All **22 model jobs** used **gpt-5.6-luna, medium reasoning**: twelve completed
gameplay turns, one interrupted gameplay attempt and nine communication jobs.

## Observed results

| Player | Native result |
| --- | --- |
| Rome | Consumed its Work Boat to improve Whales, increasing available Whales from 0 to 1; paid 90 Gold to upgrade a Trireme to Caravel |
| Rome | Sent Voyager from `(-7,-2)` toward `(1,-2)` with one previewed destination order; it moved to `(-1,-2)` immediately and arrived automatically at the next Roman turn |
| Rome | Issued a replacement order toward `(8,-3)`, reached `(4,-3)`, then cancelled with no additional movement; the order remained cleared on later turns |
| Rome | Destroyed the Greek Trireme through a native naval attack, then bombarded Athens for 27 damage without capturing it; later withdrew its damaged Frigate using a previewed destination |
| Greece | Improved Fish, defended Athens with city bombardment, and bought a replacement Frigate for 560 Gold |
| France | Issued one overseas Settler order on turn 0; it persisted through restart, landed at `(-4,7)` on turn 1, and founded Orleans on turn 2 |

Players chose tactical responses and discussed diplomacy through the existing
chat interface. France remained neutral. Every major started with 5,000 Gold;
native preflight checks verified the Work Boat improvement, funded upgrade and
multi-turn route. The wrapper issued no gameplay commands. All movement commands
in the live run use destination previews and `SET_UNIT_DESTINATION`.

## Persistence and interruptions

The planned stop after round one saved `before-restart.json`. Voyager had already
arrived through native turn automation; the French settler's destination order was
still active. Restart retained both native state and completed receipts.

A later Roman job lost its permit after an eleven-minute gap in service
heartbeats. Its committed replacement movement survived. A fresh conversation
read the actual state, cancelled that order and completed the bombardment without
repeating the movement. The failed attempt remains in the history.

The initial 16-job allowance was exhausted after round two because diplomatic
conversations also consume jobs. The same run resumed with a finite allowance of
30; prior usage, actions, conversations and the match epoch were retained.

## Artifacts

Local evidence is in `/tmp/unciv-naval-live-20260920-1`:

- `initial-game.json`, `before-restart.json`, final `exploration.json` and
  `naval-verification.json`.
- `agents/{Rome,Greece,France}/turn_*`: observed state, decisions and receipts.
- `agent-services/*/activations` and `chat-jobs`: effective model settings,
  conversation IDs, completion and handoff records.
- `runner/history`, `runner/run.json` and `run-before-restart.json`: scheduler
  history, interruptions and limits.
- `naval-live-prompts.json`: guided roles and instructions for resumed turns.

The verifier checks the native save and compares archived command receipts with
the host's durable records. It also checks automatic arrival, the saved settler
order, cancellation without movement, no later automatic continuation of the
cancelled order, overseas founding, model settings and completed handoffs. Checks
against isolated altered copies also rejected a restored cancelled order and a
modified upgrade payment/receipt; the original artifacts were unchanged.

## Desktop comparison

`/tmp/unciv-naval-desktop-20260920-4` contains 11 rendered screenshots and a
`COMPLETE` marker. All 11 were inspected. Actual native controls exercised the
Work Boat improvement, paid ship upgrade, two-turn path preview, persistent
destination, Stop movement, naval attack and coastal bombardment.

Screens show the consumed Work Boat and improved Whales, the 90 Gold upgrade and
resulting Caravel, the full path and estimated turn badge, the active destination
with zero remaining movement, and cancellation. Combat previews show the Trireme
being destroyed and Athens at 200 HP with an expected final range of 161–174 HP;
the isolated desktop attack leaves Athens at 167 HP under Greek ownership.

Earlier desktop attempts exposed an audio-startup issue and an incorrect test
shortcut for confirming movement. The probe now uses a silent audio driver and
clicks the actual move badge. The successful final run also centers result tiles
and upgraded units for readable screenshots.

## Scope

Reproduction commands are in [Naval gameplay](Naval-gameplay.md#reproduce-acceptance).
Pillage, disband refunds, resource-dependent upgrades, healing, promotions, ocean
restrictions and paired hidden-state checks have separate native/HTTPS tests.
Those effects were not all exercised by the live models or desktop clicks.
Checkpoint tests inject failures before and after replacement, including native
order continuation during handoff. The live exercise covers restarts and a lost
job permit, rather than injected disk-write failures.

This prepared position does not establish an unrestricted full game, every
civilization-specific unique, aircraft/carrier commands, expansion mechanics or
an embedded human desktop controller.
