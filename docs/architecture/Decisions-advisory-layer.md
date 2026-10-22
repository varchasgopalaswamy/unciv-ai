# Decisions advisories

`agents.decisions_enabled=false` is the default. The single switch controls
diplomacy scheduling, optional memory rankings, preservation candidates, belief
notices, early strategic-review triage and city focus/construction questions. Native gameplay recommendations are
controlled independently by `native_advice_enabled`. They go directly to the
gameplay model through `native_advice`, with original commands, costs,
prerequisites, conflicts and limitations. Separate city questions compare legal
native menus against saved objectives; they do not filter or replace the native
advisor's suggestions.
Retired `diplomacy_routing_enabled` settings and CLI flags are rejected explicitly.

The service never adds gameplay restrictions. All advice can be rejected without
an override. Native legality and player-visible information remain authoritative.
Decisions does not authorize disclosure, interpret social promises as rules,
adjudicate breaches or impose penalties. Explicit notebook writes keep their
existing validation and lifecycle; classification never filters those writes.

## Execution and budget authority

The Decisions API has selectable `luna` and `jev` backends. Jev uses local
Winnow-12B and is the default. Both adapters normalize their complete answer
probability vectors before acceptance. `decisions_acceptance_probability`
defaults to 0.95 for Luna and 0.98 for Jev; explicit routing probability overrides
inherit this value when omitted. The internal `confidence` compatibility field
contains selected-answer probability. Native confidence remains separately in
`providerConfidence`; an entropy or margin score cannot authorize acceptance.
These are conditional probabilities over supplied alternatives, not correctness
guarantees. Old confidence-named configuration controls are rejected.

The trusted broker alone chooses the model, task instructions, question types,
choices, policy versions, acceptance threshold, limits and prices. Clients send
frozen memory/review/city evidence to `/batch`; unknown top-level controls and
arbitrary gameplay tasks are rejected. Every request identifies its player, which
must match the authenticated principal. The task policy is
`player-advisories-v4`, broker protocol 3; routing uses `diplomacy-routing-v4`.
These are the only supported versions. Enabling Decisions selects the current
implementation; old policies, response adapters and settings migrations are
absent. Health checks authenticate and validate exact current versions without
calling the provider. A stale image is rejected with a rebuild instruction.

The operator registers each immutable container game ID and assigned player
against a broker principal. Credential rotation updates its token hash without
creating a new account. Settings are registered by the supervisor, never supplied
as spending controls by player services. The default additional allowance is
$0.10 per player-game, shared by all advisory batches and early-review triage.
Diplomacy routing and full strategic-review model jobs are accounted separately.
For Luna, zero allowance skips new advisory calls while preserving deterministic selection,
independent native advice and periodic/war-triggered reviews.
Jev calls remain enabled at zero allowance. Jev needs no API key, and records
usage and durable request identities without charging or reserving dollars.

`AUTH_DIR/decisions.sqlite3` owns reservations and validated input usage outside
game checkpoints. A process-shared initialization lock serializes WAL setup and
authority-file publication. SQLite `BEGIN IMMEDIATE` atomically reserves across
requests and processes before dispatch. Stable evidence-and-model-derived request IDs deduplicate
replayed requests. Validated input usage settles reservations; missing or invalid
usage retains them. Completed results are stored before replying. A lost or
unverifiable reply permits at most one free `GET /batch/{request_id}` receipt
lookup per reachable cached attempt. Lookup cannot call the provider or reserve
funds. Only an explicit `not_submitted` receipt permits resubmitting the same
immutable request through ledger deduplication. An unknown completion never
becomes a new paid retry. A restart or rewind cannot refund
spending or pending reservations. The game manifest pins the broker authority;
resume rejects an auth-directory change, and an authority/ledger mismatch or
missing ledger fails closed.

The protocol's accounting contract is `decisions-backends-v1`. Each request
pins its own backend rate at reservation and keeps that rate through settlement.
Jev uses `decisions-local-free-v1` at zero cost. Luna rate version
`decisions-standard-2026-10-14-v1` uses the verified standard
[$0.10 per million input tokens, with no output charge](https://developers.openai.com/api/docs/guides/decisions#pricing-and-availability).
The estimate counts every serialized UTF-8 byte as one token, adds 2,048 tokens
of framing headroom and 128 per question, then reserves integer nanodollars.
This is a **local estimated spending ceiling**, not an invoice guarantee;
regional processing premiums, long-context multipliers and provider tokenization
can differ. The broker does not accept client prices, exemptions or identities.

The ledger retains dispatched `replayEvidence`, model, policy version, input and
question-prompt hashes, submitted question names and typed answer diagnostics.
Original evidence remains in the trusted ledger rather than being copied into
player replies or telemetry. The runtime verifies request, model, policy, input,
prompt and question identities and reparses diagnostics before using an answer.
Historical captures from an older policy cannot be treated as current results.

Each submitted question has a `questionResults` row with status `accepted`,
`low_confidence`, `refusal`, `missing` or `malformed`. Sanitized typed choices and
refusals remain in `rawAnswers`; normalized choices retain selected probability,
native provider confidence and complete probability vectors. Only independently accepted choices
enter `answers`. A batch fallback summarizes the first failure without discarding
unrelated valid answers. An invalid named answer is isolated when possible;
an unassignable malformed envelope can reject the batch. The runtime preserves
the difference between a refusal and a low-confidence choice.

## Startup and delivery

Startup reads ordinary permitted context and action details. When native advice
is enabled, its normal player/epoch/revision-bound read is delivered directly
and recorded as exposed advice. Its alternatives retain the normal startup
bound of eight, with omission counts; explicit reads can retrieve the remainder.
Native reads and gameplay commands never initiate Decisions classification.

Independent tasks share one eligible startup batch: optional-memory inclusion
and priority, statement preservation, belief reconsideration and four questions
per captured city (whether to change focus/construction and which alternative). Each task sees
frozen inputs and does not depend on another task's result. Internal classifier
access never counts as delivering a memory or exposing gameplay advice.

Material eligibility ignores transport IDs, stockpiles and routine counters.
Memory triage and city capture have separate attempt clocks using the configured
startup gap, five turns by default. A communication activation cannot consume
the next eligible gameplay city's attempt. Memory selection needs, new permitted
sources and changed city context determine the questions within an eligible
batch. Capture failures also consume their bounded city attempt.

Attempts are persisted before network I/O. Source outcomes and optional rankings
are cached against source, objective and topic context; belief attempts also
include the recorded new evidence. Completed low-confidence, refused or failed
stable inputs do not cause repeated paid calls. A changed relevance context can
make the same source eligible for reconsideration. Overlarge inputs omit whole
optional candidates and report coverage; invalid or unusable batches leave
deterministic memory selection available. Refusal, low confidence, timeout,
cooldown and exhaustion leave direct native gameplay advice unchanged.

Private delivery bindings retain the original request/question and source
versions or native city basis. After explicit startup memory reads, separate
`advisory_delivery` events record context, notice or city-advice exposure. Review
scheduling has its own exposure acknowledgment when a review task is prepared.
Dispositions distinguish `delivered`, `no_notice`, `not_selected`, `stale` and
`not_accepted`. Cached memory advice can be exposed in a later activation when it
fits the context budget. Keep/defer city advice counts as exposed advice, never
as an executed action. These records stay outside model-facing notices, carry no
source prose or token usage, and do not add classifier calls or spending.
Acceptance, exposure, model adoption and gameplay effects are separate outcomes.

## City choices

At eligible gameplay startup with native `phase="ACTIVE"`, the service captures
up to four manageable owned cities, prioritizing empty queues. Each has an independent
change question and hypothetical alternative question for focus and construction. They use the same pinned
player/epoch/revision/turn, shared empire facts,
and saved objectives. They have independent confidence/fallback handling and do
not assume the other's answer. A recommendation to change requires both an accepted
change decision and an accepted alternative; a confident no-change answer retains
the current assignments or queue. Adopted player decisions are distinguished from
the latest optional review. The gameplay model coordinates the recommendations
and recomputes construction estimates after changing focus. Keep and defer are
explicit alternatives; no recommendation is executed automatically. An active
position can remain eligible when mandatory choices make `canEndTurn=false`;
native availability checks still determine the offered choices.

`player_inspect(subject="CITY_FOCUS", target=...)` computes native allocations on
a fresh detached world for each available focus. It preserves growth settings,
tile locks and manual specialist assignments, and reports displayed yields, food,
growth/starvation, current-construction timing and assignments. It never changes
the live city or advances a turn. It is also available as an ordinary explicit
inspection. Future growth, improvements, enemies and combined construction/focus
effects are excluded.

Construction evidence comes from the same permitted snapshot: up to eight
queue/production choices with native commands, costs, retained work, prerequisites,
resource requirements and completion effects. A bounded shortlist uses newly
available choices and objective/name relevance. Counts explicitly report omitted
cities, constructions and focuses (at most 16 captured focus alternatives).
Append and reorder alternatives identify their exact native operation and queue
entry. Append production excludes work belonging to an already queued copy;
start-now time is distinguished from unknown delay behind earlier entries.
Reordering follows native construction-name storage rules. Shared byte/question
limits remove whole city groups and their unneeded rule text
when needed. The player's ordinary menus and inspections remain available.

Internal previews and projections do not register explicit detail reads or native
advisor exposures. Only accepted guidance is included in activation context and
saved in its snapshot. The agent reads the normal selected-option inspection
before acting. A fresh native read checks the basis again before delivery; stale
results are discarded. Startup replay never initiates another paid request.

Stable context reuses cached answers against freshly captured native options.
Transport fields, treasury increments and ordinary progress counters do not force
reclassification; construction/growth estimates use meaningful timing bands for
eligibility. Reused answers retain their original request identity; delivery uses
fresh native bindings and checks the complete active-objective set. Failed stable
inputs remain cached. With no usable answer, normal gameplay and independently
enabled native advice continue. These questions share the additional allowance
and their own city attempt clock. Status exposes internal
capture counts separately from delivered cities/questions.

## Memory

The shortlist includes old active beliefs/notes from the player's dispatch
snapshot. Relevance uses objectives, topics, subjects, text overlap and saved
priority, without a hard age limit. Retired, expired and discarded-future records
are excluded; current-head/version validation prevents superseded candidates
from being applied. Strategy, eligible handoff and contribution handling stays
deterministic. Optional rankings fit the existing `memory_context_chars` budget,
preserve original wording/dates/uncertainty/citations and pass through ordinary
explicit `memory_read` delivery. Classifier access creates no delivered evidence.

Retrieval has two independent binary questions per candidate.
`retrieval_include:i` chooses `include` or `omit` for present usefulness;
`retrieval_priority:i` chooses `high` or `normal`, assuming the memory is shown.
Accepted inclusion can make an older optional note or belief eligible. Priority
changes ordering among eligible memories and cannot grant eligibility alone.
Omission applies to optional memory; pending `needsReview` notices retain their
ordinary eligibility. Mandatory strategies and eligible handoffs keep their
deterministic handling, so accepted inclusion need not fit the final character
budget. Later exposure still binds to the original source version and request.

Preservation presents source-linked attributed statements and unresolved
questions at a normal activation. It never creates an activation or saves memory
on the player's behalf. Belief notices carry the belief version and new source
versions; they never change claims, uncertainty, citations or lifecycle. New
evidence is restricted to attributed statements and recorded material facts;
classifier results, guidance observations and notices cannot seed cascades.
Candidates/notices are separate activation data. The player can read their
sources before choosing explicit notebook edits.

Attributed sources preserve author, audience, source turn, exact text and
uncertainty. Chronological bounds use source turn and event sequence, rather than
lexical record IDs. A preservation decision does not establish that a statement
has already been compared with every active belief. Belief reconsideration uses
the claim's exact polarity and uncertainty, recorded overview comparisons and
attributed new statements. Projection coverage reports missing fields and bounded
rosters; absence or truncation is not proof of contradiction. Accepted `skip` and
`retain` answers produce no positive notice and never revise memory.

## Early reviews

Full strategic reports receive source labels on their starting observations and
every frozen tool result actually delivered to that reviewer. Material assumptions
cite those labels and remain inferred or unknown. Validation binds citations to
the exact delivered payload and date; it does not prove the interpretation.
The compact named-fact catalogue is a convenience, not a whitelist of admissible
evidence. Population and partial map sightings never establish a foreign empire's
total city count. Cited payloads remain in the private report archive, while dated
references such as `review:20:1` can be retained in adopted strategy. Planning
guidance traces investments through completion, travel, prerequisites and the
first useful action, including civilian exposure.

Invalid reports receive actionable feedback and at most one correction. An
unusable report after that becomes `UNAVAILABLE`; the shared phase can release
without new advice. Earlier reports retain their dates, and an unavailable
review does not replace the last completed material-facts baseline. Input
preparation, persistence and unconfirmed shutdown or history drain remain fatal
to the phase. See the [runtime guide](../guides/Native-guidance-and-routing.md#review-evidence-and-failures)
for output guards and failure distinctions.

Before selecting participants, the scheduler requests triage only for players
with Decisions enabled and no scheduled review due now or next round. Triage
uses previously recorded permitted material facts and saved reconsideration
criteria, without opening a player connection or capturing a full review.
Each completed strategic review atomically commits a bounded material-facts
baseline, full fingerprint, schema and source identity alongside its report.
Triage compares that baseline with current recorded facts, explicit deltas,
`recentProgress` and relevant attributed statements. Triage requires the valid
committed baseline; missing, invalid or hash-only baselines cause no request.
A regular completed review supplies the baseline. Incomplete comparisons remain
explicit.

The scheduler supplies authoritative `nextReviewTurn` in completed-round units.
`sourceTurn`, `completedTurn` and `boundaryTurn` distinguish observation age from
the upcoming boundary. The binary question asks whether an additional full
review is worth its cost before that scheduled review; routine increments alone
are insufficient. Acceptance needs meaningful changed evidence, configured
confidence, three rounds since any completed review and ten since the last
scheduled early review. An unchanged attempted signature does not create another
paid call.

The original request and its evidence remain immutable during receipt recovery
or cache reuse. If a newer observation has the same material context, a separate
current delivery binding revalidates source versions, baseline identity and the
complete active-objective set. A different semantic context cannot be rebound
to the old judgment. Review exposure also requires the accepted request and
current binding to match the actual prepared review turn. Duplicate scheduling
acknowledgments are deduplicated by original request and turn.

Accepted requests join the same communication-draining hold, host barrier,
parallel review and release workflow. Persisted participant metadata lets an
interrupted barrier resume without reclassification. Overlapping periodic and
early reviews coalesce. Every completed participant review resets its periodic
anchor, avoiding another interval review shortly after an early one. Mandatory
war triggers retain their behavior. Private
criteria go to triage only, never to the independent strategic reviewer.

## Validation and operations

See the [operating guide](../guides/Native-guidance-and-routing.md) and
[trial configuration](../../examples/native-routing/trial.toml). Read-only game
status separates direct native advice exposure, broker spending/reservations,
diplomacy classifier usage and full review usage.
The broker ledger is the spending authority; checkpoint metadata is diagnostic.

The labeled [advisory replay fixture](../../tests/fixtures/decisions-advisory-replay.json)
contains provenance-labeled cases across the six advisory tasks.
These are provisional assistant-authored judgments over synthetic evidence using
current production schemas, including negative, missing-coverage and boundary
cases. One explicitly labeled schema/parser stress case combines startup and
review fields that runtime emits separately. Historical fixtures and captured
responses remain historical artifacts; they cannot be relabeled as current
quality evidence.

Replay checks policy, input, prompt, model and request identity, then reports raw
choices, accepted answers and observed delivery separately. Confidence/status
denominators remain per question and task. Threshold sweeps use calibration
cases only and do not select a threshold automatically; holdout results stay
separate. Offline replay with no responses reports missing data, not accuracy.
The current implementation has no paid current-policy advisory-quality comparison or gameplay
benefit evidence. Synthetic classifier-response tests validate contracts and
accounting, rather than stronger play.

Tests cover direct native delivery, independent toggles, unrestricted legal
choices, explicit memory reads, non-recursive notices, barrier/anchor behavior,
concurrent reservations, lost replies, bounded free receipt recovery, partial
answers, raw diagnostics, cached later exposure, restarts, rewinds, token
rotation and pinned authorities.
