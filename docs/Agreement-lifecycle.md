# Enacted agreement lifecycle

This sprint makes a player's enacted trade rows and bilateral relationship
agreements identifiable after settlement and records when they end. Agents can
inspect remaining commitments, notice lost income or access, and review their
plans. The native engine remains responsible for the underlying economic and
diplomatic effects.

**Status:** implemented against engine
`0bad73285ffa306334b89bcecbe395dc3b3b0bda`, with no engine changes. Python validation
passed on September 18, 2026: 398 tests and 90 subtests, followed by 39 focused
verifier tests after the final receipt-check refinement; Ruff lint and format checks
over 123 files; Pyrefly with zero errors and six existing warnings. JVM regression
coverage and the scoped desktop comparison also passed. The full JVM run passed
75 of 76 tests; its sole failure was an old assertion expecting 18 communication
operations instead of 20. After correcting that test, the focused MCP rerun and
Gradle checks passed. Eight targeted JVM checks also passed for the scenario
follow-up. The [live model exercise](Agreement-lifecycle-live-results.md) passed all
12 verification checks, with three completed rounds, a restart and sourced plan
reviews after native endings.

## Native authority and cancellation

The pinned desktop has no ordinary control for cancelling an enacted trade between
major civilizations. [Current trades](../vendor/Unciv/core/src/com/unciv/ui/screens/overviewscreen/TradesOverviewTab.kt)
are display rows. The [major civilization diplomacy table](../vendor/Unciv/core/src/com/unciv/ui/screens/diplomacyscreen/MajorCivDiplomacyTable.kt)
offers trade, friendship, denunciation, war or peace, and demands, without a cancel
trade, friendship, open borders, research agreement or defensive pact button.
Denunciation is not offered while a declaration of friendship is active.

[TradeView.tryRetractOffer](../vendor/Unciv/core/src/com/unciv/view/TradeView.kt)
withdraws an unaccepted offer. It does not affect enacted trades. Consequently this
sprint must expose cancellation as unavailable with an explanation and provide no
new cancellation command. Declaring war remains a separate action with its native
eligibility, confirmation and diplomatic consequences.

## Player contract

The communication API adds two read operations:

| Operation | Purpose |
| --- | --- |
| `agreement_list` | Discover this player's recorded native trade rows and relationship agreements, with stable agreement references. |
| `agreement_read` | Inspect one of this player's agreements, its current or last observed terms, lifecycle status, ending reason and confirmed effects. |

Agreement identifiers are opaque, **player-local references**. The two parties may
have different identifiers for their copies of the same native deal. A reference
does not confer authority to inspect another player's record. Counterparty, give
and receive terms retain the reader's perspective.

Records distinguish `ACTIVE` and `ENDED`. For `kind: TRADE`, these statuses describe
whether the native trade row is present for this player. They do not assert that
every effect of every clause starts or ends together. Immediate gold, city transfers and war
declarations remain historical completed effects; ending their retained trade row
does not undo them. The proposal that created a deal remains `ENACTED` in negotiation
history after its agreement row ends.

Records distinguish `representation: NATIVE_TRADE_ROW` from
`representation: NATIVE_RELATIONSHIP`. The latter independently tracks
`FRIENDSHIP`, `RESEARCH_AGREEMENT`, `DEFENSIVE_PACT` and `EMBASSY` state. These
separate records make continuing effects visible when the native engine removes
a trade bundle without clearing its relationship flags. They are views of native
state, not additional agreements negotiated by the host.

Durations come from the reader's native copy. Positive values are remaining native
turns, zero is an elapsed clause, and `-1` denotes immediate consideration.
`remainingTurns` is null for an immediate-only native row or a permanent embassy.
The two parties' countdowns can differ within one game round because each copy advances at
its owner's end turn. Imported native rows receive references without fabricating
a host proposal or original consent history. `initialGive` and `initialReceive`
preserve the terms when this host first observed the agreement; they do not claim
to reconstruct terms as originally signed before that observation.

Lifecycle notices use communication event kind `agreement`. Supported ending
reasons are:

| Reason | Meaning |
| --- | --- |
| `EXPIRED` | The native timed row completed its countdown and disappeared. |
| `RESOURCE_UNAVAILABLE` | Native resource availability cut a trade short. The event must not expose a partner's private resource supply or the location or cause of its loss. |
| `WAR` | War between the parties removed the row. |
| `DEFENSIVE_PACT_CANCELLED` | Native offensive-war handling removed a defensive-pact trade bundle with another partner. |
| `COMPLETED` | An immediate-only row was removed after its one-time effects had already occurred. |
| `CIVILIZATION_DEFEATED` | Defeat caused the native agreement to disappear. |
| `NATIVE_REMOVAL` | A native row or relationship disappeared without a more specific supported, sufficiently evidenced classification. This includes other native cleanup. |

A reason classifies an observed transition; it is not an alternative engine action.
If the permitted evidence cannot establish a specific cause, preserve that
uncertainty. Agreement endings are separate from withdrawal or rejection of a
pending proposal and from a negotiation's wall-clock deadline.

## Native inventory and effect boundaries

| Surface | Pinned source and behavior |
| --- | --- |
| Active rows and term text | `TradesOverviewTab` renders every bundle in the player's diplomacy managers. `TradeOffer.getOfferText` displays amounts and positive remaining durations. |
| Player-bound snapshots | [PlayerPeaceOperations.activeAgreements](../vendor/Unciv/core/src/com/unciv/logic/civilization/PlayerPeaceOperations.kt) retains all clauses of each present row, including immediate consideration and elapsed clauses while longer terms continue. |
| Durations | [TradeOffer](../vendor/Unciv/core/src/com/unciv/logic/trade/TradeOffer.kt) uses `-1` for immediate terms, `speed.peaceDealDuration` for peace and `speed.dealDuration` for other timed terms. |
| Scheduled expiry | [DiplomacyTurnManager.nextTurnTrades](../vendor/Unciv/core/src/com/unciv/logic/civilization/diplomacy/DiplomacyTurnManager.kt) decrements positive durations and removes a row only when all clauses on both sides are at or below zero. Received terms generate warnings at three and one remaining turns. Row removal generates ending notifications. |
| Turn ownership | [TurnManager.endTurn](../vendor/Unciv/core/src/com/unciv/logic/civilization/managers/TurnManager.kt) advances only that civilization's diplomacy managers. Bilateral copies therefore need not have equal durations mid-round. |
| Resource interruption | `DiplomacyTurnManager.removeUntenableTrades` removes a bundle when the provider cannot sustain a traded non-stockpiled resource, or the resource no longer exists in the ruleset. Both parties receive a generic trade-cut-short notification. A peace treaty in the bundle is recreated with its remaining duration. |
| Bilateral war | [DeclareWar.onWarDeclared](../vendor/Unciv/core/src/com/unciv/logic/civilization/diplomacy/DeclareWar.kt) clears bilateral trades and pending requests and closes borders. `breakTreaties` removes relevant friendship, research and defensive-pact flags. |
| Other defensive pacts | `DeclareWar.removeDefensivePacts` ends the aggressor's pacts, removes the whole associated trade bundle and notifies both parties and common known observers. Economic clauses in that bundle disappear too. |
| Civilization destruction | [Civilization.destroy](../vendor/Unciv/core/src/com/unciv/logic/civilization/Civilization.kt) clears trade rows on both sides and removes related pending requests. |
| Friendship and pact display | [GlobalPoliticsOverviewTable.getPoliticsOfCivTable](../vendor/Unciv/core/src/com/unciv/ui/screens/overviewscreen/GlobalPoliticsOverviewTable.kt) shows remaining friendship or defensive-pact flag turns and substitutes an unknown-civilization label for identities the viewer does not know. Friendship itself is a separate 30-turn flag, not a native trade row. |

The observations below describe the pinned Unciv implementation. They are not a
definition of Civ V rules, and this sprint does not change game rules or introduce
early cancellation. Any discrepancy requiring an engine correction must be handled
as a separate, independently reviewed rules change.

Several implementation details prevent a simple equation of row removal with all
effects ending:

- [DiplomacyManager.goldPerTurn and resourcesFromTrade](../vendor/Unciv/core/src/com/unciv/logic/civilization/diplomacy/DiplomacyManager.kt)
  count the respective clauses of retained rows without filtering each clause's
  duration. An elapsed clause in a mixed-duration bundle can still contribute.
- `DiplomacyManager.updateHasOpenBorders` does check that the relevant clause has
  positive duration. Losing access teleports the affected player's units out of
  the other civilization's territory.
- Resource interruption removes trade rows but does not clear research-agreement
  or defensive-pact flags and status. Those effects may continue after the row
  disappears. A preserved peace-only row must remain inspectable; its remaining
  duration must not be reset to a newly negotiated full term.
- Native `nextTurnTrades` uses `union` on the two offer lists. Equal treaty clauses
  held as distinct objects can therefore tick asymmetrically within one row. In
  the desktop resource fixture, one six-turn peace clause becomes five while its
  equal counterpart remains six; the engine creates its peace remnant from that
  six-turn counterpart. The API and desktop both report six. The host does not
  reset the duration, and this observed behavior is not claimed as a Civ V rule.
- Research-agreement science is awarded after both sides' research flags expire.
  The player's reward can be observed without disclosing the partner's private
  accumulated science. An expired trade row alone is not proof of that reward.
- [Trade](../vendor/Unciv/core/src/com/unciv/logic/trade/Trade.kt) has no persistent
  native identifier. `equalTrade` ignores duration, and `clone` retains the same
  offer objects. Reconciliation must copy values and handle duplicate bundles,
  changing durations and replacement peace rows without treating an engine list
  position as permanent identity.

Current native economic and bilateral state is the evidence for actual effects.
Lifecycle records must not refund immediate payments, replay transfers, reset
treaty durations or replace native cleanup with host-authored rules.

## Privacy, memory and recovery requirements

The implementation follows these requirements, exercised by the passing JVM
lifecycle probe and Python regressions:

- Read and event surfaces include only the authenticated player's own agreement
  records and permitted native observations. Private third-party trades, causal
  map details, treasury balances, research accumulation and unknown identities
  remain absent. Guessing a different player's reference cannot reveal whether
  their private agreement exists.
- Stable references, list ordering and lifecycle changes must not depend on
  unrelated hidden trades. Paired hidden-world tests must check this explicitly.
- The same native transition creates durable agreement events for affected
  players. Those events provide engine-backed sources for private memory and
  flag relevant plans for review. Chat claims alone cannot end a native agreement.
- Host agreement records, lifecycle events, command receipts and the native save
  share one atomic checkpoint. Each civilization service commits received events,
  its cursor and memory review flags together in its own database; coordinated
  recovery restores both sides. Exact retries and repeated reads must not duplicate
  transitions, payments or notifications.
- Restart restores existing references and history without ticking the game or
  inventing new endings. Checkpoint failures before and after replacement must
  follow the same recovery rules as the native save and existing trade receipts.
- Existing format-4 checkpoints without agreement records load with an empty
  registry. Their current native rows receive references on first observation;
  earlier lifecycle history is not reconstructed. Use the updated host to read
  checkpoints containing the new registry.
- Coordinated rollback restores the native agreements and their records together,
  including durations and event cursors, and fences requests from the abandoned
  epoch. A record from a discarded future is not current evidence.

## Acceptance gates

| Gate | Status | Required evidence |
| --- | --- | --- |
| Native source inventory | Completed | Pinned sources and cancellation constraints documented above. |
| Implementation | Implemented | Player API, durable records, events and private-memory integration; no engine changes. |
| Python regression | Passed, September 18, 2026 | 398 tests and 90 subtests, then 39 focused verifier tests after the final receipt refinement; Ruff lint/format checks; Pyrefly with zero errors and six existing warnings. |
| JVM regression | Passed across full run and corrected MCP rerun, September 18, 2026 | All 76 tests exercised; the sole initial failure was the stale schema assertion described above. Lifecycle coverage includes player-bound reads; owner-local countdowns; duplicate rows; scheduled expiry; immediate-only completion; resource interruption with preserved peace; war and defensive-pact bundle removal; retained relationship flags; actual effects; hidden-world equivalence; restart, exact retries, interrupted writes and rollback. |
| Live model exercise | Passed, September 18, 2026 | Rome/Greece negotiated Iron for GPT, both observed native interruption and revised their sourced plans; Rome/France observed normal expiry. Three completed rounds, with two after the planned restart. All 12 verifier checks passed; [evidence and scope](Agreement-lifecycle-live-results.md). |
| Visual desktop acceptance | Scoped comparison passed, September 18, 2026 | Ten inspected native screenshots, current rows and owner-local durations, expiration notices, resource cut-short and peace remnant, absence of direct cancellation controls; details below. |

Automated checks establish the exercised contract; live and graphical results are
recorded independently. These gates do not establish full trade or
player-API parity. City-states, mods and generalized cancellation remain outside
this slice.

The [JVM validation record](../build/verification/agreement-lifecycle-jvm-20260918.json)
retains the full-run results and corrected schema rerun together. The rerun used
`./gradlew spotlessApply test --tests uncivai.S06McpIntegrationTest check --console=plain`;
unchanged passing tests were not repeated after the test-only correction.

### Desktop evidence and reproduction

The [AgreementLifecycleDesktopProbe](../src/test/kotlin/uncivai/AgreementLifecycleDesktopProbe.kt)
uses isolated Vanilla Rome/Greece fixtures and the selected engine's desktop
assets. It creates shortened agreements through native `TradeLogic.acceptTrade`,
advances native `DiplomacyTurnManager.nextTurn`, and records `agreement_list` and
`agreement_read` beside the matching saved-game states. Every exposed term's type,
name, amount, duration and display text is compared with the native row.

The real [desktop launcher](../src/test/kotlin/uncivai/DesktopSaveUiLauncher.kt)
then renders those exact saved fixtures in `TradesOverviewTab`, the diplomacy
screen and the notification overview. All ten framebuffer screenshots at
1100 × 760 were inspected. The task exited successfully and closed the desktop.

Artifacts are retained under
[`build/verification/agreement-lifecycle-desktop-20260918`](../build/verification/agreement-lifecycle-desktop-20260918).
The [acceptance manifest](../build/verification/agreement-lifecycle-desktop-20260918/acceptance.json)
records the engine revision, assertions, screenshot hashes and visual inspection.
Each PNG has a scene-label text file. Five native saves and five `api-*.json` files
retain the before/after native state and player-facing agreement evidence.

| Case | Observed result and evidence |
| --- | --- |
| Current trade | Rome's immediate 7 Gold payment remains visible with its 2 GPT payment, Greece's 1 Iron and Open Borders, each timed term initially at three turns. [Current rows](../build/verification/agreement-lifecycle-desktop-20260918/01-current-rome-three-turns.png). |
| Owner-local countdown | After only Rome's diplomacy tick, Rome sees two turns while Greece sees three. [Rome](../build/verification/agreement-lifecycle-desktop-20260918/03-current-rome-two-turns.png), [Greece](../build/verification/agreement-lifecycle-desktop-20260918/04-current-greece-three-turns.png). |
| Expiry | Native warnings and ending notices appear for both players. The host records `EXPIRED`; native rows disappear, the bilateral GPT flow becomes zero and Rome loses open borders. The original 7 Gold transfer is not repeated or refunded. [Rome notices](../build/verification/agreement-lifecycle-desktop-20260918/05-expiration-notices-rome.png), [Greece notices](../build/verification/agreement-lifecycle-desktop-20260918/06-expiration-notices-greece.png). |
| Resource interruption | A prepared pillaged Roman Iron mine causes the native resource check to remove the Iron/3 GPT bundle. The API records `RESOURCE_UNAVAILABLE`; both players receive the generic cut-short notice. [Rome](../build/verification/agreement-lifecycle-desktop-20260918/09-resource-cut-short-rome.png), [Greece](../build/verification/agreement-lifecycle-desktop-20260918/10-resource-cut-short-greece.png). |
| Preserved peace | The original six-turn peace/resource row becomes a peace-only row with native duration six, for the implementation reason described above. GPT stops; native declaration of war remains unavailable. [Before](../build/verification/agreement-lifecycle-desktop-20260918/07-resource-peace-original-six-turns.png), [remnant](../build/verification/agreement-lifecycle-desktop-20260918/08-resource-peace-remnant-six-turns.png). |
| Cancellation controls | Native Current trades and the major diplomacy screen offer no direct enacted-deal cancellation control; every compared API row reports cancellation unavailable. [Diplomacy controls](../build/verification/agreement-lifecycle-desktop-20260918/02-diplomacy-no-cancel.png). |

Reproduce from a graphical desktop session with a new absolute output directory:

```sh
./gradlew runAgreementLifecycleDesktop \
  -PagreementDesktopDir="$PWD/build/verification/agreement-lifecycle-desktop-new" \
  --console=plain
```

This scoped comparison does not exercise trade publication or acceptance gestures,
ordinary full-turn scheduling, a live model, Gods & Kings rendering, war/pact
cancellation screens, or every screen size. Those are separate from the displayed
native rows and notifications verified here. Prepared durations are fixture inputs,
not a new player ability to edit treaty lengths. No engine code or Civ V rules were
changed. Generated artifacts are local and ignored by Git.
