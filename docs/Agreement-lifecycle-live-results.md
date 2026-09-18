# Agreement lifecycle live validation — September 18, 2026

**Passed.** The live exercise reached native turn three, Rome next, with nine
scheduled handoffs. All 12 verifier checks passed, including both agreement-ending
paths, sourced plan review and two complete rounds after the planned restart.
The final game is paused and all host, agent and scheduler processes have stopped.

The [scenario playbook](Agreement-lifecycle-live-scenario.md) describes the setup,
limits and commands. The run uses `gpt-6-astra` with `max` reasoning from the
existing player configuration, with three intended rounds, a 64-invocation ceiling,
90 minutes including downtime, and 600 seconds per activation.
It completed in **31 minutes**, using 19 recorded model invocations, including nine
gameplay activations. No extension or replacement run was needed.

The [final verification report](/tmp/unciv-agreement-live-20260918-1/verification/agreement-scenario.json)
was regenerated after service shutdown and its checkpoint hash matches the final
save. All eleven stored plans have sources and no outstanding review flags.

## Opening and restart evidence

Evidence is retained in
[`/tmp/unciv-agreement-live-20260918-1`](/tmp/unciv-agreement-live-20260918-1),
including the [restart checkpoint](/tmp/unciv-agreement-live-20260918-1/before-restart.json),
[private memory backups](/tmp/unciv-agreement-live-20260918-1/before-restart-memory)
and [process lifecycle log](/tmp/unciv-agreement-live-20260918-1/operator-process-log.json).

- Rome proposed one Iron for Greece's two Gold per turn; Greece accepted version
  one of `d2cvo90f0`. Both clauses use the native Quick duration of 25 turns.
  Acceptance held gameplay at handoff zero and changed no treasury: this deal has
  no immediate payment.
- The enacted resource agreement is Rome's `a2` and Greece's `a1`. Both copies had
  24 turns remaining at the first completed-round archive.
- Rome's confirmed opening `END_TURN` decision `dec6` references `mem3` version
  four; Greece's `dec7` references `mem2` version two. Each plan cites an inspected
  active resource agreement, proving it was saved before handoff.
- Rome's prepared France agreement expired at its opening end turn. Its event
  flagged the existing plan, followed by a fresh `agreement_read` and a revision
  of `mem4` explaining lost access and the saved one GPT expense. The Greek trade
  remained active. France's copy expired at the end of its own opening turn.
- France correctly observed that its retained one-turn row and current border
  access were separate observations. This reflects the pinned engine's
  owner-local countdowns, not a new host rule.
- All four services stopped at completed round one, then restarted with new
  process IDs. The original game, epoch, agreement references, receipts, event
  history and all three private memory databases were preserved.
- The opening France archive contains only its own agreement event, with no
  private Rome–Greece proposal identifier or settlement in its records or tool
  responses. No player disclosed the resource deal through chat during the opening.

## Raid, interruption and plan review

The [post-pillage checkpoint](/tmp/unciv-agreement-live-20260918-1/after-pillage.json)
and [post-interruption checkpoint](/tmp/unciv-agreement-live-20260918-1/after-interruption.json)
preserve the two separate native transitions. Player histories are retained under
[`runner/history`](/tmp/unciv-agreement-live-20260918-1/runner/history), with each
service's private memory and tool-call records under `agent-services`.

| Check | Observed result |
| --- | --- |
| Legal French raid | On turn one, France inspected the visible mine and legal movement, confirmed war against Rome, moved its Warrior to `(-2,-2)`, and confirmed pillaging. The move and pillage each cost one movement point; pillaging granted four Gold. |
| Roman response | On turn two, Antium legally bombarded the same Warrior for 29 damage, leaving it at 71 HP. There was no capture. |
| Native interruption | The Iron trade remained present after the raid. Rome's turn-two ending triggered native resource checking, removing both copies and recording `RESOURCE_UNAVAILABLE`. The bilateral resource and GPT flows became zero; Rome and Greece remained at peace. |
| Roman plan review | Rome correctly kept the deal classified as active while the mine was pillaged and available Iron was negative. After the engine-confirmed ending, it refreshed `a2` and revised the same economy plan, `mem3` version 22: income fell from +12 to +10 GPT and available Iron rose from −1 to zero as the export stopped. It recognized that there was no supply for a replacement export. |
| Greek plan review | Greece refreshed ended `a1` and revised `mem2` to version nine: income rose from +8 to +10 GPT and available Iron fell from one to zero. It stopped relying on Iron-dependent units and recognized that ending the trade did not refund past payments. |
| Durable evidence | Both plans have the active agreement as an earlier source, a lifecycle review flag, a fresh ended-agreement read and an agent-authored revision. Their review flags cleared after successful updates. |
| Information boundaries | Greece's ending event contained its own terms and confirmed effects, without a raid location or culprit. Its defense plan explicitly left the cause of the Iron loss unestablished. France's records contained no private proposal, price, resource-ending event or Rome–Greece follow-up message. |

Rome and Greece voluntarily sent private follow-up messages to each other about
their own observed effects and continued peace. These messages supplied no private
information to France. The agents changed budgeting and resource assumptions;
the guided exercise deliberately preserves production queues, so it does not
claim a new construction choice driven by the ending.

Two recoverable argument errors occurred when France supplied a generic
confirmation field. Validation rejected them before dispatch, and France used the
required `confirmWar` and `confirmAction` fields successfully. A detail read also
required a fresh observation. Concurrent memory edits encountered stale-source
and review-evidence guards; fresh reads led to successful revisions. None required
an operator to modify the game or an agent's private memory.

## Implementation validation

- The scripted production scenario passed through all nine scheduled handoffs,
  a checkpoint reload and exact retries, native war/movement/pillaging, and both
  kinds of agreement ending. Actual checkpoint output also passed the operator
  verifier's native and restart checks; it correctly lacked live memory evidence.
- Eight targeted JVM tests passed: four launch scenarios, three CLI tests and
  the lifecycle scenario integration test. Gradle formatting checks passed.
- The full Python suite passed: **398 tests and 90 subtests**, including 35 verifier
  tests covering missing or altered evidence. After refining pillage verification
  to use the committed receipt's position, all **39 focused verifier tests** passed,
  including later unit movement/removal and incorrect receipt evidence. Ruff lint/format checks passed over
  123 files; Pyrefly reported zero errors and six existing missing-stub warnings.
- No Python distribution build was used. The engine and assets remain at
  `0bad73285ffa306334b89bcecbe395dc3b3b0bda`, with no engine changes.

The [automated validation record](../build/verification/agreement-scenario-checks-20260918.json)
retains the commands and results separately from live acceptance. This run covers
the specified Vanilla agreements and prepared raid. It does not establish every
agreement type, unrestricted strategic play, a completed match or full API parity.
