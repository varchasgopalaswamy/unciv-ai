# Initiative and diplomacy fixes — September 30, 2026

The follow-up paired replay completed all twelve player turns. Initiative decisions
now have the correct scope and report dates. The offer/acceptance retry loops from
the [earlier trial](Initiative-decisions-2026-09-30.md) did not recur. This establishes
working API contracts in this position, not stronger long-term play or a general
efficiency improvement.

## Implementation

1. `initiativeDecisions` belongs directly on `END_TURN`, not on the shared `Intent`.
   Entries are optional, unique by initiative number, and checked against the
   delivered report. The service supplies `reviewTurn` in private history and strips
   all this metadata from native commands. Communication cannot submit it.
2. Model tools distinguish `proposalId`, `trade_offer.counterofferTo`, and
   `commitment_offer.reviseProposalId`. New offers omit the reference. Copyable
   reads and decisions use these names. Invalid arguments return field-specific
   diagnostics. Message, commitment and diplomatic-outcome IDs now have six
   characters, with collision checks; trade references already had six.
3. Commitment acceptance has no public-reason argument. Withdrawal/reconsideration
   require `publicReason`, translated to the native `reason` field. `intent.reason`
   stays private. The MCP schema describes the action-specific alternatives.
4. Before sending chat, the existing dispatch lock and outgoing journal identify
   another job's unseen reply in that conversation. `CONVERSATION_UPDATED` returns
   those messages and confirms no send occurred. A deliberate follow-up remains
   possible. Reads stay available while other actions are in flight.
5. `chatRecipients` identifies ordinary chat recipients. Plain text to built-in AI
   returns a useful rejection before dispatch. Only gameplay exposes
   `chat_send.nativeAction`; native offers to built-in AI still use trade tools.

The final source also excludes already-ingested or already-delivered sent messages
from the supplement used while waiting for subscription echoes. This avoids
repeating old conversations in later preloads.

Memory format 9 requires fresh stores. There are no old argument aliases or
migrations. Native social promises remain advisory; these changes do not enforce
their meaning or prohibit native-legal game actions.

## Replay conditions

Both conditions started from the same turn-139 position, with Rome active, and
played rounds 139–140. They retained the same genuine reports: Rome turn 137,
Greece and France turn 136. Fresh private stores included the historical exchanges;
old communication wakes were consumed. The source match was unchanged.

Rome and France used `gpt-5.6-luna`; Greece used `gpt-5.6-terra`, all with maximum
reasoning. Egypt used built-in AI. No strategy guide, fast priority, or new reviews
were enabled. Provider records verified the model settings.

Both copies contained the corrected diplomacy API. The control omitted only the
initiative instruction and `END_TURN` input field. Frozen source hashes, checkpoint
hashes and report hashes are recorded with the results. The control differed in
`player_prompts.py` and `game_commands.py` only.

## Results

Both games stopped normally at turn 141 with Rome active. All 36 jobs completed,
and every reported token count reconciled with its provider trace.

The enabled run recorded **11 initiative decisions on all six end-turn calls**:
five `acted`, six `deferred`. Every stored report date matched the delivered advice.
No other action contained initiative decisions, and no native request contained
either those decisions or private intent. No extra bookkeeping invocation was used.

Both Romes contacted Greece and France on turn 139. Both runs negotiated a
France–Greece social understanding. The enabled run also established a mutually
accepted Rome–Greece frontier understanding and completed these native trades:

| Participants | Terms |
| --- | --- |
| Rome–France | Rome's Wine for France's Silver, 25 turns |
| Rome–Greece | Greece's Cotton for Rome's 5 gold per turn, 25 turns |
| Rome–Egypt | Egypt's Incense for Rome's Wine and 158 immediate gold; resources for 25 turns |

The control completed no new native trade in the window. France deferred Rome's
Wine-for-Silver suggestion until its existing commitments approached expiry. The
enabled run's additional agreements are concrete follow-through, not proof that
its position or winning chances are better.

### Error handling observed

- Each condition made one commitment-source mistake: an event number supplied as
  a message ID. The response identified `sources`; each model corrected its offer.
  Neither run published and withdrew a test commitment to discover the schema.
- The control attempted ordinary chat with Egypt once. The local rejection listed
  the actual chat recipients. It did not send a message or execute a native action.
- Enabled Rome had one recoverable stale diplomacy observation.
- Enabled Rome attempted to accept a Greek draft after Greece withdrew it in favor
  of Rome's already accepted proposal. The native rejection was correct; play
  continued. Greece's withdrawal used `publicReason` successfully.
- No `UNKNOWN_TRADE`, acceptance-schema rejection, incorrect review date, or
  off-turn initiative record occurred.

The concurrent-reply guard did not trigger naturally in this replay. Deterministic
tests cover simultaneous replies, a job opening during an in-flight send, rejected
call replay, separate recipients, a deliberate follow-up, and delayed inbox echoes.

## Reported usage

| Measurement | Control | Initiative decisions |
| --- | ---: | ---: |
| Completed player turns | 6 | 6 |
| Jobs | 20 | 16 |
| Model requests | 106 | 136 |
| Model-facing tool calls | 87 | 120 |
| Input tokens, including cached | 3,480,327 | 4,894,170 |
| Cached input tokens | 2,938,368 | 4,303,616 |
| Uncached input tokens | 541,959 | 590,554 |
| Output tokens, including reasoning | 72,207 | 71,878 |
| Reasoning output tokens | 58,615 | 55,604 |
| New chat messages | 14 | 5 |

The enabled run used 28% more requests and 9% more uncached input than this control.
Its gameplay used 68 requests versus 52; communication used 68 versus 54 despite
fewer jobs and messages. More substantive negotiations and divergent choices make
this unsuitable for estimating the decision field's intrinsic cost. There is no
efficiency-win claim.

Both replay copies were frozen **before** the final sent-message supplement filter
described above. That filter has deterministic regression coverage. These usage
figures include the earlier supplement and do not measure the final preload's exact
size. A formatting-only change to `memory_records.py` also followed the freeze.

This is one paired position and two rounds per condition. The runs overlapped, so
elapsed time is not a controlled latency measurement. Renewals scheduled beyond
turn 140, new review generation, coordinated war, and long-term winning chances
remain outside this replay.

## Validation and artifacts

- `codex-log ./gradlew test --tests uncivai.CommunicationIntegrationTest --tests uncivai.ShortCommunicationIdTest installDist`:
  15 tests passed; installed host rebuilt.
- Final full Python run: 1,037 passed, two opt-in Codex tests skipped. One remaining
  assertion expected the old exception instead of the new structured recovery
  response. Updating it and rerunning all 15 observation tests passed. All observed
  regression failures are resolved; focused contract, scheduler, MCP, chat and
  preload suites also passed.
- Ruff, formatting, Pyrefly and whitespace checks passed.

Local artifacts are under `/tmp/unciv-initiative-fixes-20260930/`: `protocol.json`,
the preparation/runner scripts, both frozen source copies and isolated matches,
`results.json`, `summary.json`, and the collection/verification scripts. They record
matching inputs, actual actions, short references, native payloads, private decision
attribution, completed jobs and reconciled usage.
