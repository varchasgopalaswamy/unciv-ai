# Social promises: live Rome–Greece–France exercise

Status: passed on September 19, 2026. Three complete rounds and nine handoffs
finished, including two rounds after the planned restart. The original run
needed recovery from the clock gap described below. The match is paused and all
operator-started services are stopped.

This run exercises the [social promise contract](Social-commitments.md) with
live agents using the `war-paid` opening. Greece and France begin with prepared
history: an accepted promise not to attack each other, conditioned on remaining
“good neighbors.” The agents did not negotiate that initial record live.

## Guided objectives

Greece reads the promise and advisory, explains its interpretation to France,
negotiates a payment from Rome, and proceeds with native-legal war while leaving
the original promise and consent unchanged. France supplies its own interpretation
and chooses its response. Agreement on meaning is not required. These are test
objectives, not host legality rules or an automatic judgment of breach.

Greece then negotiates Roman entry into the war. After one completed round, the
operator archives the checkpoint and private memory, restarts all four services,
and resumes for two further rounds. Prices and military orders remain agent
choices within the guided scenario.

## Observed opening

Greece described “good neighbors” as ongoing peaceful cooperation, acknowledged
that its intended paid war might conflict with the promise, and explicitly made
no allegation that France had violated it. France replied that a paid attack
would be inconsistent with its interpretation, regardless of unchanged recorded
consent. These are attributed player positions, not host findings.

Greece received the exact promise/advisory in trade reads at 13:45:08 UTC,
before consenting through its counteroffer at 13:45:33.780. Rome accepted at
13:46:30.080. Settlement transferred 50 Gold from Rome to Greece and created
only the Greece–France war. Both original social acceptances remained in place.
Greece subsequently offered Rome 20 Gold to join, and Rome accepted. Both
canonical milestones passed their native settlement and consent checks.

The players voluntarily shared information: Greece disclosed Roman backing to
France, and France told Rome about the promise. Such messages are legitimate
knowledge sources; they must be distinguished from automatic private-record
exposure. The opening review found no automatic outsider exposure in the
inspected host event streams and tool results. France recorded the price as
Rome's claim; the operator independently verified it from settlement evidence.

Round one completed at 14:13 UTC. The operator paused the match, archived the
checkpoint and all three private-memory databases, stopped the original host
and agent services, and resumed the same game with new processes. Both enacted
deals remained present. All 15 restart-review checks passed, including exact
retention of 1,466 archived private-memory rows. One complete continuation round
and Rome's next turn finished normally.

During Greece's final turn, logs jump from 14:37:28 to 18:49:11 UTC. The scheduler
then reported its wall-time budget exhausted; the cause of the clock gap is not
established by these records. The operator preserved the interrupted checkpoint
and failing continuation report, paused the game and stopped all services. Both
settlements, the social promise and restart preservation still passed. The last
two turns were resumed from the same game with a reported 25-minute completion
window; the cumulative deadline includes the intervening gap. No model, game
state, contract or social consent was replaced. Both remaining turns finished
at 19:03 UTC, within that window.

## Final acceptance

| Check | Result |
| --- | --- |
| Advice delivered before Greek consent | Passed: original terms and ambiguous conditions were delivered before the counteroffer consent. |
| Player interpretation and discussion | Passed: Greece explained its reading, France objected, and Greece acknowledged the conflict while choosing war. No host breach verdict or compulsory clarification. |
| Native action with unchanged promise | Passed: original offer and both acceptances exactly match the opening; no withdrawal, reconsideration or revision. |
| Negotiated native effects | Passed: 50 Gold from Rome to Greece for Greek war; 20 Gold from Greece to Rome for Roman entry. Exact consent and one settlement per deal. |
| Visibility and knowledge provenance | Passed for the inspected streams and tool results: no automatic outsider exposure; voluntary disclosures remain attributed to their speakers. |
| Planned service restart | Passed: four distinct replacement processes, exact retained host records and 1,466 archived private-memory rows. |
| Continued play | Passed: final round 3, Rome next, nine handoffs; no extra or repeated deals. |
| Safe stopping | Passed: game and communication paused; all harness-owned processes exited. |

All nine canonical war-verifier checks and all 18 final social-review checks
passed within their stated scope. The final transcript scan covered 34 history
files and 6,372 events without truncation or malformed lines. All 12 active
private plans retained sources and had no outstanding review flags. The
agents handled native alerts, made ordinary city and unit decisions, and updated
private plans. Recoverable stale-memory, detail and command-schema errors occurred;
the agents refreshed evidence or corrected arguments and continued.

There were **no unit attacks or city bombardments**: players chose scouting and
fortified positions without visible legal targets. This live run demonstrates
war declarations despite an accepted social promise. Attack and bombardment
behavior remains covered by the scripted probes, not this live exercise. It also
does not test a human chat interface, a newly negotiated social promise, direct
declaration commands, or every private/hidden game state.

## Run configuration and evidence

- Configured model: `gpt-6-astra`, reasoning `max`; no model override.
- Initial bounds: 96 invocations, 90 minutes including restart downtime,
  600 seconds per activation, 900 seconds per binding negotiation.
- Actual gameplay: ten activations, including the interrupted Greek activation;
  nine successful handoffs. The reported completion window adjusted the
  cumulative limit to 20,062 seconds after the clock gap.
- Runtime preparation: all four `GameLaunchIntegrationTest` cases passed;
  formatting and `installDist` completed. No Python distribution build.
- State and raw private evidence:
  `build/verification/social-promises-live-20260919-1/`.
- Operator harness, read-only transcript audit helper and preparation log are
  retained under that directory's `operator-scripts/`.

Local reports:

- [Canonical war verification](../build/verification/social-promises-live-20260919-1/verification/war-scenario.json)
- [Opening social and disclosure review](../build/verification/social-promises-live-20260919-1/verification/social-opening-review.json)
- [Planned restart review](../build/verification/social-promises-live-20260919-1/verification/restart-review.json)
- [Final social evidence](../build/verification/social-promises-live-20260919-1/verification/social-final-evidence.json)
- [Final social review](../build/verification/social-promises-live-20260919-1/verification/social-final-review.json)
- [Final state and shutdown review](../build/verification/social-promises-live-20260919-1/verification/final-state-review.json)
- [Interrupted continuation report](../build/verification/social-promises-live-20260919-1/verification/interrupted-war-scenario.json)
- [Process history](../build/verification/social-promises-live-20260919-1/operator-process-log.json)

The canonical war verifier checks native effects, exact consent and recovery.
Separate transcript review checks delivered advisories, player interpretations,
unchanged social consent and knowledge provenance. A single live run cannot
establish complete API coverage or prove privacy for all hidden game states.
