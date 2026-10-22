# Shared strategic reviews and turn accounting

Gameplay ends at `END_TURN`. There is no mandatory model-written handoff,
post-turn memory phase, or requirement to rewrite a strategy before acting.
The host records commands, reasons, receipts, diplomacy and accounting itself.
During gameplay, players can still save private information that observations
and action records cannot reconstruct.

## Review schedule

Each civilization receives an opening review before its first move on turn **0**.
These reviews run concurrently, subject to the shared job limits. Each uses only
that civilization's revealed starting area, units and ability profile, plus
permitted planning information. Gameplay waits until each opening review has
published advice or safely finished as unavailable.

`--review-interval K` defaults to **5**. Subsequent reviews are prepared after all
players finish rounds K, 2K, 3K, and so on, including built-in AI processing.
The launcher retains the interval on resume.
Independent agent servers accept the same option.

A civilization has at most one pending review. For example, Rome, Greece and
France all finish turn 5 before any turn-5 review starts. Existing conversations
finish, new communication jobs are held, and the scheduler freezes gameplay and
communication. The services then capture fresh permitted views at the turn-6
boundary and run their reviews concurrently. Each review must publish advice or
safely finish as unavailable before play or communication resumes. Each report's
`asOfTurn` names the completed round.

Humans may submit plain chat during this pause. The host checkpoints it in a
sender-private queue, outside all recipient streams and review inputs. Releasing
the phase delivers those messages once, before normal conversations resume.
Trade, social commitments and native diplomatic actions still wait for release.

The scheduler waits before capturing the next turn checkpoint or creating a
gameplay activation. The wait uses no gameplay model time or activation budget;
the match wall-time budget still applies. It keeps the operator lease alive,
checks service health, and responds to pause, stop and budget limits. The console
and dashboard show the shared phase and remaining reviewers. A service rejects
gameplay until the scheduler releases the phase, even if its own report is ready.

## Frozen input and publication

During the shared pause, before opening play or after a due round, the service saves:

- The player's permitted position, with its detailed city, unit and explored-map data.
- All permitted victory sections and chart-history pages, plus research/policy
  catalogues including named planning details.
- The player's diplomatic context and received messages.
- Recent confirmed actions and outcomes. Strategic rationales remain in the
  audit history and are excluded from the review inputs.
- The original model, reasoning effort and timeout settings.

Action history uses the recorded API to distinguish native commands from social
operations. Native successes use their effect summaries; social offers and revisions
are identified as proposals, without implying acceptance or native peace effects.

Capturing data is service work, not another model request. Opening inputs become
pending immediately, with no preceding gameplay job or action history. Reconnecting
reuses pending inputs or the recorded review outcome. Periodic reviews require a confirmed
gameplay handoff for that civilization in the completed round. No review is
captured by `END_TURN`. Late message arrivals are included directly from the host
without advancing the paused inbox listener's cursor.

The reviewer receives its own conversation and credential, using the player's
model, reasoning effort, fast-priority setting and optional strategy guide. Its
context excludes prior strategy assessments, handoffs, contributions and previous
model reasoning. Selected objectives and expectations arrive as dated hypotheses
in factual `decisionFeedback`. Frozen detail tools have no live host connection. The reviewer cannot
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
pages at the shared boundary; subsequent reads filter that snapshot. Native
visibility, missing values, ranking scope and metric
order are preserved. The initial review preload includes own/global status and
rankings; chart history and demographics remain on demand to limit context size.

The frozen `player_turn` also contains two compact review-only summaries:

- `developmentSincePreviousReview` compares the last completed review's observed
  position with this one. It reports science, culture, production and net gold
  per turn, population, cities, happiness and military counts, plus acquired
  technologies, added buildings and changes in city/unit ownership or unit type.
  These are endpoint comparisons, not interval averages or inferred causes:
  additions may be purchases, gifts or captures, and units no longer owned are
  not automatically counted as combat losses. Recent action outcomes provide
  the available intervening history. Each change list is bounded to 16 entries
  with omitted counts. The opening review has no earlier position to compare.
- `upcomingEvents` lists up to 16 events within the next ten turns, ordered by
  their native remaining times: current research and unlocks, active non-wonder
  construction and its current-city benefits, first growth/starvation, individual
  trade-term expiries, peace-treaty expiry and city-state relationship changes.
  It retains current happiness alongside growth and building estimates without
  adding independent scenarios into a projected budget. Later queue entries are
  excluded because their individual estimates assume immediate starts. Growth
  suppressed by avoid-growth and city-state zero/no-change timers are excluded.
  Research/production and influence estimates remain conditional on current rates;
  trade renewal, gifts or other players can change the dates. Wonder estimates
  remain exclusively under `wonderOpportunities`.

Both summaries use facts already captured for the review and add no game API or
model requests. Rival comparisons continue to use the permitted individual
rankings and trends; no hidden yields or total score are added. These summaries
do not change the review cadence. The last completed review's small factual
baseline is committed atomically with its report in the existing SQLite metadata;
failed attempts cannot advance it. Turn recovery restores
the matching baseline, and the full review inputs are still deleted on completion.

## Reports and diplomatic advice

Starting observations and frozen tool results carry source labels belonging to
that reviewer. Material assumptions cite any of those delivered sources and
distinguish inference from unknown premises. Validation checks provenance rather
than certifying the interpretation or limiting evidence to the named-fact summary.
Cited payloads are retained once in the private report archive; later advice and
adopted memory keep compact dated references.

Reviews compare up to three concrete `plans`, each with an objective, required
investment, expected observations, first actions, relative outcome and risks.
They compare current choices, preparation, opportunity costs and rival responses
over one `horizonTurns` (1–100). `preferredPlan` is a one-based number or null when the choice is open;
`reconsiderWhen` identifies evidence that would change the recommendation. An
undecided review can recommend information gathering or reversible investments.
`outcomeAssessment` interprets previous expectations against observed changes
and linked receipts, retaining uncertainty about causes and missing evidence.
The schema validates structure and plan references, not a preferred strategy.
Enabled victory rules remain readable; exhaustive `victoryOutlooks` and the
overlapping short-/mid-/long-term sections are removed. Runtime format 11 requires
new stores; read-only dashboard rendering supports retained baseline reports.

Both gameplay and reviews receive `outputSummary` from the same owned-position
projection, including science contributed by production conversion. The remainder
can still include temporary bonuses. Focused `construction_details` with selected
`names` and `horizonTurns` compares continuing current output, building and
purchasing. It uses native rates, costs, blockers and completion previews, with
static timing assumptions and no aggregate utility score. Missing previews stay
unavailable. Purchase costs are separate; completion outputs exclude conversion,
and the helper does not assume subsequent builds, resumed conversion, growth or
future specialist staffing.

The report's `diplomacy` section contains an assessment of relationships and
agreements and zero to two proposed initiatives. Each references its supporting
plan and names the players to
approach, the desired change in their behavior and our relative benefit, their
incentives, proposed terms and disclosures, a concrete next step and conditions
for reconsideration. The overall report targets 6,000 characters. Gameplay and
communication receive a single compact report including the alternatives, and
the Strategy tab renders all report fields. The host does not require a particular
victory, military posture or use of deception.

The reviewer considers how influencing another player could improve a plan or
make it feasible. Initiatives remain advice, with an empty list valid. Gameplay
chooses whether and how to initiate contact; subsequent communication jobs can
negotiate, seek information or contact another player. Built-in AI receives native
actions and trades, while human and agent players can negotiate freely in chat.
Social promises remain nonbinding, and trade/peace terms use the trade flow.

Gameplay considers relevant initiatives against current facts and may record
`acted`, `deferred` or `rejected` in the existing `END_TURN` call's optional
`initiativeDecisions`. Each entry identifies a one-based initiative number, with
a reason. The service supplies the date of the delivered report and validates
the initiative number. Deferral explains when or under what conditions
to revisit it. These private decisions appear in action history, cost no additional
model call, and are stripped before native dispatch. They do not require following
the review, contacting another player or writing a notebook entry. References to
unavailable initiative numbers produce feedback without blocking END_TURN.

Both kinds of player job receive the latest report and use initiative participants
to select relevant private notes within the existing memory budget. Private advice
is not sent automatically. Communication receives gameplay-owned decisions as
part of the strategy and uses them to judge which negotiations matter. Reviewer
inputs include dated chosen expectations, while excluding the player's persuasive
strategy and action rationales. Earlier choices are hypotheses to reconsider.

Detail matches the game stage and evidence. The reviewer chooses its calculations
and strategic conclusions; projections can describe development milestones and
conditions instead of a victory date. The service validates structure, plan
references and victory coverage, not strategic preferences.

The latest completed report is selected when a subsequent gameplay activation
starts, with `asOfTurn`. It cannot change midway through that conversation or
overwrite the player's notebook. The player weighs it against current facts and
need not agree with it or copy it into another memory entry. Opening advice is
available at turn 0; periodic advice is available on later turns.

## Recovery and failures

Review inputs commit at the shared boundary. A stop during review preserves
completed reports and unfinished inputs; resume restarts only unfinished reviews.
The scheduler records the reviewed boundary before taking the next gameplay
checkpoint. An interrupted subsequent turn therefore rewinds to a position that
already includes the reports. Reports and temporary inputs change atomically;
inputs are removed only after the report is saved.

A restarted reviewer uses a fresh conversation and the same frozen inputs.
An invalid report gets actionable feedback and at most one correction, normally
in the same conversation with the observations already read. `--review-attempts 1`
disables correction; larger values do not expand the two-attempt cap. Attempts
and spending survive a rewind. Exhaustion marks the review `UNAVAILABLE`, retains
failed inputs and diagnostics, and lets the shared phase release when otherwise
safe. Later player context exposes that status; any earlier completed report
keeps its original date and the completed-review baseline is not advanced.
Pausing stops model processes and keeps pending inputs without launching retries.
Prompt and output-schema preparation failures stop immediately as
`REVIEW_PROMPT_FAILED`, retaining their exception causes in diagnostics. They are
distinct from persistence failures (`REVIEW_RECORD_FAILED`). Frozen diplomacy
reads use the stored model-facing `proposalId` field when selecting current
commitments and consents; the native communication boundary uses `proposal`.
Persistence failures and unconfirmed process shutdown stop immediately; another
reviewer is not launched while the previous process might still be running.
Structured report output also has size and consecutive-whitespace guards.
Accepted stream fragments are combined without changing text or crossing usage,
tool or lifecycle events, and the notification queue remains bounded. Confirming
process exit is separate from draining accepted history/accounting events:
`STOP_UNCONFIRMED` identifies the former, while `REVIEW_HISTORY_DRAIN_FAILED`
identifies the latter. A failed drain remains owned by the service and stops the
phase rather than publishing advice or starting another reviewer. See the
[runtime guide](../guides/Native-guidance-and-routing.md#review-evidence-and-failures)
for bounds and diagnostics.

The shared phase permits frozen review reads and report publication while keeping
ordinary gateway calls fenced. MCP initialization, tool discovery and tool calls
share this admission rule; blocking reviewer initialization would deadlock
`thread/start` before the model runs. A recovery checkpoint also holds review publication.
Restoration closes abandoned reviewer processes and credentials before replacing
state. A restored pending review must publish or safely become unavailable before
the shared phase is released.

## Mechanical completion and telemetry

After a successful `END_TURN`, the gateway saves the receipt and fences gameplay
tools. The service immediately requests inference interruption instead of waiting
for a closing model response. It accepts final usage notifications, confirms
process shutdown and reconciles provider usage records. The committed game turn
is completed even when the provider conversation ends as interrupted. Usage
remains incomplete if interruption leaves no authoritative accounting record.
The scheduler can start the next player while this accounting finishes.

`finish_handoff` accepts an activation-specific native handoff proof.
`begin_finalize` performs durable completion/accounting asynchronously, with
idempotent retries for lost acknowledgements. A civilization's next activation
waits for its mechanical completion. Shared review phases wait for all completed
turns' accounting before capturing inputs.

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
including lease renewal, pause/resume, unavailable advice and fatal lifecycle failures.
They use fake model processes; no paid model is needed for lifecycle validation.
