# Sprint handoff

Use this page to resume implementation; the [player API plan](Player-api-plan.md)
and [coverage ledger](Player-api-coverage.md) retain the broader contract.

## Previous delivery: third-party war trades

The September 14, 2026 sprint adds paid and joint war declarations through the
existing versioned trade flow. See [scope, inventory and verification](Third-party-war-trades.md).
It follows the delivered trade, compact-observation and ordinary-notification slices.

The implementation includes shared native trade options and effects, per-player
declaration warnings, peace-commitment checks for both signers, private receipts,
target notifications, memory invalidation, scheduled turns and checkpoint recovery.
The engine already provides the trade-screen operations; this delivery uses the
recorded engine revision without new engine changes. Live-model and visual desktop
acceptance remain separate checks.

Automated validation passed: 74 Kotlin tests, 329 Python tests and 86 subtests,
plus formatting, lint and types. Regression fixes cover imported requests naming
unknown war targets and native notifications appended during conquered-save load.
Routine checks now use the source checkout without building a Python distribution.

## Guided war exercise follow-up (September 18)

The [Three-Cornered War launcher](War-trades-live-scenario.md) now provides
`war-paid` and `war-joint`, private role instructions and a checkpoint verifier.
Both scripted native scenarios and the full JVM check pass. The Python suite has
346 passing tests and 86 subtests, including a live-discovered null-trade rejection
regression. Scenario identity is checked before a scheduled checkpoint can be
rewritten during resume.

The [live results and evidence](War-trades-live-results.md) confirm 60 Gold from
Rome to Greece for Greece's declaration, followed by 20 Gold from Greece to Rome
for Roman entry. A separate joint-war run enacted both declarations with a
50-Gold Roman payment. Consent, native settlements, target privacy and Run A's
planned restart preservation passed. The first attempted run exposed the fixed
receipt-handler failure and is preserved separately.

Both initial one-hour runs stopped during their second continuation round. At the
user's request, the original saved games resumed with additional windows of
30 minutes for Run A and 15 minutes for Run B. Both reached native turn 3 and
handoff 9 within those windows; every verifier check now passes, including two
rounds after Run A's planned restart. Original deals, outcomes and all prior
communication receipts remain unchanged. Final checkpoints are paused and all
services are stopped.

[Desktop acceptance](War-trades-desktop-acceptance.md) also passed: 16 actual
rendered screenshots, native offer/acceptance buttons, target war notices,
relationships and the shared native promise/pact warning. A reusable
`runWarTradeDesktop` task captures isolated Vanilla fixtures and checks native
balances and wars. Targeted compilation and formatting passed. Prepared draft
terms and direct-declaration warning inspection are explicit limits of this UI
exercise; it does not establish full player-API parity.

## Current sprint: enacted agreement lifecycle

Make the end of an enacted agreement inspectable and actionable through the same
player, notification and memory surfaces as its creation.

The [implementation and native inventory](Agreement-lifecycle.md) add
`agreement_list` and `agreement_read`, stable player-local references, per-owner
countdowns, durable ending events and private plan review. Trade rows and native
friendship, research, defensive-pact and embassy state are tracked separately:
removing a row does not prove every effect ended. Pending proposals retain their
historical `ENACTED` outcome.

The native UI has no voluntary early cancellation control for these enacted
agreements. The API therefore reports cancellation unavailable and adds no new
game rule. Expiration, resource interruption and war use existing native logic;
engine differences are documented rather than presented as Civ V rules.

Automated verification passed: 76 JVM tests across the full run and a focused
rerun after correcting an outdated schema assertion, plus eight targeted JVM
checks for the live scenario follow-up; 398 Python tests and 90 subtests, followed
by 39 focused verifier tests after the final receipt refinement; formatting, lint
and type checks. The scoped desktop comparison passed with ten inspected screenshots.

The [live lifecycle exercise](Agreement-lifecycle-live-results.md) also passed:
Rome supplied one Iron for Greece's two GPT at the normal native duration; the
prepared Rome–France agreement expired, and France's legal mine raid caused native
resource interruption. Both affected agents refreshed their agreement evidence and
revised their existing plans. The run completed three rounds, including two after
the planned service restart, in 31 minutes with 19 recorded model invocations.
All 12 verifier checks passed; the final match is paused and services are stopped.
The sprint's automated, scoped desktop and live acceptance gates are complete.

1. Inventory the desktop's active-trade rows, duration displays, expiration and
   resource-loss notifications, war cancellation, and any ordinary cancellation
   controls. Identify which agreements a human may actually cancel; do not add a
   universal cancellation command for fixed-duration treaties.
2. Give permitted active agreements stable references and report native endings
   with their reason and actual effects. Keep these separate from withdrawing an
   unaccepted proposal and from the wall-clock negotiation deadline.
3. Expose validated cancellation only where the native UI permits it, sharing any
   extracted engine operation with that UI. Deliver the appropriate events to each
   affected player and flag relevant private plans for review.
4. Verify durations through scheduled turns, resource loss, declarations of war,
   restart, exact retries, interrupted checkpoint writes and coordinated rollback.
   Include paired hidden-world checks and comparison with native UI operations.

Keep city-states, mod-only introductions, three-way consent for mediated peace,
embedded desktop play and operating-system isolation in the wider backlog.

## Completion gates

Automated regression coverage, a live model exercise and visual desktop acceptance
are distinct results. Record what was actually run in the slice inventory. A passing
slice does not establish a completed duel, unrestricted settings, or full API parity.
