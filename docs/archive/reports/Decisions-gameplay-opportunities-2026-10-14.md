The saved games support two concrete gameplay experiments for Decisions:
checking whether a player-authored condition has actually been established,
and detecting when new native estimates undermine a saved plan's timing.
They also show why generic warnings about reserves, stationary units, or accepted
commands would be poor uses of a paid classifier.

This is an offline retrospective. No Decisions or gameplay-model calls were made,
and no running game, checkpoint, configuration or runtime code was changed. The
examples identify questions worth evaluating; they do not establish that
Decisions would answer them correctly or improve play.

The review screened the latest frozen player overviews and recorded reviews in
eight saved games, covering 40 model seats. The completed-turn cutoffs were:

| Batch | Game 1 | Game 2 | Game 3 | Game 4 |
| --- | ---: | ---: | ---: | ---: |
| `all-luna-attempt-2-0` | 115 | 110 | 118 | 100 |
| `luna-terra-attempt-1-0` | 101 | 110 | 109 | 105 |

Five examples received closer inspection through actual tool calls, delivered
observation versions and saved plans. One is an older China sequence from
`all-luna-attempt-1-1/g0002`, turns 238–257. Screening 40 seats is not an exhaustive
audit of every decision in their histories.

The extraction verifies host and player checkpoint file hashes, uses read-only
SQLite transactions, and restricts selected gameplay jobs to completed jobs in
the frozen player checkpoint. Candidate inputs contain that player's recorded
information. Later outcomes are stored separately. Analyst labels were assigned
retrospectively and are not a blinded evaluation.

**Greece's Worker raid: check the stated withdrawal condition.**

In mixed game 3 on turn 99, Greece saved a narrowly conditional decision: gain
one Arabian Worker only if its Scout could safely capture and withdraw, abandoning
the raid if safe withdrawal was unavailable. Its map read already showed Damascus
at `(-2,-10)` and an Arabian Archer nearby. The native attack preview offered
the Worker capture at `(0,-9)` with zero immediate damage, while explicitly
showing the Archer two hexes from the destination.

The attack succeeded. Afterward, the Scout had `1/2` movement and six offered
moves; the captured Worker had `0/2` movement and no offered moves. Greece ordered
the Scout to escort the Worker and ended the turn on the capture tile. At the
next Greek turn, its own notifications reported 74 damage from Damascus and the
remaining 26 from the Archer, destroying the Scout. The Worker survived and was
moved away, so this was a Worker-for-Scout exchange rather than a failed capture.

A bounded question before ending the turn could have been: **Does the recorded
evidence establish the saved safe-withdrawal condition, or should it be reviewed?**
The useful output would identify an unverified condition and its source. It
would invite the model to inspect the retreat or explicitly reconsider the
tradeoff. It would not predict the enemy's exact actions, prohibit the raid,
or establish that exchanging a Scout for a Worker is strategically wrong.

This is a stronger semantic use than detecting a nearby hostile, which is already
a factual native observation. The potential added value is connecting that
observation to the player's particular condition. [Frozen case and separate
outcome](../../../build/decisions-gameplay-review-20261014/cases/raid.json),
[original selected calls](../../../build/decisions-gameplay-review-20261014/cases/raid-raw-calls.json).

**France's University: notice that the timing assumption changed.**

In all-Luna game 1, France's delivered turn-100 plan expected the four
Universities to finish over roughly the following 12–28 turns. Troyes initially
had six production per turn. Its delivered turn-106 observation showed 24 turns
remaining for its University. On turn 107, displayed production was two and the
native remaining-time estimate was 67 turns. The queue remained University.

France's normal turn-110 review identified the bottleneck, using a later estimate
of about 63 turns, and recommended changing citizen assignments. On turn 111 the
player selected the native `ProductionFocus`; its delivered city record showed
six production and a 21-turn University estimate. The retrospective does not
establish what caused the original assignment change.

The bounded question is: **Do the new native construction estimates materially
undermine this delivered plan's near-term timing?** Numerical changes can be
detected deterministically; Decisions could judge their relevance to the written
plan and decide whether to surface an advisory or request an eligible review.
The plausible gain here is a few turns of earlier attention. It is not a claim
that 43 turns were lost: the existing review already caused a correction.

Current review triage receives a reduced material-fact projection and historical
progress. It does not explicitly receive this native ETA change. A meaningful
experiment would add the relevant estimate and pinned plan to the evidence,
rather than restore ranking of native suggestions. [Before/after case](../../../build/decisions-gameplay-review-20261014/cases/production-drift.json),
[delivered observation versions](../../../build/decisions-gameplay-review-20261014/cases/production-drift-observations.json).

**China's repeated scouting: use factual detection first.**

In the older all-Luna game 2, turns 238–257 contain 18 accepted `EXPLORE`
orders for each of China's two Scouts. Its delivered empire exploration count
increased from 820 to 821. Explicit destination orders on turn 257 finally
produced actual movement for both Scouts.

Repeated accepted orders plus negligible observed progress are directly
detectable from the journal. The existing gameplay-feedback work already exposes
this type of evidence. Decisions might judge whether a stalled reconnaissance
task matters to a written objective, but it is unnecessary for detecting the
loop. Aggregate exploration alone also cannot identify which unit caused a
revealed tile. [Orders, observations and corrective receipts](../../../build/decisions-gameplay-review-20261014/cases/scouting-control.json).

**China's treasury: a large reserve needs context.**

All-Luna game 4 China's turn-100 overview showed 1,475 gold. Its delivered plan
already included a conditional military use for reserves. The subsequent
turn-100 review priced five Archer upgrades at 850 gold and a Spearman upgrade
at 75, while retaining a development plan as the preferred choice. The later
review is kept outside the proposed turn-100 classifier input.

This does not prove the reserve size was optimal or every contingency timely.
It does mean that "large treasury implies missed spending" would be a poor
label. A useful question would require actual native purchase alternatives,
their delays, and the saved reason for reserving funds. An automatic spending
recommendation from the balance alone would add little. [Player context and
separate subsequent review](../../../build/decisions-gameplay-review-20261014/cases/reserve-control.json).

**Inca's surviving Brute: structured outcomes already answer the question.**

Mixed game 1 Inca's turn-100 city attack carried an expected outcome that the
Brute would be destroyed. The receipt reported 97 damage, three defender health
remaining and `defenderDestroyed=false`. A semantic objective-versus-outcome
checker could flag this, but those structured fields make a paid call unnecessary
for this particular outcome. This should be a control case for evaluating
whether a proposed classifier adds anything beyond ordinary receipt handling.
[Expected outcome and native receipt](../../../build/decisions-gameplay-review-20261014/cases/outcome-control.json).

The first experiment should therefore evaluate **condition coverage and plan
assumption changes**. Deterministic code would shortlist material changes and
relevant proposed actions. Fixed Decisions questions would return a concern
category tied to existing sources, with an explicit insufficient-evidence
alternative. The gameplay model would receive any accepted notice alongside its
ordinary evidence and retain complete freedom to choose native-legal actions.

Neither experiment needs another general native-advice selector. The raid case
would require an optional check at a relevant action/turn boundary; startup-only
memory triage cannot see evidence first acquired later in that activation. The
University case fits review triage once its recorded evidence includes the
material estimate change. Stable inputs should be cached, and deterministic
outcome checks should stay outside paid work.

Before live delivery, compare these questions against factual checks and the
unassisted gameplay model on more cases, including deliberate sacrifices,
justified growth tradeoffs, completed recoveries and purposeful reserves. Measure
useful additional findings, missed issues, distracting notices, latency and spend.
This review supplies two positive candidate examples and three controls; it is
too small and retrospectively selected to estimate accuracy or playing strength.

The [artifact directory](../../../build/decisions-gameplay-review-20261014/) retains
the eight frozen native saves, per-player checkpoint references, overviews,
reviews, hashes and five detailed cases. The [screening script](../../../build/decisions-gameplay-review-20261014/review_saved_games.py)
and [case extractor](../../../build/decisions-gameplay-review-20261014/extract_cases.py)
document the read-only queries. Their scripts read live local journals; the
frozen outputs preserve the reviewed evidence if games later advance.
