# Background strategic reviews and turn accounting

Gameplay ends at `END_TURN`. There is no mandatory model-written handoff,
post-turn memory phase, or requirement to rewrite a strategy before acting.
The host records commands, reasons, receipts, diplomacy and accounting itself.
During gameplay, players can still save private information that observations
and action records cannot reconstruct.

## Review schedule

Each civilization receives an opening review before its first move on turn **0**.
These reviews run concurrently, subject to the shared job limits. Each uses only
that civilization's revealed starting area, units and ability profile, plus
permitted planning information. Its first gameplay turn waits for its own report.

`--review-interval K` defaults to **5**. Subsequent reviews are prepared at the end
of turns K, 2K, 3K, and so on. The launcher retains the interval on resume.
Independent agent servers accept the same option.

A civilization has at most one pending review. Other civilizations continue while
it runs, but that civilization's next gameplay turn waits for the report. For
example, Greece and France can play while Rome reviews turn 5; Rome cannot start
turn 6 until that review is saved.

The scheduler waits before capturing the next turn checkpoint or creating a
gameplay activation. The wait uses no gameplay model time or activation budget;
the match wall-time budget still applies. It keeps the operator lease alive,
checks service health, and responds to pause, stop and budget limits. The console
and dashboard identify the waiting civilization and review turn. The service
also rejects a direct gameplay start while a required review is unfinished.

## Frozen input and publication

During initial player connection, before gameplay starts, and before each due
`END_TURN`, the service saves:

- The player's permitted position, with its detailed city, unit and explored-map data.
- All permitted victory sections and chart-history pages, plus research/policy
  catalogues including named planning details.
- The player's diplomatic context and received messages.
- Recent confirmed actions and outcomes. Strategic rationales remain in the
  audit history and are excluded from the review inputs.
- The original model, reasoning effort and timeout settings.

Capturing data is service work, not another model request. Opening inputs become
pending immediately, with no preceding gameplay job or action history. Reconnecting
reuses pending inputs or a completed report. For periodic reviews, a rejected
handoff cannot launch a review. A successful native handoff, or its reconciled
receipt, marks the saved task pending. The next player may then act while the
reviewer runs.

The reviewer receives its own conversation and credential, using the player's
model, reasoning effort, fast-priority setting and optional strategy guide. Its
context excludes private strategy, handoffs, contributions and previous model
reasoning. Frozen detail tools have no live host connection. The reviewer cannot
act, communicate, write memory, inspect private history or request live movement
previews. Current tactical validation remains the gameplay player's responsibility.
The initial prompt includes current diplomatic commitments; the older ledger and
duplicated economic context stay behind explicit frozen reads.

The preload also summarizes known foreign cities from that frozen map: owner,
position, original-major-capital status, last-observed turn and distance in hexes
to the nearest own city. Visible cities include population, health and native
defensive strength. Under fog, identity and ownership remain last-seen facts;
current population and defenses are omitted. Unexplored cities are absent.
Capital identity comes from observed city facts, not matching city names.

Frozen `victory_progress` accepts the same section, victory, metric, civilization
and chart-range filters as gameplay. The service captures all permitted chart
pages before handoff; subsequent reads filter that snapshot without contacting
the advancing game. Native visibility, missing values, ranking scope and metric
order are preserved. The initial review preload includes own/global status and
rankings; chart history and demographics remain on demand to limit context size.

Reports compare enabled victory paths and propose short-, mid- and long-term
plans over the same horizons. Each victory path names a next concrete objective,
the investment required and expected payoff for the player and rivals, alongside
remaining requirements, a projection and observed rival progress. Detail matches
the game stage and evidence. Field descriptions identify the report's contents; the
reviewer chooses its calculations and strategic conclusions. Projections can
describe development milestones and conditions instead of a victory date. The
service validates the report's structure and coverage.

The latest completed report is selected when a subsequent gameplay activation
starts, with `asOfTurn`. It cannot change midway through that conversation or
overwrite the player's notebook. The player weighs it against current facts and
need not agree with it or copy it into another memory entry. Opening advice is
available at turn 0; periodic advice is available on later turns.

## Recovery and failures

Review inputs commit before the native handoff. Inputs and the pending task are
part of coordinated turn recovery. If a review finishes after a checkpoint and
the game rewinds to that checkpoint, its report is discarded and the captured
inputs become pending again. Reports and temporary inputs change atomically;
inputs are removed only after the report is saved.

A restarted reviewer uses a fresh conversation and the same frozen inputs.
`--review-attempts` defaults to 3. Attempts and spending survive a rewind;
exhaustion is exposed through service health and stops the scheduler for
inspection. Explicitly increasing the limit permits further attempts on resume.
Pausing stops model processes and keeps pending inputs without launching retries.
Persistence failures and unconfirmed process shutdown stop immediately; another
reviewer is not launched while the previous process might still be running.

Checkpoint holds drain admitted work and briefly defer report publication, while
model computation can continue. The scheduler releases those holds before waiting
for a report. Restoration closes abandoned reviewer processes and credentials
before replacing state. A restored pending review must finish before that
civilization's next gameplay activation.

## Mechanical completion and telemetry

After `END_TURN`, gameplay tools are fenced. The conversation finishes immediately;
the service allows up to 60 seconds for provider completion, final usage and
process shutdown. This is not a second thinking or writing budget. The scheduler
can start the next player while that completion drains.

`finish_handoff` accepts an activation-specific native handoff proof.
`begin_finalize` performs durable completion/accounting asynchronously, with
idempotent retries for lost acknowledgements. A civilization's next activation
waits for this mechanical completion and any pending strategic review.

Reviews are separate `review` jobs. Periodic reviews link to the original gameplay
job; opening reviews have no parent and count toward their owner's turn 0.
All attempts' tokens, calls and durations appear in host telemetry and the
source turn's totals; player totals also separate `reviewUsage`. Final provider counters
can arrive after handoff, so accounting may initially be incomplete. Concurrent
job durations must not be added to estimate game wall time.

SQLite remains authoritative. Gameplay and background completion do not generate
Markdown or per-turn exports. Human-readable history is generated on request.
The dashboard's Strategy tab reads the latest completed reports directly from
the local review stores, alongside pending-review status and host job telemetry.
It shows both the source turn and the first gameplay preload that received the
advice, without implying that the player adopted it.
See [telemetry](../reference/Telemetry.md), [private memory](Structured-memory.md)
and [turn recovery](Turn-recovery.md).

## Validation

Tests cover concurrent opening reviews, private starting views, turn-zero usage,
cadence, read-only frozen tools, other-player overlap, the next own
turn waiting and receiving the report, model/effort/priority settings, failed and
cancelled reviews, bounded attempts, checkpoint restoration, lost handoff
responses and delayed usage. Waiting is tested beyond the gameplay timeout,
including lease renewal, pause/resume and fatal review failures.
They use fake model processes; no paid model is needed for lifecycle validation.
