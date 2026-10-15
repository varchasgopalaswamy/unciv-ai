# Native guidance and Decisions advisories

These capabilities are opt-in for **new games**. Do not change flags on active
benchmarks. Example matched configurations are in [examples/native-routing](../../examples/native-routing).
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

Standalone native guidance needs no additional credential. Decisions needs a
separate OpenAI API key in `AUTH_DIR/decisions.key`, a regular private file (mode `600`) outside the match
directory. Configure it using your normal secret-management process, not in a
TOML file, command argument, notebook or player workspace. Missing or invalid
credentials reject an enabled new game before image resolution or launch.

The supervisor starts a trusted Decisions broker with the auth directory. Player
services receive only its local socket and their own broker credential, distinct
from the provider-login credential. The OpenAI API key is not mounted into player
containers. Local agent-service development uses `CIV_DECISIONS_SOCKET` and
`CIV_DECISIONS_TOKEN_FILE` to identify this broker; neither variable holds an API
key. The classifier is `gpt-6-luna`. The default policy remains
`diplomacy-routing-v1`; the revised `diplomacy-routing-v2` policy is an explicit
candidate for new trials, pending the evaluation gates below.

Use the ordinary [game launch workflow](Full-vanilla-run.md) with the selected
example. Effective flags, guide version, classifier model and policy version are
recorded in `container.json` under `experiments`, and flags in `resolved.json`.
The engine changes must be published and pinned under the
[dependency workflow](../Development-workflow.md) before a reproducible remote
build. Local image builds use the selected checkout, including uncommitted engine
changes, so publication is not required for a local trial.

The broker authenticates a free local health check before agents start and verifies
its protocol, model and policy version. A running broker from another runtime image
rejects the new launch; stop its affected games before replacing it, or select
another auth directory. The preflight does not call the provider. Its upstream
access is `unchecked` until an actual classification succeeds or fails.

The current Decisions backend uses Platform API-key authentication and separate
API billing. ChatGPT subscription access used by the game agents does not supply
this key. The documented [ChatGPT plan integration](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
supports Responses inference; the [Decisions API](https://developers.openai.com/api/docs/guides/decisions)
is a separate endpoint. To try guidance without a Decisions key, set
`decisions_enabled=false` on a new game.

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

The default $0.10 additional allowance is per player-game and shared by all
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

At eligible gameplay startup, up to four owned cities each get a population-focus
question and a construction question in the same budgeted memory/advisory batch.
Each answer has its own confidence and keep/defer alternatives. Focus alternatives
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
review requires meaningful recorded evidence, sufficient confidence, three rounds
since any review, and ten since the last early review. Scheduled reviews due now
or next round suppress it. Early reviews use the existing shared barrier, retain
periodic anchors, and do not send private player rationales to the reviewer.
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
durable request identity and receipt reconciliation. Restart retains deadlines;
rewind retains only reachable decisions and queues. Pauses can exceed a queue's
wall-clock deadline: delivery resumes only after the existing barrier permits it.

## Advisory policy v3

`player-advisories-v3` covers memory selection, preservation, belief notices,
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
| `accepted` | Valid choice at or above `decisions_min_confidence`. |
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
and attributed new statements. Unavailable baselines and incomplete comparisons
remain explicit. The scheduler supplies authoritative `nextReviewTurn` in
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

The candidate `diplomacy-routing-v2` policy separates timing from usefulness.
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

Select the candidate only in a new trial configuration:

```toml
[agents]
decisions_enabled = true
decisions_routing_policy = "diplomacy-routing-v2"
decisions_incoming_batch_confidence = 0.90
decisions_incoming_suppress_confidence = 0.90
decisions_outgoing_batch_confidence = 0.90
decisions_outgoing_omit_confidence = 0.90
```

These four confidence controls govern routing alone. Keep
`decisions_min_confidence` at its advisory setting; lowering it also affects
city, memory and review advice. Older saved settings without an explicit routing
policy preserve their previous effective routing thresholds. An immediate model
choice needs no confidence override; low confidence and an actual override from
batch/suppression to immediate handling are recorded separately. Original choice,
confidence, validated probability vector, effective choice, threshold and policy
version are retained without message text in accounting. Provider refusals and
malformed probabilities must not be interpreted as calibrated certainty.

Routing settings are registered immutably with the trusted broker. A candidate
game needs compatible broker and runtime code in a new trial environment;
the isolated comparison command below does not upgrade a running broker.
Keep existing games on their pinned services. A mismatched routing-policy
version fails open to immediate handling and is recorded as such.

## Evaluation and remaining rollout gate

`diplomacy-routing-v3` changes incoming routing to two independent binary-choice
questions in the same Decisions request:

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

Select this policy with `decisions_routing_policy = "diplomacy-routing-v3"` in a
new trial configuration. Outgoing requests and parsing are identical to v2.
Incoming records retain both named `rawAnswers` with their own confidence and
binary probabilities. They do not invent a three-way probability distribution.
`questionBasis` identifies the question supporting the effective action;
`confidence` belongs to that question, while `rawChoiceConfidence` belongs to
the unthresholded projected disposition. The runtime independently checks that
the effective non-immediate action uses the corresponding question evidence.
Status and replay report each question separately, including unknown answers.

To compare v3 with the same frozen 185-case benchmark, reuse verified identical
outgoing requests and make only 96 new incoming requests:

```sh
uv run --no-sync python scripts/trial_diplomacy_routing.py \
  --trial-dir build/routing-two-question-trial-20261015 \
  --fixtures build/routing-prompt-trial-20261015/fixture.json \
  --reuse-trial build/routing-prompt-trial-20261015 \
  --policies two-question --max-calls 96 --budget-usd 0.05 \
  --key-file AUTH_DIR/decisions.key
```

Reuse checks the exact frozen fixture bytes, model and outgoing request bodies
before dispatch. The 89 cached outgoing results, including two earlier timeouts,
remain explicitly identified as reused; they do not count as new calls or spend.
The old journals, labels and game-separated splits remain unchanged. This is a
further comparison on an already examined benchmark, not a fresh blind holdout.

The v3 run completed all 96 new requests without provider failures, using
145,780 input tokens ($0.014578 at the pinned local rate). The previous journal
and exact fixture bytes were independently verified unchanged. At the existing
0.90 thresholds, incoming results were:

| Incoming measure, 96 cases | v2 | Two-question v3 |
| --- | ---: | ---: |
| Effective dispositions matching acceptable labels | 25 | 87 |
| Batch | 15 | 86 |
| Immediate attention | 81 | 9 |
| Skip separate activation | 0 | 1 |
| Unnecessary immediate attention | 71 | 4 |
| Harmful effective delays / suppressions | 0 / 0 | 0 / 0 |

On the 62 real incoming cases, batching increased from 6 to 62 and immediate
attention fell from 56 to zero. Effective agreement rose from 11/62 to 61/62;
one clearly duplicated answer still receives unnecessary batched attention.
Necessity remains uncertain: median `response_needed` confidence was 0.87 on
these cases, while median `response_timing` confidence was 0.98. Five uncertain
`not_needed` answers were conservatively converted to supported batching;
they did not force immediate activation. Across all incoming cases, nine
confidence overrides ended in batching and two in immediate attention.

This improves the composition of the answers, not raw semantic accuracy.
Projected raw-choice agreement was 91/96 for v2 and 89/96 for v3. Three real
false raw suppressions and the changed-relative-date authored error were blocked
by the necessity threshold. All five strict urgent incoming authored cases
received immediate attention. The sole accepted suppression was a simple
authored acknowledgement; suppression precision remains largely unmeasured.
The previously held-out incoming subset improved from 10/29 to 27/29 effective
agreement with no paired regressions, but it was already examined before this
iteration and contains no strict urgent real case.

The new incoming requests used about 26% more input tokens than the old incoming
requests; observed mean provider latency was 204 ms versus 247 ms. These calls
were made later than the cached baseline, so the latency change is not isolated
to question formulation. Neither the static replay nor batching counts establish
token/time savings per player-turn. Outgoing routing is unchanged and its 89
results, including two old timeouts, were reused rather than measured again.

The [v3 comparison report](../../build/routing-two-question-trial-20261015/comparison-report.json),
[provenance verification](../../build/routing-two-question-trial-20261015/independent-comparison.json)
and [prior-holdout review](../../build/routing-two-question-trial-20261015/independent-prior-holdout-audit.json)
record the evidence and limitations. V3 remains opt-in, with existing defaults,
saved games and auth services unchanged. Matched new-game evaluation remains
the next behavioral validation step.

The current [advisory v3 fixture](../../tests/fixtures/decisions-advisory-replay.json)
contains 19 assistant-authored cases with explicit provenance and grouped
calibration/holdout labels. It covers the six advisory tasks and constructed
parser cases; related variants are not independent gameplay observations. V3 has
been checked with authored inputs and fake responses, without a new paid quality
trial. These checks establish neither classifier accuracy in games nor token,
latency or gameplay improvements. Validate the fixture and replay harness offline:

```sh
uv run --no-sync python -m civ_agent.advisory_replay tests/fixtures/decisions-advisory-replay.json
uv run --no-sync pytest -q tests/test_advisory_answers.py tests/test_advisory_replay_v3.py
```

Without responses, replay reports unobserved questions rather than manufacturing
answers. Supply `--responses PATH` only for version-matched saved results with
verified request, input and prompt identities. `--calibrate` describes calibration
cases only and does not select a threshold. Historical response captures do not
become v3 evidence by changing a version string.

Live replay requires `--live-broker` with the authenticated local broker environment
and uses that player's existing allowance.
For an isolated short classifier trial with a $0.005 local estimated ceiling:

```sh
uv run python scripts/trial_decisions_advisories.py \
  --trial-dir /tmp/advisory-trial --key-file AUTH_DIR/decisions.key
```

Reusing the trial directory reuses its immutable account and cached results.
The separate key is read only in the trusted trial process. The report includes
submitted-case frequency, tokens, validated input cost, reservations, fallbacks,
labels and triage outcomes. This trial executes no gameplay and cannot
measure stronger play; matched gameplay evaluation remains
a separate rollout requirement.

The labeled fixture [diplomacy-routing-replay.json](../../tests/fixtures/diplomacy-routing-replay.json)
contains seven source-attributed audit examples, one explicitly counterfactual
duplicate and four synthetic edge cases. Labels concern scheduling, not truth or
promise compliance. Histories exclude later messages and other audiences.

The larger
[diplomacy-routing-calibration.json](../../tests/fixtures/diplomacy-routing-calibration.json)
contains 179 cases: 172 authored variants in 43 synthetic scenario groups and
seven historical incoming excerpts from four exact-audience threads in one
archived game. Twenty-one authored scenario groups cover outgoing delivery, which
was absent from the original observed sample. Related variants and whole real
threads stay in the same split: 92 calibration cases and 87 holdout cases.
The 11 protected cases are bypass checks, not paid classifier examples.

Every label is an **assistant-authored scheduling judgment, not human-reviewed
ground truth**. The synthetic variations are not independent observations and
the holdout was reserved by the same author, not an external blind evaluator.
Historical messages retain their source attribution and original text; history
is limited to the last six earlier messages to match production shape. Historical
wall-clock timing is unknown and is never filled in using later game state.
Several old labels treated usefulness as urgency; the new rationales apply the
30-second scheduling distinction explicitly. Ambiguous cases allow multiple
acceptable choices. These provisional judgments support comparison against the
stated routing intent; their coverage and uncertainty limit conclusions about
production error rates. Same-wording relative-time reports on later
turns are useful updates, not duplicate evidence. Timing counterfactuals include
the same deadline with five versus 28 seconds of remaining batching budget;
quoted, past and negated deadlines are explicit hard negatives. This authored
pilot is a coverage supplement, not a representative estimate of actual
benchmark routing correctness.

The broader historical collection covers all 28 games in seven benchmark
batches, including saved older games. It contains 32,641 observer-local cases:
16,179 incoming messages and 16,462 outgoing calls. Collect a fresh private
snapshot without network requests or benchmark writes:

```sh
uv run --no-sync python scripts/collect_diplomacy_routing_cases.py \
  --benchmarks-root benchmarks \
  --output-dir build/routing-historical-review --sample-size 120
```

`all-cases.json` preserves the reconstructed evidence; `sample-unlabeled.json`
selects cases by a deterministic round-robin over game and direction, with input
hashes determining order within each bucket. Protected cases are excluded from
that review sample. The private `observed-outcomes.json` file holds matched old
routing results separately: keep it hidden while labeling. `summary.json`
records coverage and fingerprints. Original classifier inputs were not retained,
so these cases must not be described as exact replays of earlier API requests.

Incoming histories contain only earlier event IDs from the same player epoch and
exact audience. Outgoing history remains explicitly unknown: call insertion
order cannot prove that a read completed before a send, and final job snapshots
can change during a job. The collector therefore uses neither as earlier
conversation evidence. Wall-clock delay budgets, queued drafts and private
objectives are also unknown. Missing evidence never establishes duplicate
delivery, a resolved question or an inactive native negotiation; potentially
binding cases allow ambiguous labels where the available evidence warrants it.

The reviewed real sample contains 120 cases: 62 incoming and 58 outgoing across
all 28 games. Two games had no eligible outgoing sampling bucket after protected
calls were excluded. Its derived evaluation fixture reserves one whole
game per batch: 88 cases are calibration and 32 are holdout. Cases from the same
game stay together, including different players and threads; these reviewed
split assignments supersede the collector's preliminary split metadata.
Labels are assistant-authored judgments made before viewing API outputs, with
independent second review. This sample has little clear urgency and no outgoing
history with which to establish omission safety, so the authored safety cases
remain a necessary supplement. Neither set alone establishes production error
rates or gameplay savings.

The frozen 2026-10-15 comparison combined these 120 real cases with 65
prespecified authored cases, making 185 calls per policy at the unchanged 0.90
threshold. On the real sample:

| Measure | Legacy v1 | Candidate v2 |
| --- | ---: | ---: |
| Raw choices matching acceptable labels | 10/120 | 120/120 |
| Raw choices matching real holdout labels | 3/32 | 32/32 |
| Effective batch decisions | 0/120 | 40/120 |
| Median confidence | 0.33 | 0.85 |
| Calls below 0.90 confidence | 114/120 | 80/120 |
| Actual confidence overrides to immediate | 43/120 | 80/120 |

The override count rises because the old prompt often selected immediate
handling even with low confidence. Low confidence and changing a proposed
non-immediate choice are different events. The candidate separates usefulness
from urgency much more consistently on these provisional labels, but the
threshold still rejects two-thirds of its real routing proposals.
This is concentrated in incoming routing: 56/62 incoming calls were overridden,
versus 24/58 outgoing calls; their median confidences were 0.73 and 0.93.
Historical outgoing inputs lack prior history, so this is not an isolated test
of direction with otherwise identical evidence.

The candidate matched 52/63 available authored raw answers; two authored calls
timed out. The guard prevented an urgent request incorrectly batched at 0.48
confidence and a changed relative arrival date incorrectly suppressed at 0.70.
Keep the thresholds at 0.90. There were no harmful effective delays among 22
held-out batches, but this small correlated sample does not establish a
production error bound. No omission or suppression passed the threshold, so
zero harmful omissions supplies no omission-safety estimate. Some conservative
immediate choices also depend on unknown response/activation timing.

The trial reported $0.0316033 of validated input usage and retained $0.0012026
in local reservations for the two timeouts; provider completion/usage for those
requests remains unknown. Candidate classifier inputs used about 33% more
tokens on the real cases, with mean request latency 249 ms versus 236 ms.
These are classifier costs, not total player tokens or time per turn. The
private [comparison report](../../build/routing-real-cases-20261015/comparison-report.json)
and [independent error review](../../build/routing-prompt-trial-20261015/candidate-error-review.json)
retain the evidence. No prompts, thresholds or labels were changed after
viewing outcomes. The candidate remains opt-in pending matched new games.

Two additional live requests passed an isolated Unix broker → runtime routing →
text-free accounting canary, preserving raw choice, confidence, probabilities
and policy version. The runtime rejected forged low-confidence and mismatched
policy results. The [canary result](../../build/routing-wire-live-canary-20261015/summary.json)
records these checks. Comparison plus canary made 372 requests, with $0.0318630
of validated input usage and the same $0.0012026 reserved for unknown timeout
usage. This verifies the live service boundary without enacting gameplay or
changing saved games; matched new-game behavior is still unmeasured.

The offline builder uses the same bundle helper as production. Verify provenance,
pending/history separation and split integrity without making model requests:

```sh
uv run python scripts/build_routing_corpus.py --check
uv run pytest tests/test_routing_corpus.py tests/test_diplomacy_replay.py
uv run python -m civ_agent.diplomacy_replay tests/fixtures/diplomacy-routing-calibration.json
```

Validate the replay harness without network access:

```sh
uv run pytest tests/test_diplomacy_replay.py
uv run python -m civ_agent.diplomacy_replay tests/fixtures/diplomacy-routing-replay.json
```

Without responses, the report marks classifier cases missing; it does **not**
claim model accuracy. Supply `--responses PATH` for a JSON mapping from case IDs
to captured Decisions response envelopes. An explicit `--live-broker` instead
makes paid requests through the configured broker. Its output includes the raw
response mapping for later offline replay. Results that retained only an effective
fallback choice cannot reconstruct the discarded model choice and are marked
unavailable for raw-choice analysis.

For an isolated comparison of both prompts on the same frozen corpus, the
trusted trial process calls the provider with its own durable request journal
and spending reservations:

```sh
uv run python scripts/trial_diplomacy_routing.py \
  --trial-dir /tmp/routing-trial --key-file AUTH_DIR/decisions.key \
  --budget-usd 0.10 --max-calls 400
```

This command makes paid calls. The defaults impose a local estimated $0.10
ceiling and at most 400 requests across both policies; they do not guarantee a
complete comparison or a billing ceiling. The 168 nonprotected pilot cases require
336 requests for a full two-policy comparison. Reusing the trial directory keeps
its identity, frozen fixture, responses, spend and unresolved reservations;
resuming does not refill the allowance. Player message text stays in private
trial artifacts, outside text-free accounting and shell summaries.

Compare prompts at the existing 0.90 thresholds first. Report incorrect delays,
incorrect omissions, unnecessary immediate handling, useful batching/suppression,
confidence distributions and usage separately by direction, split and source
type. Higher confidence alone is not an improvement. Threshold sweeps use the
calibration split only; review routing-specific thresholds before a single
held-out check and leave advisory thresholds unchanged. Report uncertainty at
the scenario/thread level; related variants do not multiply the independent
sample size. A clean synthetic replay cannot establish an acceptable live error
rate or token savings.

Before production adoption, validate the broker and service path with protected
bypasses, deadlines, player overrides and recovery. Run an isolated canary, then
matched **new** games with the same seeds, assignments, models and runtime,
varying routing alone. Require no bypass/delivery regressions, acceptable
held-out routing errors with uncertainty reported, and reduced unnecessary
activations and total tokens/time per completed player-turn including classifier
overhead. Leave active saved games and the default policy unchanged until these
gates are met.

Classifier latency/usage/fallbacks are separate from player activations in
`telemetry/accounting.sqlite3` (`classifier_calls`). These accounting records
survive rewind; a cancelled or lost network response can leave billed usage
unknown, so they are not a billing statement. Reachable memory records use
`routing`, `delivery` and `guidance` categories for covered events, dispositions,
overrides, queue delay, advice exposure and same-revision action overlaps.
`experiment_metrics.summarize(memory)` reports reachable outcomes only. An action
match does not establish that advice caused the action. Incoming event counts
use each event's effective disposition, separately from routing-decision counts,
so coalescing does not count the same event twice.

The [advisory triage trial](../archive/reports/Decisions-triage-trial-2026-10-14.md)
records seven live synthetic-case calls. The routing comparison above adds
live Decisions requests on frozen historical and authored inputs; matched-game
behavioral evaluation remains pending. Do not interpret passing contract tests as evidence
of reduced chatter or stronger play. Compare unnecessary messages, activations,
missed/delayed actionable requests, enacted negotiations, latency and total usage
including classification. For guidance compare development, stalled orders and
casualties at matched horizons, not score or recommendation acceptance alone.
