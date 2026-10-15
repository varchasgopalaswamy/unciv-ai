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

The trusted broker alone chooses the model, task instructions, question types,
choices, policy versions, confidence threshold, limits and prices. Clients send
frozen memory/review/city evidence to `/batch`; unknown top-level controls and
arbitrary gameplay tasks are rejected. The versioned task policy is `player-advisories-v2`,
broker protocol 2. Health checks authenticate locally and make no provider calls. There are no provider retries.

The operator registers each immutable container game ID and assigned player
against a broker principal. Credential rotation updates its token hash without
creating a new account. Settings are registered by the supervisor, never supplied
as spending controls by player services. The default additional allowance is
$0.10 per player-game, shared by all advisory batches and early-review triage.
Diplomacy routing and full strategic-review model jobs are accounted separately.
Zero allowance skips new advisory calls while preserving deterministic selection,
independent native advice and periodic/war-triggered reviews.

`AUTH_DIR/decisions.sqlite3` owns reservations and validated input usage outside
game checkpoints. A process-shared initialization lock serializes WAL setup and
authority-file publication. SQLite `BEGIN IMMEDIATE` atomically reserves across
requests and processes before dispatch. Stable evidence-derived request IDs deduplicate
replayed requests. Validated input usage settles reservations; missing or invalid
usage retains them. Pending work after a lost reply/crash is never redispatched.
Completed results are stored before replying. A restart or rewind cannot refund
spending or pending reservations. The game manifest pins the broker authority;
resume rejects an auth-directory change, and an authority/ledger mismatch or
missing ledger fails closed.

Rate version `decisions-standard-2026-10-14-v1` uses the verified standard
[$0.10 per million input tokens, with no output charge](https://developers.openai.com/api/docs/guides/decisions#pricing-and-availability).
The estimate counts every serialized UTF-8 byte as one token, adds 2,048 tokens
of framing headroom and 128 per question, then reserves integer nanodollars.
This is a **local estimated spending ceiling**, not an invoice guarantee;
regional processing premiums, long-context multipliers and provider tokenization
can differ. The broker does not accept client prices, exemptions or identities.

## Startup and delivery

Startup reads ordinary permitted context and action details. When native advice
is enabled, its normal player/epoch/revision-bound read is delivered directly
and recorded as exposed advice. Its alternatives retain the normal startup
bound of eight, with omission counts; explicit reads can retrieve the remainder.
Native reads and gameplay commands never initiate Decisions classification.

Independent tasks share one eligible startup batch: optional-memory
relevance, statement preservation, belief reconsideration and two questions per
captured city (population focus and construction). Each task sees
frozen inputs and does not depend on another task's result. Internal classifier
access never counts as delivering a memory or exposing gameplay advice.

Material eligibility ignores transport IDs, stockpiles and routine counters.
Startup batches require the configured turn gap and a meaningful memory
selection need, new permitted evidence for preservation/belief reconsideration,
or changed city-choice context.
Attempts, including failures, are cached before network I/O. Optional-memory
rankings have their own objective/topic/candidate-version key. Stable failed
inputs are not repeatedly submitted. Overlarge/invalid batches use deterministic
memory selection. Refusal, low confidence, timeout, cooldown and exhaustion
leave direct native gameplay advice unchanged.

## City choices

At eligible gameplay startup, the service captures up to four manageable owned
cities, prioritizing empty queues. Each has one focus question and one construction
question. Both use the same pinned player/epoch/revision/turn, shared empire facts,
and saved objectives. They have independent confidence/fallback handling and do
not assume the other's answer. The gameplay model coordinates the recommendations
and recomputes construction estimates after changing focus. Keep and defer are
explicit alternatives; no recommendation is executed automatically.

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
Shared byte/question limits remove whole city pairs and their unneeded rule text
when needed. The player's ordinary menus and inspections remain available.

Internal previews and projections do not register explicit detail reads or native
advisor exposures. Only accepted guidance is included in activation context and
saved in its snapshot. The agent reads the normal selected-option inspection
before acting. A fresh native read checks the basis again before delivery; stale
results are discarded. Startup replay never initiates another paid request.

Stable context reuses cached answers against freshly captured native options.
Transport fields, treasury increments and ordinary progress counters do not force
reclassification; construction/growth estimates use meaningful timing bands for
eligibility. Failed stable inputs remain cached. With no usable answer, normal
gameplay and independently enabled native advice continue. These questions share
the existing additional allowance and startup cooldown. Status exposes internal
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

Preservation presents source-linked attributed statements and unresolved
questions at a normal activation. It never creates an activation or saves memory
on the player's behalf. Belief notices carry the belief version and new source
versions; they never change claims, uncertainty, citations or lifecycle. New
evidence is restricted to attributed statements and recorded material facts;
classifier results, guidance observations and notices cannot seed cascades.
Candidates/notices are separate activation data. The player can read their
sources before choosing explicit notebook edits.

## Early reviews

Before selecting participants, the scheduler requests triage only for players
with Decisions enabled and no scheduled review due now or next round. Triage
uses previously recorded permitted material facts and saved reconsideration
criteria, without opening a player connection or capturing a full review.
Acceptance needs meaningful changed evidence, configured confidence, three
rounds since any completed review and ten since the last accepted early review.
An unchanged attempted signature does not create another paid call.

Accepted requests join the same communication-draining hold, host barrier,
parallel review and release workflow. Persisted participant metadata lets an
interrupted barrier resume without reclassification. Overlapping periodic and
early reviews coalesce. Early-only participants do not reset periodic anchors;
existing periodic and mandatory war triggers retain their behavior. Private
criteria go to triage only, never to the independent strategic reviewer.

## Validation and operations

See the [operating guide](../guides/Native-guidance-and-routing.md) and
[trial configuration](../../examples/native-routing/trial.toml). Read-only game
status separates direct native advice exposure, broker spending/reservations,
diplomacy classifier usage and full review usage.
The broker ledger is the spending authority; checkpoint metadata is diagnostic.

The labeled [advisory replay fixture](../../tests/fixtures/decisions-advisory-replay.json)
has seven synthetic, provenance-labeled cases. Offline replay with no responses
reports missing data, not accuracy. The bounded live replay exercises real API
contracts and accounting, and does not execute gameplay or measure stronger play.
Tests cover direct native delivery, independent toggles, unrestricted legal
choices, explicit memory reads, non-recursive notices, barrier/anchor behavior, concurrent reservations, lost
replies, restarts, rewinds, token rotation and pinned authorities. No engine edits
or public contributions are part of this advisory layer.
