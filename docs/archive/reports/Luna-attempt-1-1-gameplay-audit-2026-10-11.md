**Gameplay audit: `all-luna-attempt-1-1`, October 11, 2026**

Luna has built substantial research infrastructure and now conducts real combat,
but its economies are falling behind the native controls in three of the four
games. The strongest recurring concern is choosing a sustained Science conversion
plan while population, production, culture, and the eventual victory requirements
remain underdeveloped. There are also clear recoveries from earlier mistakes;
the October 10 findings should not be treated as permanent failures.

This assessment uses fresh turn-boundary saves copied at **13:33:58 UTC** on
October 11. Completed-round comparisons are **224, 257, 230, and 207**; queues
and owned assets come from the corresponding saves at 225, 258, 231, and 208.
The batch still uses host `b2e55af5102ba69bbd252e95947f4791996444a4`, engine
`c93c2d380b0fd4d0e399a8d3738fec3938793792`, and runtime image `b3072dc4d626…`.
These games have **not received the five newly implemented fixes or the
final-era conversion restriction**. They cannot measure those changes' effects.
The 20 Luna seats, native assignments, settings and terrain seed are unchanged
from the [October 10 audit](Luna-attempt-1-1-gameplay-audit-2026-10-10.md).

| Game | Completed round | Native control score | Leading Luna score | Luna mean score | Current assessment |
| --- | ---: | --- | --- | ---: | --- |
| g0001 | 224 | Inca 351 | Persia 613 | 462.4 | All five Luna seats lead; the control has a severe, prolonged happiness problem |
| g0002 | 257 | Greece 798 | France 577 | 462.0 | Inca has the best research output, but Greece has much more population and production |
| g0003 | 230 | France 948 | China 518 | 431.4 | The clearest development deficit; native France leads production, population and culture |
| g0004 | 207 | Greece 576 | Arabia 466 | 410.6 | Most Luna seats lead research, while Greece leads production and culture |

At capture, g0001 and g0003 were stopped for inspection after host status and
heartbeat timeouts, respectively. g0004 was stopped with
`Persia: HOST_PROTOCOL_ERROR; diagnostic strategic-review`. g0002 remained
running. These are recorded operational stops, not defeats. Their root causes
were not established by this gameplay audit; no game was resumed or modified.

**The research advantage is narrower than it first appears.** Fifteen of 20
Luna seats lead their control in technology count, with three ties. Eleven lead
in current science output. Only five lead in production or score, all in g0001.
Equally weighted seat/control averages are:

| Metric | Luna | Native control |
| --- | ---: | ---: |
| Population | 41.3 | 61.5 |
| Production per turn | 69.9 | 106.0 |
| Science per turn | 243.5 | 202.8 |
| Culture per turn | 25.3 | 109.8 |
| Technologies | 40.8 | 36.3 |
| Adopted policies, excluding completion markers | 9.0 | 11.8 |
| Treasury | 980 | 322 |

Excluding the unusually weak g0001 control, Luna averages **68.8 production
versus 132**, **41.9 population versus 70.3**, and **237.9 science versus 251.3**.
Every remaining Luna seat trails its control in both population and production.
These are descriptive comparisons across different positions and turn horizons,
not independent samples or winning probabilities. Current production depends on
assignments, construction bonuses and temporary effects; it is not a normalized
measure of maximum production capacity.

**g0001: useful development and disruption, against a weak control.** Persia
leads with 613 score, 103 production and 287 science. France, Greece, Arabia and
China all also exceed native Inca's score and research output. Native Inca has
been unhappy for 57 consecutive completed turns, is at −11 happiness, and added
no population over the last 20 turns. Its current production is only 28.
Persia's earlier civilian captures and later wars are real disruption, but this
control is a poor measure of the batch's general strength.

Luna warfare is no longer limited to the native seat: Arabia declared on China
at 193, and France on Greece at 214. France's force rating rose by 1,502 over
the last 20 turns while its population rose by one; it still owns the same three
self-founded cities. That investment needs an explicit objective and outcome
review, rather than being judged successful from the force rating alone.
Eleven of the 16 model cities currently run Science.

**g0002: Inca recovered impressively, but Greece has the larger economy.**
Inca now has 63 population, +2 happiness, two completed Research Labs and a
third nearly finished. Its 476 science and 49 technologies exceed Greece's
349 and 41. The original 13-turn unhappy spell ended in the completed-turn
statistics at 138, with some later relapses; the most recent 30 rounds contain
no negative happiness. This is substantial recovery under the old runtime.

Greece nevertheless has 86 population, 155 production and 97 culture, versus
Inca's 63, 88 and 33. France is the leading Luna score seat and produces 312
science, but uses Gold in two cities and has only 186 gold. Persia has invested
heavily in force (3,493) while generating 159 science. Its new fourth city was
founded at 238; its other cities are still catching up on Public Schools.
There are credible science paths here, but the eventual production and resource
requirements need continued investment and verification.

China supplies a particularly clear repeat of the scouting failure. During
turns 238–257 it issued **18 accepted EXPLORE orders to each of two Scouts**,
while civilization-wide exploration rose by only one tile over 20 turns.
The player finally issued explicit destinations on 257, and both receipts show
actual movement with the destination still pending. That corrective action
matters: this is evidence of delayed adaptation, not proof the Scouts remain
stationary. See the [receipt evidence](../../../build/luna-attempt-1-1-audit-20261011/scout-receipts.json).

**g0003: tactical survival has not closed the economic gap.** Native France
has 77 population, 155 production and 220 culture. The strongest Luna values
are 50 population, 76 production and 64 culture; Arabia narrowly leads France
in science, 286 to 267. France acquired Sidon at 215, so some of its recent
population gain comes from acquisition. The score lead is now accompanied by
a large development advantage, beyond the earlier wonder-score explanation.

The earlier concern about China's 680-gold Chu-Ko-Nu investment needs updating.
Arabia declared war on China at 196 and Greece at 200. China subsequently used
its ranged core in repeated attacks, with confirmed kills at 209–210, and
retained all four cities. Peace with Greece enacted at 210 and with Arabia at
228. The army demonstrated practical combat value even though the original
Incan campaign never produced a captured city. Further spending should be
evaluated against that changed defensive role.

Investment comparisons remain a concern. China's turn-230 advice explicitly
keeps Guangzhou on Science because its Public School would lose about 71
science over the chosen 30-turn comparison. The same advice acknowledges a
long remaining research path and France's production advantage. This does not
prove that particular School is optimal; it shows why endpoint assets and
post-completion conversion rates belong in the comparison. Thirteen of 18
model cities currently run Science, including all four Persian cities. Persia
is still researching Scientific Theory and has no Public Schools yet.

**g0004: previous plans eventually recovered, but development remains uneven.**
Arabia leads Luna score at 466; France leads its research output at 270.
Native Greece has 86 production and 101 culture, versus Luna means of 60.4
and 20.2. Nine of 18 model cities run conversion: seven Science and two Gold.

Ecbatana's Library was explicitly moved back ahead of Science at **140**,
after being displaced at 116. It was complete by **157**, as confirmed by the
player's building-sale inventory. The earlier concern was therefore a real
24-turn interruption, not permanent abandonment. The city is now population three,
with a Library and Science active. Arabia also founded Baghdad at **144**,
so the earlier failed Settler did not permanently end its expansion. Several
Arabian and Incan cities are now actively building Universities, Public Schools
or Workshops. These are useful corrections to preserve.

**The recurring issues now have clearer bounds.** Across the batch, **41 of
70 model cities run Science and four run Gold**. The previous audit found only
eight Science cities out of 67. Conversion has become a widespread default,
although many other cities are building research infrastructure and plan to
convert afterward. Removing early conversion will change immediate science and
gold totals; judge the new policy by the resulting development and progress
toward a feasible finish, not by an immediate science increase.

Exploration has improved overall: seven of 20 model seats revealed fewer than
ten tiles in their last 20 turns, down from 14 in the previous audit. Four
revealed none. The China case still demonstrates why repeated orders require
an observed-result check.

Diplomacy continues to revolve heavily around distant victory projects:
**306 of 453 unique messages (67.5%)** in the four latest 20-round windows
mention Apollo, spaceship, Utopia, terminal, United Nations or UN. This is a
keyword count, not a count of useless messages; several also negotiate real
trades or resources. No captured city has Apollo, a spaceship part, the UN or
Utopia in its current queue or completed-building list. Preparing Aluminum
access is useful, but repeated negative status reports should not substitute
for reassessing ordinary rival development.

A separate follow-through problem deserves investigation: **four China–Arabia
native peace proposals expired between turns 206 and 215** before peace finally
enacted at 228 in g0003. The recorded reason is the negotiation deadline, and
the proposals each lacked the second acceptance when they expired. The evidence
does not distinguish delayed scheduling/delivery from player inaction. Conversation
continuity can make the outstanding action visible, but it does not itself
repair a timing or service problem.

The next evaluation should use the new feedback and conversion policy from
matched checkpoints, checking construction completion, productive scouting,
happiness recovery, enacted agreements and investment outcomes. The current
evidence supports those fixes; it does not yet establish their behavioral effect.
The newly implemented mechanisms and software checks are documented in
[gameplay feedback](../../reference/Gameplay-feedback.md).

**Evidence and reproduction.** Reads were limited to local files and read-only
SQLite transactions. The copied saves fix the comparison boundary; command,
message and review extraction is restricted to each completed-round cutoff.
Observation versions are selected through their reachable history at or before
that cutoff. Reviews can describe the next player's actionable position and
are identified by their recorded review turn. Operator-wide comparisons here
do not expand what any individual player may observe.

- [Saved positions, statistics, cities, queues, units and histories](../../../build/luna-attempt-1-1-audit-20261011/summary.json).
- [Computed comparisons and message-count definition](../../../build/luna-attempt-1-1-audit-20261011/analysis.json).
- [Snapshot capture](../../../build/luna-attempt-1-1-audit-20261011/capture.py), [read-only extraction](../../../build/luna-attempt-1-1-audit-20261011/extract.py), and [analysis script](../../../build/luna-attempt-1-1-audit-20261011/analyze.py).
- The same private artifact directory retains each native save and its hash,
  source/runtime metadata, per-player calls and reviews, permitted observations,
  diplomacy records, and the reconstructed China progress feedback.
