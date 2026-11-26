# Native guidance and Decisions advisories

`decisions_enabled` controls whether Decisions is used. When enabled, it runs the
current implementation only. Example matched configurations are in [examples/native-routing](../../examples/native-routing).
Their seeds, assignments, models, reasoning settings and limits match; only
`agents.native_advice_enabled` and `agents.decisions_enabled` differ.
The Decisions switch controls diplomacy routing, memory ranking, preservation
candidates, belief notices, early review triage and city focus/construction advice. Native suggestions go directly
to the gameplay model under the independent `native_advice_enabled` switch.
`decisions-only` enables Decisions advisories without the native advisor; `combined` enables
both. Retired `diplomacy_routing_enabled` TOML keys and CLI flags
produce an error; explicitly choose `decisions_enabled` for a new game.
See the [advisory architecture](../architecture/Decisions-advisory-layer.md).

## Credentials and launch

Decisions has two backends: `jev` (the default, using local Winnow) and `luna`
(OpenAI Decisions). Jev uses `http://127.0.0.1:8091/v1/systemone` during local
development and requires no API key. The trusted container broker reaches host
loopback through its own pasta mapping. Set the operator environment variable
`CIV_JEV_ENDPOINT` to override that endpoint; in container launches, the URL must
be reachable from the broker container. Player containers retain socket-only
access to classification.

Luna requires a separate OpenAI API key in `AUTH_DIR/decisions.key`, a regular
private file (mode `600`) outside the match directory. Configure it using your
normal secret-management process. Missing or invalid Luna credentials reject an
enabled Luna game before image resolution or launch.

```toml
[agents]
decisions_enabled = true
decisions_backend = "jev"                  # or "luna"
decisions_acceptance_probability = 0.98     # optional: Jev 0.98, Luna 0.95
```

Acceptance uses the probability assigned to the selected answer among the
supplied alternatives. The common API's legacy `confidence` field aliases that
probability; `rawAnswers` also retains explicit `probability` and native
`providerConfidence` fields. Jev's entropy and Luna's margin scores are
diagnostics, and do not grant acceptance. Conditional answer probability is not
a measured chance of correctness. Defaults are conservative operating points
from the local comparison, rather than a calibration guarantee.

The supervisor starts a trusted Decisions broker with the auth directory. Player
services receive only its local socket and their own broker credential, distinct
from the provider-login credential. The OpenAI API key is not mounted into player
containers. Local agent-service development uses `CIV_DECISIONS_SOCKET` and
`CIV_DECISIONS_TOKEN_FILE` to identify this broker; neither variable holds an API
key. `CIV_DECISIONS_BACKEND` selects the expected backend for standalone broker
clients; game containers receive it from their saved configuration.
The classifier is `Winnow-12B` for Jev and `gpt-6-luna` for Luna.
The only supported routing policy is
`diplomacy-routing-v4`; enabling Decisions selects it automatically. Older
routing policies, account settings and response schemas are rejected.

Use the ordinary [game launch workflow](Full-vanilla-run.md) with the selected
example. Effective flags, guide version, classifier model and policy version are
recorded in `container.json` under `experiments`, and flags in `resolved.json`.
The engine changes must be published and pinned under the
[dependency workflow](../Development-workflow.md) before a reproducible remote
build. Local image builds use the selected checkout, including uncommitted engine
changes, so publication is not required for a local trial.

The broker authenticates a free local health check before agents start and verifies
its protocol, model and policy version. A running broker from another runtime image
rejects the new launch. Rebuild the current runtime image and use
`civ-agent auth restart` to replace the broker. The preflight does not call the provider. Its upstream
access is `unchecked` until an actual classification succeeds or fails.

The Luna backend uses Platform API-key authentication and separate API billing.
ChatGPT subscription access used by the game agents does not supply this key.
Choose Jev to use Decisions without API billing or a Decisions key.

The broker protocol is version 4. Old confidence-named TOML/CLI controls are
rejected so their values cannot silently acquire different units. Use
`decisions_acceptance_probability` and the routing probability overrides below.
Existing broker accounts pin their complete settings; historical accounts and
receipts are not silently relabeled or converted. Rebuild runtime images for
new games using this contract.

## Short local trial

[trial.toml](../../examples/native-routing/trial.toml) configures two agents and one
native AI on a small map, bounded by 20 turns, 100 activations and two hours. The
normal [launch workflow](Full-vanilla-run.md) accepts this file:

```sh
civ-agent game start --config examples/native-routing/trial.toml --state-dir PATH
civ-agent game status --state-dir PATH
```

For a subscription-only guidance trial, add `--no-decisions-enabled` to
the `game start` command. This keeps native guidance enabled and needs no
Decisions key. Messages involving a human player bypass classification even when
routing is enabled, including public chat that reaches that human.

`game status` includes effective experiment settings, authenticated broker health,
classifier fallbacks/latency/usage, latest advice availability and call latency,
and outstanding delivery counts/deadlines. It reads service databases without
constructing services or interrupting running jobs. `upstreamStatus=unchecked`
does not confirm key or model access. Provider authentication, access, rate-limit
and outage failures are distinguished without exposing keys or provider bodies.
Status separates native recommendation exposure/action overlap,
internal city preview capture and delivered city recommendations (`cityAdvice`),
`additionalSpending` from the trusted ledger, and `fullReviewUsage` from service telemetry. The broker ledger is the
spending authority; checkpoint metadata can show an older diagnostic sample.

For Luna, the default $0.10 additional allowance is per player-game and shared by all
new advisory tasks. Diplomacy scheduling and full strategic-review model jobs
remain separate. The broker reserves estimated cost before dispatch and settles
validated input usage. Unknown completions retain reservations. Keep both
`AUTH_DIR/decisions.sqlite3` and `decisions-authority.json`; the game pins that
accounting authority and refuses to resume against another auth directory.
Restarting, rotating a token, moving the match or rewinding does not refill it.
The allowance is a **local estimated spending ceiling**, not an invoice guarantee.
Standard Decisions input costs
[$0.10 per million tokens with no output charge](https://developers.openai.com/api/docs/guides/decisions#pricing-and-availability);
regional and long-context premiums may differ. All defaults and bounds are shown
in [trial.toml](../../examples/native-routing/trial.toml), including
`memory_context_chars=12000`. Set `additional_decisions_budget_usd=0` to preserve
routing and independent native advice while disabling new paid advisory work.
Jev records token usage and durable receipts at zero cost, even when the dollar
allowance is zero or an earlier paid allowance was exhausted. Historical paid
receipts keep their recorded rate; restarting never refunds them.

Rootless container smoke checks use an explicit freshly built runtime image,
synthetic credentials and a fake provider; they make no model requests:

```sh
CIV_CONTAINER_TESTS=1 CIV_TEST_IMAGE=localhost/civ-agent:runtime \
  uv run --no-sync pytest -q tests/test_decisions_container.py
```

## Player behavior

Guidance adds up to eight recommendations to gameplay startup and a read-only
`native_advice` tool for refreshes and optional city/unit filters. Each result
names its epoch, turn and observed revision. It preserves real human costs and
production progress and never changes controller type or relaxes command rules.
Commands still require the ordinary details, confirmations and current revision.
Native advisor suggestions always reach the gameplay model directly. Decisions does not
select a subset, add strategic priorities or replace their explanations. Native
reads and gameplay actions never trigger paid classification. Every suggestion
is optional; rejecting all advice requires no override, and other native-legal
actions remain available. Decisions outages or budget exhaustion affect Decisions
advisories; native advice remains available independently.

At eligible gameplay startup, up to four owned cities each get independent change
and alternative questions for focus and construction in the same budgeted batch.
Changing requires an accepted change answer and an accepted alternative; a
confident no-change answer keeps the current assignments or queue. Focus alternatives
use native allocations on detached copies, available explicitly through
`player_inspect(subject="CITY_FOCUS", target=city_handle)`. Construction options
retain native costs, effects, progress and commands; queue append and reorder are
distinct operations. The model coordinates both answers and ordinary inspections
remain required before action. Captured inputs do not count as delivered evidence.
Current request/question limits can omit whole city pairs, with omission counts.
See the architecture for candidate bounds, caching and stale-result handling.

Optional old active memories are ranked within the existing character budget and
delivered through normal explicit memory reads with their original dates,
uncertainty and citations. Preservation candidates and separate version-pinned
belief notices appear only at normal activations. The player decides what to
read and save; explicit notebook edits keep their existing behavior. An early
review is checked only at fresh completed-round boundaries reached during play,
starting after completed turn 10. Startup and resume do not issue new early-review
classification requests. A check requires meaningful recorded evidence, sufficient
confidence, three rounds since any review, and ten since the last early review. Scheduled reviews due now
or next round suppress it. Early reviews use the existing shared barrier and
reset each reviewed player's periodic anchor. Private player rationales are not sent to the reviewer.
Recommendations with overlapping subject, treasury, target or destination are
alternatives, not a sequence. An action invalidates the old position.

The guide shares native selectors for construction, buying, upgrades, citizen
assignment, current-tile improvements, nearby settlement/exploration/attack-origin
movement, attacks, bombardment, healing and promotions. It deliberately reports
coverage limits: foreign-capital defense/wonder competition, strategic production
branches, unseen/long movement routes, city attack valuation, road networks,
multi-step worker plans, air/religious/great-person actions and diplomacy are not
fully ranked. Damage ranges and promotion pools remain alternatives without
consuming live random state. This is not full native autoplay coverage or a proof
of the player API's complete privacy boundary. The
[advice information-boundary audit](../architecture/Native-advice-information-boundary.md)
records permitted dependencies and excluded native branches. Selection uses detached value
inputs outside the game lock and a cooperative five-second deadline; failure
returns unavailable advice without blocking ordinary play.
Revision-valid cache hits avoid repeated input capture. Healing considers all
observed hostile units within the native danger radius; its explanations describe
observed threats and never assert that undisclosed enemies are absent.

With routing enabled, `chat_send(delivery="auto")` may return `sent`, `queued`,
or `omitted`, with `deliveryId` and `delivered`. Only `sent` with a real host
receipt means delivered. Original text and audience are retained. Use
`delivery="immediate"` to bypass scheduling or
`chat_delivery(deliveryId=..., action="status"|"immediate"|"cancel")` to inspect,
send or cancel an owned draft. Repeating an override does not duplicate delivery.
An unknown delivery ID returns `UNKNOWN_DELIVERY` with `applied=false`; the model
can read the outstanding statuses and correct its request without pausing the game.
`chat_read` includes outstanding statuses. There is no message quota, rewriting,
honesty policy or enforcement of social promises.

Binding proposals, consent, native diplomatic actions, humans and active binding
negotiations bypass classification. Ordinary batching is bounded by the oldest
item's 30-second deadline; arrivals do not extend it. Incoming suppression skips
only a separate communication activation, never history or gameplay reads. New
events can activate communication again during the same turn. Classification
defaults to confidence at least 0.90 to delay/omit; refusal, malformed output,
five-second timeout and outages fail open. No automatic API retries are used;
three transport failures open a temporary 30-second circuit. Waiting for the
per-player classifier slot consumes the same five-second budget. An unclassified
incoming backlog becomes eligible at 30 seconds; a late answer cannot suppress
that fallback or an event already delivered to the player model.

Checkpoint barriers cancel unfinished classifications and suspend delivery.
Drafts are not unsettled host requests until delivery begins. Sending uses a
durable request identity. The routing worker owns reconciliation of routed sends;
general chat reconciliation leaves those requests to it. Sending and receipt
recovery are serialized, so a lookup cannot turn an active send into a failed
delivery. `NOT_ENACTED` means no receipt was found, not that the request can never
succeed. Missing or transient results stay uncertain. The routing worker can
retry the exact original request; gameplay commands are not automatically replayed.
Confirmed receipts remain immutable, and unresolved requests block checkpoint
capture with `CHECKPOINT_REQUEST_UNSETTLED`. Before taking snapshots, the trusted
coordinator resolves abandoned non-routed requests under the paused host executor:
completed actions retain their receipts; absent actions receive a durable
`REQUEST_CANCELLED` receipt that also blocks late execution. The player and host
snapshots therefore include the same outcome. Restart retains deadlines;
rewind retains only reachable decisions and queues. Pauses can exceed a queue's
wall-clock deadline: delivery resumes only after the existing barrier permits it.

## Review evidence and failures

Reviewers can cite any observation delivered in their starting context or frozen
tool reads. Each has a source label tied to its exact permitted contents and date.
Material assumptions cite those labels and distinguish inference from unknown
premises. Validation checks that sources were actually delivered to that review;
it does not certify that an inference is true. A short named-fact summary remains
available but does not restrict which observations can support a report. Cited
payloads stay in the private report archive; later advice and adopted strategies
retain compact dated references.

An invalid report receives clear feedback and at most one correction, usually in
the same conversation with the observations already read. A configured attempt
limit of one disables correction; larger limits do not add repeated attempts.
If no usable report results, that review becomes `UNAVAILABLE`, its failed input
and diagnostics remain inspectable, and play resumes when the shared phase is
otherwise safe. Players see that advice is unavailable; an earlier completed
report may still appear with its original date. Missing advice does not invent a
replacement strategy or advance the completed-review evidence baseline.

Structured turns limit every assistant message to 128 KiB, regardless of its phase.
More than 4,096 consecutive whitespace characters outside JSON strings, or a
4,096-character suffix repeating a pattern of up to 64 characters, stops generation
through the bounded recovery path. These guards do not inspect reasoning or tool output. Adjacent
text fragments from the same stream are combined before history processing,
without changing their text or crossing tool, usage or completion events. The
notification queue remains bounded at 16 MiB and 4,096 entries, including work
being processed; overflow is an explicit failure rather than silent event loss.

Shutdown confirms process exit separately from saving accepted history and
accounting events. `STOP_UNCONFIRMED` means process shutdown was not confirmed.
`HISTORY_DRAIN_FAILED` or `REVIEW_HISTORY_DRAIN_FAILED` means those accepted events
could not finish draining after shutdown. Persistence, input-preparation and
shutdown/drain failures still stop for inspection; the service never publishes
a successful review or starts another reviewer over unfinished shutdown work.

## Advisory policy v4

`player-advisories-v4` covers memory selection, preservation, belief notices,
early-review triage and city recommendations. It is separate from the diplomacy
routing policy described below. Each request identifies `player`; broker identity
must agree with that perspective. Attributed sources retain the exact text,
author, audience and source turn. Candidate chronology uses source turn and
event sequence, rather than lexical ordering of record IDs. Request limits omit
whole optional candidates and report coverage; they do not shorten a source's
uncertain wording or make omitted information evidence of absence.

Every submitted question has a `questionResults` row. Valid typed answers remain
in `rawAnswers`, including choices below the confidence threshold and optional
probability vectors. Only independently accepted choices enter `answers`:

| Question status | Meaning |
| --- | --- |
| `accepted` | Valid choice at or above `decisions_acceptance_probability`. |
| `low_confidence` | Valid raw choice retained, with no accepted choice. |
| `refusal` | Typed refusal retained, with no invented choice or confidence. |
| `missing` | No answer for this submitted question. |
| `malformed` | This question's answer cannot be validated. |

A batch-level fallback summarizes the result; one uncertain or refused answer
does not discard an independent accepted answer. The runtime verifies request,
policy, model, input, prompt and question identities before using broker results.
Acceptance is separate from delivery: source/objective changes or a stale native
position can still prevent an accepted recommendation from reaching the player.
None of these answers executes an action or writes notebook content.

Private `advisory_delivery` events record actual context, notice, city-advice or
review-scheduling exposure against the original request and question. Cached
memory can be recorded as exposed in a later activation if it then fits the
context budget. A review is exposed only when a review task is prepared.
These events do not count as classifier calls, consume tokens or replace a
question's confidence status. Exposure does not establish that the player
followed the advice.
Status counts each immutable advisory request once for calls, provider tokens
and latency, including usage recovered from a receipt. `advisoryObservations`
separately retains receipt counts and transient failures.

Optional memory retrieval asks two independent binary questions in one request.
`retrieval_include` decides whether showing the memory would help a current
decision or unresolved question. `retrieval_priority` asks whether it deserves
priority, assuming it is shown. Confident inclusion can make an older optional
memory eligible; priority changes ordering among eligible memories and cannot
by itself grant eligibility. Confident omission applies to optional notes and
beliefs, while a pending `needsReview` notice remains eligible. The existing
character budget still bounds selected context, and explicit memory reads remain
available. Preservation queues an attributed source for the player's judgment;
belief notices compare the belief's polarity and uncertainty against recorded
new evidence without revising it. Recorded overview comparisons disclose missing
fields and truncated rosters instead of asserting loss or contradiction.

Completed strategic reviews atomically retain a bounded material-facts baseline,
its full fingerprint, schema and source identity. Early triage compares that
baseline with current recorded facts, explicit deltas, existing `recentProgress`
and attributed new statements. Early triage requires a valid committed baseline;
missing, invalid or hash-only baselines do not trigger classification. Incomplete
comparisons remain explicit. The scheduler supplies authoritative `nextReviewTurn` in
completed-round units; `sourceTurn`, `completedTurn` and `boundaryTurn` distinguish
observation age from the upcoming round boundary. The single binary question asks
whether an additional review is worth its cost before the scheduled review.
Existing minimum gaps, shared barriers and suppression when a periodic review is
due now or next round remain in force. Routine turn/version increments do not
alone generate another paid classification of unchanged material context.
Recovered review judgments retain their original request identity and use
separately validated current delivery bindings when only observation identity
has changed.

Memory triage and city capture have independent attempt clocks. A communication
activation cannot consume the next eligible gameplay city's attempt. City capture
requires native `phase="ACTIVE"`, including active positions whose mandatory
choices make `canEndTurn=false`; native availability and freshness checks still
apply. Capture attempts are bounded even on failure. Semantically unchanged city
inputs reuse their original result, while fresh native bindings and the complete
active-objective set are checked before delivery. Focus and construction remain
independent questions over the same captured state; their answers do not assume
that another recommendation was applied.

The runtime records attempts before broker I/O and retains outcomes by immutable
request and source context. Repeated low-confidence, refusal or other completed
outcomes do not trigger another paid call for the same context. After a lost or
unverifiable broker reply, at most one free receipt lookup per reachable cached
attempt can reconcile the original request, including a mixed memory/city batch.
The lookup cannot call the provider or reserve more funds. Only an explicit
`not_submitted` receipt permits
resubmitting that same immutable request through the broker's ledger deduplication.
An unknown completion is never retried as a new paid request; its reservation
remains authoritative. Checkpoint rewind or credential rotation does not refill
the allowance.

## Routing prompt and confidence controls

The current `diplomacy-routing-v4` policy separates timing from usefulness.
Immediate attention or delivery requires evidence that waiting within the
remaining delay budget could miss an imminent action, deadline or interaction
opportunity. Useful new facts, questions and changed terms can be batched when
there is no such time sensitivity. Incoming suppression skips only a separate
communication activation; the message remains in history and gameplay reads.
Outgoing omission preserves the original draft for an explicit player override.
Neither choice rewrites text, decides gameplay or enforces social promises.

Inputs identify the named player, direction, current turn and remaining delay
when known. `pending` is separate from the exact-audience thread objects in
`recentExchanges`; a pending message does not become evidence of an earlier
response merely because it appeared in a context excerpt. Missing history is
not proof that no prior exchange exists or that a question has been answered.
An outgoing draft with status `omitted` or `classifying` is not evidence that its
contents have been delivered or will be sent; a matching `queued` draft has a
scheduled delivery. Protected bypasses and deadlines remain application rules.

Enable the current implementation with:

```toml
[agents]
decisions_enabled = true
decisions_backend = "jev"
decisions_acceptance_probability = 0.98
# Optional per-action overrides:
decisions_incoming_batch_probability = 0.98
decisions_incoming_suppress_probability = 0.99
decisions_outgoing_batch_probability = 0.98
decisions_outgoing_omit_probability = 0.99
```

These four probability overrides govern routing alone. When omitted, they inherit
`decisions_acceptance_probability`, which also governs city, memory and review
advice. Explicit routing overrides remain independent of the global setting.
An immediate model
choice needs no confidence override; low confidence and an actual override from
batch/suppression to immediate handling are recorded separately. Original choice,
confidence, validated probability vector, effective choice, threshold and policy
version are retained without message text in accounting. Provider refusals and
malformed probabilities must not be interpreted as calibrated certainty.

Routing settings are registered immutably with the trusted broker. Broker and
player runtimes must report the current routing policy, advisory policy and
protocol. There are no older execution paths or settings migrations. Unsupported
versions fail startup validation; a mismatched in-flight result is discarded and
reported. Build the current image and use `civ-agent auth restart` to replace
shared services. The isolated evaluation command below does not upgrade them.

`diplomacy-routing-v4` uses two independent binary-choice questions in each
Decisions request. Incoming questions are:

1. `response_needed`: "Do we need to respond to this communication?" Response
   includes reviewing and potentially acting on consequential information, even
   without a written reply. Unresolved requests, changed terms and new dated
   facts remain useful; missing history does not establish redundancy.
2. `response_timing`: "Assuming a response is needed, when does this communication
   need attention?" The choices are `now` and `can_wait` within the remaining
   batching window. This hypothetical is answered from the input independently
   of question 1; it describes when attention starts, not when a reply completes.

A `not_needed` answer at or above the incoming suppression threshold skips a
separate activation. Otherwise `can_wait` at or above the incoming batching
threshold permits batching, even when the necessity answer is uncertain.
Other results receive immediate attention. Both questions must be valid;
refusal, incomplete or malformed answers fail open. Existing protection and
deadline bypasses still apply. All thresholds remain 0.90 by default.

This policy is the default and the only accepted `decisions_routing_policy` value.
Outgoing questions are `delivery_needed` (whether this original draft needs delivery
to this exact audience) and `delivery_timing` (assuming delivery is needed, whether
delivery can wait until the end of the full remaining window). An authored
deadline is attributed timing evidence even if it could be a bluff; it does not
become a binding gameplay rule or an instruction to the classifier. Relative
deadlines follow their stated drafting, receipt or future-event anchor; quoted
expired deadlines do not restart. Confidently unnecessary drafts can be
omitted. Otherwise a confident safe-delay answer batches the draft even when
delivery necessity is uncertain; other outcomes send immediately.
The timing question compares delivery after the full delay with the deadline:
a deadline farther away does not by itself require immediate delivery. Supplied
lead times count as evidence; unknown recipient response time is not invented.
Records retain both named `rawAnswers` with their own confidence and
binary probabilities. They do not invent a three-way probability distribution.
`questionBasis` identifies the question supporting the effective action;
`confidence` belongs to that question, while `rawChoiceConfidence` belongs to
the unthresholded projected disposition. The runtime independently checks that
the effective non-immediate action uses the corresponding question evidence.
Status and replay report each question separately, including unknown answers.
Returned confidence and choice probabilities are distinct measures; thresholds
are action-specific controls, not an empirical accuracy guarantee.

The oldest message's deadline decides when to wake. Once a communication job is
awake, it can combine younger classified batch messages rather than leave them
for another activation. Gameplay can acknowledge successfully handled message IDs
with `chat_read(handledMessages=[...])`; only IDs delivered to that job can be
acknowledged, and a failed job does not complete them. Merely showing a message
in context does not establish that it was handled.
