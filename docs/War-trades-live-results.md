# Three-Cornered War live validation — September 18, 2026

The implementation and scripted engine checks pass. Live agents have enacted
paid war, recruitment into an existing war, and a joint declaration. The full
live exercise remains unverified: both runs exhausted their one-hour wall-time
budgets during the second continuation round. Their settlements, consent and
privacy checks passed, and Run A's restart preserved its recorded state.
Both checkpoints are paused and all host, agent and scheduler services are stopped.

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
| Continued play | One complete round after restart, then part of Rome's next turn. France made two archer attacks; Rome subsequently made one attack. |
| Required two further rounds | **Not satisfied.** The one-hour wall-time budget ended at game turn 2 with Rome active, handoff 6. |

The configured model was `gpt-6-astra` with `max` reasoning. Two 300-second Roman
activations timed out before handoff; confirmed actions were preserved on resume.
The activation timeout was then increased to 600 seconds, while retaining the
original one-hour wall-time and 96-invocation ceilings. Pauses and downtime counted.
The verifier reports restart preservation independently of continuation length,
so the incomplete second round is not mislabeled as lost restart data.

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

The opening and first continuation rounds completed. Rome and Greece also
completed their next turns; the wall-time limit stopped France during its final
turn, at game turn 2 and handoff 8. Therefore **the required two complete
continuation rounds were not satisfied**. All other automated checks passed.
There were nine gameplay activations and no individual activation timeouts before
the overall limit. All three retained three sourced active plans; France still had
one plan awaiting review when interrupted. No attacks were made in this variant.

The run also exercised recoverable detail, stale-observation and argument errors.
For example, France refreshed a construction offer and corrected the confirmation
field before its purchase succeeded; the rejected attempts made no purchase.
The prepared research queue advanced through Pottery normally.

The run used fresh game state and private memory, omitted Run A's prepared social
promise, and used the same configured model. Its limits were three complete rounds
(within the planned maximum of four), 48 invocations, one hour including downtime,
and 600 seconds per activation. The wall-time ceiling was not extended.

## Scope

The [scenario playbook](War-trades-live-scenario.md) describes the launcher and
verifier. Scripted failure/retry and hidden-fourth-civilization checks remain
separate from these live results. Desktop visual acceptance was not performed.
