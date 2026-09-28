# Routine strategic review — France, September 28, 2026

France completed the new review from the start of turn 243, using the original
game's **gpt-5.6-luna, max reasoning**, fast priority and disabled strategy guide.
It retained Science as its preferred victory route and took the same native
actions as the original turn. This demonstrates the workflow, not better play.

## Implemented behavior

From turn 100, a review is due every five turns. An overdue review is completed
on the next activation, so this trial reviewed on 243 and would review again on
245. The review covers every enabled victory route, remaining requirements,
completion projections, rivals and decisions. It also compares alternatives,
disruption and the relative value of agreements.

Plans use short, mid and long horizons. The agent chooses appropriate timeframes
and records approximate timing, milestones, dependencies and conditions for
changing course. There is no fixed 5–50-turn horizon.

The agent saves the review before actions or diplomatic commitments. The service
checks completeness against the enabled victories and a delivered current
overview; it does not judge the strategic opinion or change native legality.
Completion survives restart and rewinds with memory. Communication jobs cannot
fulfill a gameplay review. The single strategy record is replaced within its
existing size limit, and historical revisions remain available.

See the [memory contract](../../architecture/Structured-memory.md#routine-strategic-review)
and [preload description](../../architecture/Agent-context.md).

## Trial setup

The trial used the coordinated start-of-France-turn-243 boundary from
`vanilla-26_09_21`, including existing private memory and diplomacy. The original
game was read only. The copy lives at `/tmp/france-strategy-review-20260928` and
stopped at turn 244 after France's handoff and background work.

Copied service bindings and pending-finalization endpoints were redirected to the
isolated services. One telemetry report from an original-game activation created
after the captured boundary was excluded from replay into the copy. No model
decision or game action occurred during the failed setup attempts.

The original France activation was `413e3b22-272e-4e4b-837d-41a40e1cbb96`;
the reviewed activation was `f0341b37-72a0-475e-af66-05f7e31176c1`. Both report the
same effective model and reasoning effort. The comparison is one observation per
variant, without a win-rate or long-term execution measurement.

## Decisions and execution

France's accepted review said:

- **Science:** aim for turns 295–330; Apollo was complete, Satellites was about
  four turns away, and Paris had 149 production. Greece had 1/6 spaceship parts
  and led technology count.
- **Culture:** strongest fallback, with three of five branches complete, but
  judged slower because of remaining policies and Utopia.
- **Domination:** reject a campaign as too costly to prepare.
- **Diplomacy:** reject the UN/vote route as insufficiently prepared.
- **Time:** Egypt led score, making a turn-limit win unfavorable.
- **Short term:** finish Satellites and start parts when unlocked.
- **Mid term:** pursue part prerequisites and production during turns 248–275.
- **Long term:** assemble the spaceship during turns 276–330; reconsider if
  Greece's lead becomes unanswerable or the route becomes too slow.
- **Agreements:** acknowledge that research agreements also accelerate Greece;
  reassess renewals when existing agreements expire.

The review initially exceeded the 8,000-character content limit (9,191 serialized
characters). The model shortened it to 6,421 characters and saved successfully.
It also retired six handled diplomatic contributions. No action was dispatched
before the accepted review.

Both the original and reviewed turn acknowledged the Golden Age alert and ended
the turn. Neither changed research, production, units or diplomacy. Both cities
remained on Science conversion while spaceship-part technology was unavailable.
The original inspected construction, research and city details; the review trial
used its preload without additional focused game reads.

## Assessment

The new review made alternatives and relative agreement benefits explicit, but
the strategic conclusion remains weakly supported:

- It did not estimate Greece's completion date or establish why France would
  finish first. Its own turns-295–330 projection was not backed by an inspected
  research and production schedule.
- Several fallback triggers still emphasize finishing before the turn limit,
  although an earlier rival victory is the more immediate risk.
- It omitted one undisclosed capital from its prose list of remaining domination
  targets, despite the overview showing three incomplete capital objectives.
- It treated Culture as a fallback while also describing it as likely too slow.

These are model assessment errors or unresolved uncertainties, not evidence that
the host should prescribe war or reject a legal strategy. The next useful check
is whether subsequent reviews respond to actual rival milestones with concrete
timing and action changes. This single turn does not establish that they will.

## Cost and validation

France-only usage includes each activation's background memory; input includes
cached input and output includes reasoning. Copied Greece bookkeeping is excluded.

| Measurement | Original turn | Review trial |
| --- | ---: | ---: |
| Input tokens | 289,674 | 200,038 |
| Uncached input tokens | 43,658 | 42,598 |
| Output tokens | 4,387 | 7,826 |
| Gameplay seconds | 38.2 | 99.0 |
| Background memory seconds | 26.1 | 4.4 |
| Model-issued game/memory tool calls | 9 | 4 |

Different reads and the rejected review write affect this comparison. The lower
input count does not demonstrate an efficiency improvement; the reviewed turn
took longer and produced more output.

The full Python run passed 840 tests and 75 subtests, with two opt-in checks
skipped. Two trade-race fixtures needed to represent a completed turn-110 review;
after that adjustment, all 41 strategic-review and trade-gateway tests passed.
Coverage includes cadence, overdue reviews, incomplete coverage, communication
isolation, rejected-call replay, durable completion and coordinated rewind.
Ruff, formatting, Pyrefly and Gradle `installDist` passed. No engine changes were
needed for this feature.

## Follow-up: independent adviser

The scheduled service now prepares the review in a separate read-only conversation,
using the same configured model, reasoning effort and priority as gameplay. It
receives current player-visible facts, victory progress, diplomatic obligations
and the full research cost catalogue. It does not receive private strategy,
handoff notes or contributions. The gameplay agent receives fresh observations
and the report, then adopts or revises it. The adviser cannot mutate the game,
send messages or write memory. Its deliberation is excluded from background note
inputs; the player's adopted strategy remains the durable decision.

The follow-up exposed missing information: named, locked spaceship parts and
victory projects lacked production estimates. They now return native costs,
technologies and resources while remaining unavailable to build. For example,
France's Paris estimate for an Engine was 502 production, four turns at the
current rate, requiring Particle Physics; Orleans needed nineteen turns. Part
counts are also visible before Apollo is complete. These are hypothetical build
times, excluding prerequisite research and travel, not promises of availability.

### Replay and actual decisions

The successful replay is `/tmp/france-adviser-final-20260928`, activation
`8c8286dc-99d3-4834-8f12-afe84fbc63c0`, from the same start-of-France-turn-243
boundary. Both adviser and player used **gpt-5.6-luna / max**, with fast priority
and no guide. The run stopped normally at its turn-244 budget after notes finished.

The adviser calculated a unique remaining research cost of 36,103, less 3,894
already stored on Satellites: 32,209 science, or about ninety turns at the current
360 science/turn. It inspected part costs, policy progress, diplomatic consequences,
rival charts and map regions. It challenged the previous turns-295–330 science
projection and recommended a 4–7-turn reconnaissance/force check for a possible
domination pivot. It still rejected an immediate declaration without a credible
campaign plan, given the Egypt–Greece defensive relationship.

France adopted that reconnaissance decision point and made two concrete changes:
it upgraded both Anti-Tank Guns to Helicopter Gunships for **420 gold total**.
It then acknowledged the Golden Age and ended the turn. It kept both cities on
Science to finish Satellites, made no movement or production changes, and did not
declare war. Its saved review includes a military decision around turns 247–250.
This measures one turn's decisions, not subsequent follow-through or victory odds.

### Remaining weaknesses

- The adviser added most part production after the research chain, despite the
  possibility of overlapping them. Research-agreement acceleration was not
  quantified. Its roughly-turn-350 finish is therefore not a reliable forecast.
  The player further misstated that as a research-only finish in its assessment.
- The report conditionally suggested securing more Aluminum if part requirements
  accumulate, without resolving that assembling a part consumes the unit and
  releases its resource use. That uncertainty is not a new host restriction.
- Rival completion timing remained unknown. The fallback still emphasized the
  turn limit, and the military gate could lead to further deferral.
- Two adviser queries had invalid arguments. Its report was 8,682 serialized
  characters despite the requested concise format. The player's first strategy
  write exceeded the existing entry limit; its shortened replacement succeeded.

The independent review produced more concrete analysis and changed some actions.
It did not establish that France can win or that the new strategy is better. This
comparison also changes available facts and instructions; it does not isolate
inherited memory as the cause of the earlier recommendation.

### Cost

France-only, including its background notes. Input includes cached tokens; output
includes reasoning. Copied Greece bookkeeping and earlier failed setup attempts
are excluded from this table.

| Measurement | Adviser | Gameplay conversation and notes | Combined |
| --- | ---: | ---: | ---: |
| Input tokens | 698,147 | 446,445 | 1,144,592 |
| Uncached input tokens | 91,427 | 56,813 | 148,240 |
| Output tokens | 26,789 | 10,160 | 36,949 |
| Reasoning output tokens | 23,065 | 5,430 | 28,495 |
| Model-issued game/memory tool calls | 27 | 10 | 37 |

The adviser took **344.6 seconds**. Total gameplay took **462.0 seconds**, followed
by **22.2 seconds** of notes, compared with 99.0 + 4.4 seconds for the earlier
same-conversation review. Final combined usage is complete. This is a substantial
cost on scheduled review turns; it is not an efficiency improvement.

### Integration and validation

The first adviser-only attempt found that the scheduler interpreted switching to
the player conversation as an unexpected resume. Adviser conversation IDs now live
in their own activation section; the main `threadId` is reserved for gameplay.
That attempt's adviser usage was retained in its own interrupted activation in
`/tmp/france-independent-review-20260928`; it made no France game changes.
Subsequent setup retries also exercised loading old saved observation records and
exhausted the copied Greece note-writer retry allowance. The successful trial used
a fresh copy of the same original boundary, without changing production recovery
or retry behavior.

The full Python suite passed **849 tests and 75 subtests**, with two opt-in checks
skipped. After narrowing the review filter to preserve player-authored nested
terms, all **7 adviser tests** passed. The **18 observation integration tests** and
**4 victory progress/statistics tests** passed, as did Ruff, formatting, Pyrefly
and `installDist`. Adviser tests cover read-only enforcement, context isolation,
call-ID collisions, independent threads with pinned settings, early events,
additive/partial usage, invalid reports, timeout cleanup and note-input isolation.
