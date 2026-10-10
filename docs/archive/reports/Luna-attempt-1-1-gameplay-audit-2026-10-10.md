**Gameplay audit: `all-luna-attempt-1-1`, October 10, 2026**

Luna is competitive in these openings, but its strongest recurring weakness is
checking whether a plan actually produced its intended result. There is useful
economic development and some successful limited warfare. There are also repeated
scouting orders with no exploration, prolonged happiness problems, and extensive
diplomacy about victory projects that remain far beyond the current position.

The four saves were captured at **15:53:39 UTC** on October 10. All four games
were still running when checked again at 16:00 UTC. This is an unfinished-game
audit, not a victory result. Completed-round comparisons use turns **111, 137,
138, and 120**, respectively; city queues and units use the captured saves at
112, 138, 139, and 121. Selected follow-through evidence includes Inca's turn
138 in g0002. Latest completed advice comes from review boundaries 105, 135,
130, and 113. A review boundary and the player's next actionable turn can differ.

All 20 model seats use `gpt-5.6-luna`, maximum gameplay reasoning and medium
communication reasoning, with both strategy guides enabled and a configured
10-turn review interval. The games use Prince, Standard speed, Vanilla,
Pangaea, one terrain seed, and randomized starting positions and assignments.
Running metadata records host `b2e55af5102ba69bbd252e95947f4791996444a4` and engine
`c93c2d380b0fd4d0e399a8d3738fec3938793792`. Findings describe the running batch,
not the uncommitted source changes in the workspace.

| Game | Completed round | Native control score | Leading Luna score | Luna mean score | Assessment |
| --- | ---: | --- | --- | ---: | --- |
| g0001 | 111 | Inca 135 | Persia 315 | 240.2 | All five Luna seats lead a heavily disrupted control |
| g0002 | 137 | Greece 285 | France 336 | 273.4 | France has a substantial research advantage; Inca has a growth problem |
| g0003 | 138 | France 469 | Arabia 344 | 287.8 | Native France leads development; wonders inflate the score gap |
| g0004 | 120 | Greece 332 | Arabia 312 | 253.4 | Native score lead, but Arabia leads science and model France leads production |

Seven of 20 model seats exceed their game's control in score. Nineteen have
higher completed-turn science output, but only ten have more technologies
(two tie), and only six have more production (one ties). Average science is
67.2 versus 36.5, production 45.5 versus 50.8, culture output 14.3 versus 19.8,
and treasury 529 versus 126. These are equally weighted seat/control averages,
not independent samples or estimates of winning chances.

The weak g0001 control matters: excluding that game, model population averages
25.4 versus 29.0 for the controls, and production 45.7 versus 60.3. Conversely,
g0003's 125-point France–Arabia score gap is mostly wonders: subtracting their
200 and 80 wonder points leaves 269 versus 264. France still has meaningful
advantages in production (70 versus 46), population (32 versus 29), and
technologies (24 versus 19). Score alone overstates that particular gap.

**The findings, in priority order:**

1. **Accepted orders are still being mistaken for achieved reconnaissance.**
   In g0004, France issued **17 accepted `EXPLORE` orders to Scout `unit18`
   during turns 100–120**, repeatedly expecting new routes or rival information.
   Its civilization-wide explored-tile count was **246 at both endpoints**.
   The last Scout read still showed an idle unit with no exploration order;
   the save shows it at `(1,6)`, the same destination reached on turn 102.
   France also had a second Scout. Native exploration may be stopping because
   it cannot find a usable route; acceptance does not establish movement.
   The demonstrated gameplay failure is repeating the request without resolving
   that lack of progress. Four of five g0004 model seats revealed zero new tiles
   over the same 20-turn interval, and 14 of 20 model seats across the batch
   revealed fewer than ten in their respective last 20 turns.

   Revisit an exploration order when position and newly explored tiles fail to
   change. Inspect a specific route, access requirement, embarkation option,
   or alternative unit; record an actual constraint if none works. The existing
   player context already supplies unexplored approaches. More generic advice
   to scout is unlikely to solve this case.

2. **Happiness repair loses priority even after growth visibly collapses.**
   g0002 Inca was unhappy for **13 consecutive completed turns, 125–137**.
   Growth output fell from 41 at turn 117 to roughly 11–14 while unhappy;
   treasury rose from 694 at turn 125 to **939 at turn 137**. Happiness ended
   at **−6**, worsened by the expiring Dyes import. The player did queue a
   Colosseum behind its University at turn 126, but allowed the science build
   to finish first. The Colosseum's completion improved happiness to **−4**
   at turn 138, after which the player selected **Oxford** and retained a
   Slinger and Workshop in its other cities. Its turn-138 reads explicitly
   showed legal Circus Maximus options providing +5 happiness in about 14 or
   16 turns in those other cities. Both satellite cities also remained
   unconnected to the capital.

   Oxford has real value, and gold does not make every happiness solution
   purchasable. The failure is that the observed bottleneck did not produce a
   sufficiently urgent comparison of recovery choices. The requested
   Sugar-for-Pearls replacement was still pending; it was not secured happiness.
   Compare the lost growth and follow-on science with the delay to Oxford,
   construction elsewhere, connection benefits under adopted policies, and
   actually available trades or purchases. Avoid an unconditional reserve floor
   or a rule forcing spending.

3. **Distant-project reporting consumes attention and becomes an overly narrow
   strategic trigger.** Across each game's last 20 completed rounds, **197 of
   338 diplomatic messages (58%)** mention Apollo, spaceship parts, Utopia,
   terminal projects, or the UN. This is a keyword count, not a finding that
   every such message was useless; some also negotiated worthwhile trades.
   Nevertheless, repeated reports that nothing has started are conspicuous.
   For example, g0002 Persia repeatedly justified continued restraint or
   development at turns 132–137 by the absence of a verified terminal start.
   g0003 China sent repeated terminal-status checkpoints while researching
   Education and completing its medieval military buildup.

   Total recorded communication jobs through the comparison cutoffs consumed
   about **107.2 million input tokens**, 13.0% of all recorded input tokens;
   many were cached, and this total also includes useful communication. It is
   not a cost estimate for terminal-status messages alone. Prefer contact when
   new information or an expiring term could change a present decision. Rival
   production, research infrastructure, settlement access, and nearby forces
   can justify a reassessment long before Apollo. Social agreements remain
   player choices, not host-enforced restrictions on native-legal actions.

4. **Investment comparisons can reward immediate output while neglecting the
   developed position beyond the comparison window.** The running adviser
   already compares alternatives and reviews outcomes; this capability is not
   missing. However, g0004 Persia's turn-113 review recommended temporarily
   replacing Ecbatana's Library with Science for about **0.5 science per turn**.
   On turn 116 the player inspected the Library: 26/75 production stored,
   approximately 25 turns remaining. It then moved Science ahead of Library;
   the receipt confirms `Science, Library`. The Library remains behind that
   perpetual process in the captured save. It will need another explicit
   production change to resume.

   g0002 Arabia's turn-135 review similarly rejected satellite Universities
   using their 20-turn science losses and static break-even estimates. Those
   estimates explicitly exclude future growth, specialist staffing, subsequent
   builds, empire-wide effects, and rival responses. They are useful inputs,
   but cannot establish the best long-run science plan on their own. Ecbatana's
   short bridge is not yet proof of permanent stagnation: it had run only five
   production turns in the saved position. The concern is the decision method
   and whether its stated temporary window ends. Compare the resulting city
   and its next useful investment at a later checkpoint, and name the event
   that ends conversion. Currently **8 of 67** model cities run Science, so the
   earlier audit's near-empire-wide conversion diagnosis would overstate this
   batch's present behavior.

5. **Some military investments lose their stated purpose before they pay off.**
   g0003 China spent **680 gold on four Archer-to-Chu-Ko-Nu upgrades at turns
   126–127**, explicitly to support a post-restraint Tiwanaku operation. It
   also built additional units and the Statue of Zeus. By turn 138 it had six
   Chu-Ko-Nu and force 826, versus native France's 120. The game had seen no
   major war, China's three cities were all self-founded, and China had
   supported another peace window through turn 140 while shifting to science.
   Its last-20-turn force rose by 407 while population rose by four; France's
   population rose by nine and production by 24 during that interval.

   This does not prove an attack was feasible or that deterrence had no value.
   In particular, the weak French army does not establish a known, reachable
   French target for China. It does show a need to reassess the purpose of
   further military spending when the intended operation is abandoned or
   postponed. Evaluate a specific attainable objective, a justified defensive
   requirement, or a change in investment; do not impose compulsory war.

6. **Settlement plans need earlier access and resource revalidation.**
   g0004 Arabia queued a Settler on turn 97 for a purported two-Horse site.
   It later cancelled blocked or low-value routes, repeatedly requested Chinese
   Open Borders, and ultimately **disbanded the Settler at turn 117 for 25 gold**.
   No fourth city resulted. In the later player-visible settlement observation,
   the Horses near that site were owned by Sydney, not unclaimed resources the
   new city could simply acquire. The records establish the failed expansion;
   they do not establish exactly when the resource ownership changed or that
   another route would have succeeded. Abandoning a bad plan can be sensible.
   The improvement is to revalidate access, ownership, and expected founding
   benefits before continuing construction, movement, or negotiation around
   the original premise.

**There is substantial useful play to preserve.** g0001 Persia actually
captured an Incan Settler at turn 32, two Workers at turn 55, and another Worker
at turn 85; the four capture receipts show no damage to the attackers. Its
advantage over native Inca therefore includes demonstrated disruption, not just
favorable scoring. This also makes the weak control a poor general baseline.
g0001 France completed three Universities and was constructing three Workshops.
g0002 France combined Oxford with a research lead of 26 technologies and 126
science versus Greece's 19 and 46. g0004 France had 74 production versus native
Greece's 54 while building four Universities. g0003 Arabia gained 13 population
over 20 turns. Trade records contain enacted luxury exchanges and research
agreements. These are concrete gains, and the science/growth investments should
not be discarded in a blanket push toward conquest.

The highest-priority follow-up is an outcome check for continuing actions:
did the Scout move or reveal anything, did happiness recover, did the negotiated
access actually become available, and did the military plan produce the result
used to justify it? Next, test whether investment decisions improve when they
compare a developed position beyond the immediate yield window. Evaluate from
identical checkpoints with repeated trials. Neither these four games nor the
older October 7 audit provide a controlled before/after comparison: starts,
civilization assignments, and turn horizons differ.

**Evidence and reproduction.** This audit made read-only queries of live SQLite
stores and copied native game data for analysis. It did not send player messages,
change gameplay, or alter runtime code. The retained private artifacts are:

- [Decoded statistics, city queues, units, and complete ranking histories](../../../build/luna-attempt-1-1-audit-20261010/summary.json).
- [Selected command receipts and their source memory-call row IDs](../../../build/luna-attempt-1-1-audit-20261010/case-evidence.json).
- [Read-only extraction script](../../../build/luna-attempt-1-1-audit-20261010/extract.py).
- The same artifact directory contains parsed game saves, per-player reviews,
  command records, player-visible observations, diplomatic records, and usage.
  Native saves were captured together; SQLite reads happened shortly afterward
  and were restricted to comparison cutoffs where applicable. Observation heads
  can describe a player's following turn and are used as explicitly identified
  follow-through evidence, not for the completed-round ranking table.

The audit uses operator access for aggregate comparisons. Proposed player-facing
improvements must continue to use only that player's permitted observations.

**All-seat reference.** Score, population, production, gold, happiness and
technologies below come from the same completed round within each game. Science
and culture use that round's stored output. City counts use the captured save.

| Game / turn | Civilization | Control | Cities | Score | Pop. | Prod. | Science | Culture | Gold | Happy | Techs |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| g0001 / 111 | France | Luna | 3 | 280 | 27 | 44 | 75 | 27 | 334 | 5 | 21 |
| g0001 / 111 | Greece | Luna | 3 | 201 | 21 | 44 | 47 | 7 | 644 | 6 | 18 |
| g0001 / 111 | Arabia | Luna | 3 | 212 | 24 | 42 | 49 | 9 | 871 | 2 | 19 |
| g0001 / 111 | China | Luna | 3 | 193 | 20 | 32 | 41 | 7 | 426 | 4 | 18 |
| g0001 / 111 | Persia | Luna | 4 | 315 | 26 | 62 | 53 | 19 | 418 | 3 | 16 |
| g0001 / 111 | Inca | Native | 2 | 135 | 10 | 22 | 13 | 7 | 97 | 9 | 15 |
| g0002 / 137 | Inca | Luna | 3 | 262 | 32 | 44 | 107 | 16 | 939 | -6 | 24 |
| g0002 / 137 | France | Luna | 4 | 336 | 29 | 45 | 126 | 22 | 78 | 2 | 26 |
| g0002 / 137 | Arabia | Luna | 3 | 214 | 22 | 33 | 53 | 10 | 921 | 4 | 20 |
| g0002 / 137 | China | Luna | 4 | 286 | 23 | 51 | 53 | 21 | 125 | 13 | 20 |
| g0002 / 137 | Persia | Luna | 3 | 269 | 22 | 46 | 65 | 15 | 425 | 2 | 23 |
| g0002 / 137 | Greece | Native | 4 | 285 | 25 | 57 | 46 | 26 | 224 | -6 | 19 |
| g0003 / 138 | Inca | Luna | 3 | 237 | 28 | 49 | 98 | 5 | 576 | 4 | 22 |
| g0003 / 138 | Greece | Luna | 3 | 271 | 24 | 46 | 61 | 23 | 787 | 4 | 23 |
| g0003 / 138 | Arabia | Luna | 4 | 344 | 29 | 46 | 58 | 17 | 712 | 11 | 19 |
| g0003 / 138 | China | Luna | 3 | 336 | 21 | 44 | 45 | 13 | 749 | 11 | 20 |
| g0003 / 138 | Persia | Luna | 4 | 251 | 27 | 41 | 62 | 12 | 595 | 4 | 19 |
| g0003 / 138 | France | Native | 3 | 469 | 32 | 70 | 51 | 31 | 232 | -1 | 24 |
| g0004 / 120 | Inca | Luna | 3 | 204 | 22 | 32 | 61 | 11 | 261 | 13 | 17 |
| g0004 / 120 | France | Luna | 4 | 267 | 29 | 74 | 70 | 25 | 43 | 5 | 20 |
| g0004 / 120 | Arabia | Luna | 3 | 312 | 29 | 37 | 106 | 7 | 810 | 10 | 19 |
| g0004 / 120 | China | Luna | 3 | 200 | 20 | 43 | 44 | 7 | 549 | 3 | 18 |
| g0004 / 120 | Persia | Luna | 4 | 284 | 24 | 54 | 71 | 13 | 322 | 3 | 20 |
| g0004 / 120 | Greece | Native | 4 | 332 | 30 | 54 | 36 | 15 | -49 | -1 | 20 |
