# Initiative decision evaluation — September 30, 2026

The subsequent [implementation and paired replay](Initiative-diplomacy-fixes-2026-09-30.md)
address the API problems documented here.

The paired live trial provides some evidence of earlier follow-through and clearer
accounting. It does not establish better strategic play. Both conditions eventually
negotiated border agreements; initiative decisions increased communication work and
exposed API problems that should be fixed before a larger comparison.

## Setup

- Identical saved position: Rome active on turn 139, after the earlier live diplomacy
  evaluation. Each condition played rounds 139–140: six player turns, twelve total.
- Rome and France: `gpt-5.6-luna`, max reasoning. Greece: `gpt-5.6-terra`, max reasoning.
  Egypt used built-in AI. These match the source evaluation's settings.
- Identical real, model-written reports: Rome turn 137; Greece and France turn 136.
  Existing agreements and recent communication were retained. Fresh private stores
  received those reports and exchanges; old message wakes were marked handled.
- Both conditions retained the current proactive diplomacy guidance. The control
  omitted only the initiative-decision instruction and its input/history fields.
  Two copied Python source files differed; the native host was identical.
- No new reviews were generated during this window. This tests initiative decisions,
  not the shared strategic-review phase. No guide or fast priority was enabled.
- Both games stopped normally at turn 141. The original match was not modified.

Starting world/report hashes matched. Actual provider records confirmed the selected
models and reasoning effort for every job. The source checkpoint remained unchanged.

## Observed actions

| Outcome | Control | Initiative decisions |
| --- | --- | --- |
| Rome contacts France and Greece | Turn 140 | Turn 139 |
| Rome requests French neutrality and information | Yes | Yes |
| Rome negotiates a quiet Greek frontier | Yes | Yes |
| Rome obtains Egyptian Incense | No completed offer | Accepted Egypt's counteroffer on turn 140: Wine plus 158 gold for 25 turns of Incense |
| France–Greece social border agreement | Both consented by turn 140 | Both consented by turn 140 |
| Other native agreements | France–Greece Declaration of Friendship | France–Rome luxury trade: two Silver for Wine and Spices, for 25 turns |
| New chat messages | 13 | 10 |

The control also followed through, one turn later. The trial therefore supports an
attention/timing benefit more than a claim that the feature unlocks diplomacy.
The different native agreements are observed outcomes, not a ranking of which
position is stronger. No coordinated war or deception was demonstrated.

Both Romes initially put descriptive prose in `trade_offer.proposal`, which expects
an existing proposal reference. Both received `UNKNOWN_TRADE`. The enabled Rome
corrected its request and completed the negotiation; the control left it unresolved.
This is one paired observation, not proof that the decision field caused recovery.

### Decision records

All six enabled gameplay turns supplied decisions in the existing `END_TURN` call:
eleven entries, comprising five `acted` and six `deferred`. Every end-turn entry
referenced the correct report turn, survived into private action history, and was
excluded from the native game command. No additional bookkeeping invocation was needed.

Examples of useful distinctions:

- Rome recorded its Egyptian offer as pending on turn 139 and its acceptance of
  Egypt's counteroffer on turn 140.
- Greece deferred research-agreement renewal until turn 144, with reserve and
  relationship conditions, rather than treating an eventual renewal as due now.
- Greece identified the Cotton-for-Spices trade as already completed on turn 137.
  Its `acted` entry is historical follow-through, not a new action in this trial.
- France deferred Egyptian renewal and later recorded the accepted Greek border
  understanding and its alternative luxury supply from Rome.

The field also appeared on six communication action records, with seven incorrect
review-turn references. Those jobs used the current game turn instead of the actual
report turn. Some were rejected requests. The shared `Intent` schema exposes the
field beyond its intended end-turn use; these extra records should not be counted
as valid initiative completion evidence.

## Reported usage

| Measurement | Control | Initiative decisions |
| --- | ---: | ---: |
| Completed player turns | 6 | 6 |
| Model requests | 127 | 144 |
| Model-facing tool calls | 116 | 131 |
| Input tokens | 4,247,079 | 5,004,970 |
| Cached input tokens | 3,628,032 | 4,251,392 |
| Uncached input tokens | 619,047 | 753,578 |
| Output tokens | 73,533 | 81,810 |
| Reasoning output tokens | 58,134 | 62,875 |

Cached tokens are included in input; reasoning tokens are included in output.
Requests rose 13%, tool calls 13%, uncached input 22%, and output 11%.

Gameplay itself changed little: 57 to 59 requests, 69 to 72 tool calls, and
342,635 to 347,232 uncached input tokens. Communication accounted for 129,934 of
the 134,531 additional uncached input tokens. Its jobs increased from eleven to
fourteen. These differences include divergent choices, negotiations and retries;
they are not a measurement of the decision field's intrinsic token cost.

All reported token counts reconciled against provider usage records for all 37
jobs. All twelve gameplay turns had complete usage. Each planned stop interrupted
one final communication job: its reported usage reconciles, but any unreported
cancellation usage cannot be established. The runs overlapped, so wall time is not
a controlled latency comparison.

## Problems exposed

1. **Decision metadata has too broad a scope.** Communication tools accept the same
   optional field and models supplied incorrect report references. Limit it to
   end-turn decisions and derive the report reference from the advice actually
   delivered to that job.
2. **Proposal references resemble prose fields.** Both variants confused `proposal`
   with a description. Social offers then returned generic `INVALID_ARGUMENT`,
   prompting changes to unrelated fields. France in the enabled run made eight
   rejected commitment offers and briefly published then withdrew a minimal offer
   while trying to discover valid arguments. Describe or rename the reference
   field, distinguish creation from revision, and return specific argument errors.
3. **Acceptance schemas disagree.** Python `CommitmentDecision` permits `reason`
   with `decision="accept"`; the native `communication.json` accept variant forbids
   it. The gateway forwards it. Both conditions hit this rejection. Align the
   exposed schema and native translation.
4. **Some communication was redundant or misrouted.** Rome's gameplay and
   communication jobs sent overlapping acknowledgements. Enabled Rome and France
   also attempted plain-text chat with built-in Egypt; both calls were rejected.
   Improve cross-job awareness and recipient guidance/error details before adding
   more diplomacy instructions.

Keep the optional end-turn mechanism, narrow its scope, and fix the social API
problems before expanding the experiment. This single position and two rounds
cannot establish a win-rate improvement, long-term execution, or a general cost
increase. Several proposed renewals fall outside the tested window.

## Artifacts

Local evidence is under `/tmp/unciv-initiative-eval-20260930/`:

- `protocol.json`: hashes, model settings and exact comparison conditions.
- `prepare.py` and `run_comparison.py`: isolated source copies, saved-position
  preparation and ordinary service/scheduler launch.
- `control/` and `decisions/`: separate checkpoints, private memory databases,
  monitoring databases and service logs.
- `results.json`: actual calls, receipts, messages, agreements and per-job usage.
- `summary.json`: totals and verification results.
- `collect.py` and `verify_results.py`: provider reconciliation and checks of model
  settings, matching inputs, completed turns and persisted decision metadata.

This report preserves the conclusions if the temporary artifacts are removed.
