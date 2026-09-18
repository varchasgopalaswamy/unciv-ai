# Three-Cornered War live validation — September 18, 2026

The implementation, scripted engine checks, both live exercises and the scoped
[desktop acceptance](War-trades-desktop-acceptance.md) pass. Both live verifiers
now report `verified: true` at game turn 3, handoff 9, including the required two
continuation rounds and Run A's restart preservation.

The initial one-hour runs stopped during the second continuation round. At the
user's request, both saved games resumed with additional time and completed;
their epochs, agreements, outcomes and prior receipts were preserved. Both final
checkpoints are paused and all host, agent and scheduler services are stopped.

## Validation and fixes

- Full JVM `check` passed. Final targeted scenario checks also passed after the
  role instructions and prepared research queue were updated.
- Python: 346 tests and 86 subtests passed; Ruff and Pyrefly passed. The verifier's
  later distinction between preserved restart data and completed continuation
  rounds passed its 16 focused tests and type checking.
- No Python distribution build was used. The engine remained at
  `0bad73285ffa306334b89bcecbe395dc3b3b0bda`, with its matching assets.
- Fixed a Kotlin option-validation compilation error.
- Moved scenario identity validation before restoring a scheduled checkpoint:
  selecting the wrong scenario now rejects without first persisting a paused save.
- The first live attempt exposed a null-trade rejection bug. A communication job
  opened the offer before a parallel gameplay job tried to do the same. The host
  correctly rejected the duplicate with `trade: null`; Python receipt handling
  crashed on that nullable field. Receipt processing, settlement scans and history
  rendering now handle it, with a regression covering rejection, replay and a later
  successful settlement. Scenario text also points negotiators to an existing offer.
- Corrected the prepared research queue to include Pottery before Writing.

## Run A: paid war, then joining

Evidence: [final automated report](/tmp/unciv-war-live-paid-20260918-2/verification/war-scenario.json),
[restart archive](/tmp/unciv-war-live-paid-20260918-2/before-restart.json),
[process lifecycle log](/tmp/unciv-war-live-paid-20260918-2/operator-process-log.json),
and [player histories](/tmp/unciv-war-live-paid-20260918-2/runner/history).
The [first failed attempt](/tmp/unciv-war-live-paid-20260918-1) remains separate.

| Check | Result |
| --- | --- |
| Initial offer and counter | Rome offered 30 Gold; Greece countered at 60; Rome accepted version 2. |
| Reconsideration | Greece explicitly reconsidered its France promise before the successful counteroffer. |
| Direct-war settlement | Rome 500 → 440 Gold; Greece 500 → 560; France unchanged. Only Greece–France entered war. |
| Recruitment | Greece offered 20 Gold for Rome to declare; Rome accepted. Native state and settlement snapshots confirm the second transfer and Roman entry. |
| Turn ownership | First settlement remained on Rome's turn; recruitment remained on Greece's turn. |
| Inspection | Histories show trade reads and option/warning reads before the successful counter and acceptances. The gateway bound acceptance to inspected versions. |
| Target information | France received its own declaration events and acknowledged both native declaration alerts. Its event stream contains no private trade proposals. |
| Voluntary disclosure | France asked about sponsorship; Rome explicitly disclosed its 60-Gold payment in private chat. This supplies a legitimate source for France's knowledge of the payment. |
| Restart | At completed round 1, all services stopped and restarted. Both proposals, outcomes and every prior communication receipt survived unchanged; no replacement deals or duplicate payments occurred. |
| Plans and alerts | All three players maintained sourced plans and processed native notices. Final stored active plans had no outstanding review flags. |
| Continued play | Two complete rounds after the planned restart. France made two archer attacks; Rome made one attack. The resumed final round preserved those results, repositioned armies and updated sourced plans. |
| Required two further rounds | **Passed.** Final native game turn 3, Rome next, handoff 9. The original round-1 restart archive still matches all recorded agreements, outcomes and prior communication receipts. |

The configured model was `gpt-6-astra` with `max` reasoning. Two 300-second Roman
activations timed out before handoff; confirmed actions were preserved on resume.
The activation timeout was then increased to 600 seconds, while retaining the
initial one-hour wall-time and 96-invocation ceilings. Pauses and downtime counted.
The verifier reports restart preservation independently of continuation length,
so the initial incomplete second round was not mislabeled as lost restart data.
The later completion window is recorded below.

## Run B: joint war

Evidence: [final automated report](/tmp/unciv-war-live-joint-20260918-1/verification/war-scenario.json),
[process lifecycle log](/tmp/unciv-war-live-joint-20260918-1/operator-process-log.json),
and [player histories](/tmp/unciv-war-live-joint-20260918-1/runner/history).

Rome proposed a single agreement with both parties declaring war on France and
50 immediate Gold from Rome to Greece. Greece accepted version 1. The engine
confirmed both declarations in that one settlement: Rome's treasury changed
500 → 450, Greece's 500 → 550, and France's remained unchanged. Rome read its
declaration warning through `trade_options` before publishing; Greece read its
warning and the final version through `trade_read` before accepting. Rome read
the enacted outcome afterward. There was no extra proposal, replacement deal or
duplicate payment.

France subsequently proposed separate bilateral social ceasefires through turn
10, and both opponents accepted. These promises prohibit attacks, captures,
pillaging and new territorial entry; they do not enact engine peace. Continued
play therefore exercised defensive positioning and commitment handling. The
agents chose these terms themselves.

All three players completed the opening round. France read both full native
declaration alerts, acknowledged them, and dismissed the joint-war notification
that names Rome and Greece. Across the final histories, its observed responses
contained no private trade proposal identifier, and no participant disclosed the
price to France. All three players saved sourced plans reflecting the changed
diplomacy.
During the next round, France spotted a Greek spearman and explicitly left an
available archer attack unused because of the ceasefire.

The initial wall-time limit stopped France during its final turn, at game turn 2
and handoff 8, after nine gameplay activations. On resume, France read the prior
receipts, finished its city adjustments and sourced plan reviews, dismissed its
remaining notices, and ended the turn. **Both required continuation rounds now
pass**, at game turn 3 and handoff 9. All nine active plans have sources and no
outstanding review flags. No attacks were made in this variant.

The run also exercised recoverable detail, stale-observation and argument errors.
For example, France refreshed a construction offer and corrected the confirmation
field before its purchase succeeded; the rejected attempts made no purchase.
The prepared research queue advanced through Pottery normally.

The run used fresh game state and private memory, omitted Run A's prepared social
promise, and used the same configured model. Its initial limits were three complete rounds
(within the planned maximum of four), 48 invocations, one hour including downtime,
and 600 seconds per activation. It required one additional gameplay activation
during the later completion window.

## Completion of the remaining acceptance tasks

The user requested completion of both remaining gates on September 18. The saved
games resumed through the normal scheduler, retaining their original run identity,
model settings, usage and cumulative elapsed-time accounting. The additional
windows began at scheduler resume; prior downtime was included when calculating
the cumulative `--max-seconds` values. Activation limits remained 600 seconds.

| Run | Initial stop | Additional window | Time used, including startup | Final result |
| --- | --- | --- | --- | --- |
| A: paid/joining | Turn 2, Rome, handoff 6 | 30 minutes | About 15 minutes; three gameplay activations | Turn 3, handoff 9; all ten verifier checks pass. |
| B: joint | Turn 2, France, handoff 8 | 15 minutes | About five minutes; one gameplay activation | Turn 3, handoff 9; all eight verifier checks pass. |

The [paid preservation comparison](/tmp/unciv-war-live-paid-20260918-2/verification/completion-preservation.json)
and [joint preservation comparison](/tmp/unciv-war-live-joint-20260918-1/verification/completion-preservation.json)
confirm unchanged game IDs, epochs, proposals and outcomes, plus exact retention
of all 21 and 30 respective pre-resume communication receipts. Run A added no
communication receipts during completion. Run B added only France's withdrawal
message to Greece; no new private contract disclosure occurred. Both official
verifier commands exited 0, and their recorded checkpoint hashes match the final
saves. The scheduler's final `budget_exhausted` status means the requested
**three-round limit was reached**, not another wall-time interruption.

Final transcript and memory review found all 19 active plans sourced and reviewed
(ten in Run A, nine in Run B), with no war alerts left unresolved. New research
and border-expansion notifications are ordinary next-turn items. During Run A's
completion, two overlong end-turn reasons were rejected before dispatch; shorter
retries succeeded exactly once. Source-review guards likewise prompted fresh
reads before the agents successfully updated their plans.

Desktop acceptance used separate Vanilla fixtures and actual rendered Unciv
screens. Sixteen screenshots were inspected, native publication and acceptance
buttons enacted all three war forms, and exact balances and relationships matched
the expected results. The [desktop report](War-trades-desktop-acceptance.md)
contains the evidence, reproduction command and scope limitations. The reusable
`runWarTradeDesktop` task and test launcher passed targeted compilation and
formatting checks; the desktop process exited cleanly.

## Scope

The [scenario playbook](War-trades-live-scenario.md) describes the launcher and
verifier. Scripted failure/retry and hidden-fourth-civilization checks remain
separate from these live results. The desktop check covers the three war-trade
forms and shared native warning in Vanilla; it does not establish full trade or
player-API parity.
