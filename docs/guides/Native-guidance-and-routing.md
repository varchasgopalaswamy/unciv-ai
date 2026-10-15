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
key. The classifier is `gpt-6-luna`, policy `diplomacy-routing-v1`.

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
requires confidence at least 0.90 to delay/omit; refusal, malformed output,
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

## Evaluation and remaining rollout gate

The additional seven-case advisory fixture is synthetic and provenance-labeled.
Run it offline without making calls:

```sh
uv run python -m civ_agent.advisory_replay tests/fixtures/decisions-advisory-replay.json
```

Supply `--responses PATH` for saved real answers, or `--live-broker` with the
authenticated local broker environment to use that player's existing allowance.
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

Validate the replay harness without network access:

```sh
uv run pytest tests/test_diplomacy_replay.py
uv run python -m civ_agent.diplomacy_replay tests/fixtures/diplomacy-routing-replay.json
```

Without responses, the report marks classifier cases missing; it does **not**
claim model accuracy. Supply `--responses PATH` for a JSON mapping from case IDs
to captured Decisions response envelopes. An explicit `--live-broker` instead
makes paid requests through the configured broker. Its output includes the
response mapping for later offline replay. Review labels and actionable-request
deferrals before launching the four matched variants.

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
records seven live synthetic-case calls. Live diplomacy replay and matched-game
behavioral evaluation remain pending. Do not interpret passing contract tests as evidence
of reduced chatter or stronger play. Compare unnecessary messages, activations,
missed/delayed actionable requests, enacted negotiations, latency and total usage
including classification. For guidance compare development, stalled orders and
casualties at matched horizons, not score or recommendation acceptance alone.
