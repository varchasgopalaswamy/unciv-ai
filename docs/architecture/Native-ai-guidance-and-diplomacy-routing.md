# Native AI guidance and diplomacy routing

Status: opt-in implementation with local regression validation. A bounded live
[memory/review triage replay](../archive/reports/Decisions-triage-trial-2026-10-14.md)
is recorded; city-choice gameplay evaluation and matched-game behavioral acceptance
remain pending. See the
[rollout guide](../guides/Native-guidance-and-routing.md) for configuration,
coverage limits, credentials and evaluation instructions.

## Summary

Native guidance remains independently available. The shared `decisions_enabled`
switch controls routing, memory/review triage and city focus/construction questions.
Native suggestions go directly
to the gameplay model under the independent `native_advice_enabled` switch. See
[Decisions advisories](Decisions-advisory-layer.md) for city choices, memory, budget
accounting and early-review behavior. The native selector and diplomacy-specific
mechanics below continue to apply.

1. A native AI guide covering economy and unit actions, automatically included
   at turn start and refreshable through a read-only tool.
2. A diplomacy scheduler using the OpenAI Decisions API to classify incoming
   and outgoing ordinary chat as immediate, batchable, or unnecessary.

The player retains control over gameplay and can override an omitted or queued
outgoing message. Ordinary batches wait at most 30 seconds, subject to existing
pause and recovery barriers. Active benchmarks retain their current configuration.

## Native AI guide

- Reuse native decision logic in Kotlin. Extract read-only selection functions
  from native construction, spending, citizen, and unit automation. Native
  execution and the guide share those functions; the host does not implement a
  competing set of gameplay heuristics.
- Cover construction, purchases, upgrades, citizen assignments, Worker
  improvements, Settler and Scout movement, military movement, attacks,
  bombardment, healing, and promotions. Leave declarations, peace, trades, and
  other diplomatic decisions to the player. Explicitly report unsupported
  specialized actions.
- Constrain the planner's inputs to permitted player knowledge. Supply owned
  assets, current visible information, retained permitted observations, and
  legal action previews through a read-only interface. Native routines requiring
  unavailable information return an unavailable recommendation. Do not run
  unrestricted full-world autoplay and then filter its output.
- Preserve the player's actual costs, difficulty modifiers, construction
  progress, and production restriction. Select native AI preferences without
  changing the player's controller type. Report `continue current construction`
  when that is what native automation would do; do not imply it independently
  reconsidered every queued investment.
- Return next-action recommendations from the current position, without
  simulating combat outcomes or discoveries. Recommendations sharing a budget,
  destination, or target are alternatives requiring re-evaluation after
  execution. Explanations come from native decision branches and observable
  facts.
- Add a read-only `native_advice` tool with optional city and unit filters.
  Results include the epoch, turn, revision, recommendations, command arguments,
  costs, prerequisites, and coverage limitations. Include up to eight prioritized
  recommendations in startup context; the tool exposes the remainder.
- Cache by player and observed revision. Compute from immutable inputs outside
  the live-game mutation lock, with a five-second deadline and cooperative
  cancellation. Timeout or failure yields unavailable advice and ordinary
  gameplay continues. Existing command validation remains authoritative.

This requires engine work to separate selection from execution. It is more
substantial than wrapping the existing `AUTOPLAY` command.

## Decisions-based diplomacy scheduling

Use `POST /v1/decisions` with `gpt-6-luna` and a named choice question. These
questions classify message timing and delivery while preserving the original text. See the
[OpenAI Decisions API documentation](https://developers.openai.com/api/docs/guides/decisions).

### Classification inputs and behavior

- Classify each exact-audience thread using pending messages, recent exchanges,
  already queued drafts, relevant player-authored objectives, and pending
  proposal metadata. Keep private threads separate and treat message contents as
  untrusted, attributed claims.
- Incoming choices are `respond_now`, `batch`, and `no_separate_response`. The
  last choice suppresses a dedicated communication activation; the message
  remains available in history and gameplay context.
- Outgoing choices are `send_now`, `batch`, and `omit`. Omission targets redundant
  acknowledgements, repeated settled positions, and unchanged reports. It must
  not enforce honesty, cooperation, or compliance with social promises.
- Initially require confidence of at least 0.90 to batch or omit. Lower
  confidence, refusal, malformed output, timeout, or service failure falls back
  to ordinary delivery or activation.
- Binding proposals, consent decisions, native diplomatic actions, messages
  involving human players, and ordinary text associated with an active binding
  negotiation bypass classification.

### Batching and player control

- Start the 30-second deadline with the oldest pending item; subsequent arrivals
  never extend it. Urgent arrivals trigger immediate processing of their thread.
- Preserve original outgoing text and audience. Flush messages together through
  ordinary host sends; do not introduce another model to rewrite or summarize
  them.
- Extend `chat_send` with `delivery="auto"|"immediate"`. Automatic sends return
  an explicit `sent`, `queued`, or `omitted` disposition and a delivery identifier.
  Only actual host receipts establish delivery.
- Add `chat_delivery` for status, immediate delivery, or cancellation of an owned
  queued or omitted message. Overrides retain the original text and audience and
  cannot duplicate an already delivered message.
- Include outstanding delivery statuses in `chat_read`. Update tool descriptions
  and receipt handling to explain these semantics; retain the existing strategic
  communication instructions and impose no message quota.

### Scheduling integration

- Place incoming classification before the main communication model is started.
  Replace the blanket once-per-turn communication gate with per-event routing and
  completion state, so a new urgent proposal can receive attention during the
  same turn.
- Classify only changed input bundles, coalesce arrivals, and allow one
  classification request in flight per player. Never repeatedly classify an
  unchanged acknowledgement.
- Use a five-second API timeout with automatic retries disabled. Make requests
  outside database transactions and host locks. A temporary circuit breaker
  falls back to existing communication behavior during an outage.

## Persistence, interfaces, and rollout

- Store routing decisions, covered event IDs, deadlines, and outgoing drafts in
  the existing versioned record and metadata machinery. Keep unsent drafts
  separate from unsettled host requests, since current checkpoint verification
  requires recorded host requests to have settled receipts.
- At delivery, create the normal durable host request and reconcile uncertain
  outcomes through its existing receipt lookup. Delivery requires no additional
  player-model invocation.
- Checkpoint barriers suspend delivery and discard unfinished classification
  work. Restart restores pending drafts and deadlines. Rewind restores only the
  queues and decisions reachable from that checkpoint; results from a discarded
  future cannot influence the resumed game.
- Maintain separate routing, model-delivery, and gameplay-read bookkeeping.
  Classifying a message must not falsely mark it as read by the player or skip
  another thread.
- Add `native_advice_enabled` and `decisions_enabled`, both defaulting to
  false when absent. Record effective settings, native-guide version, classifier
  model, and routing-policy version in benchmark metadata. Preserve existing
  checkpoint and manifest compatibility.
- Run Decisions requests through a trusted Python broker using the existing
  authenticated local-socket pattern, with a separately configured OpenAI API
  key. Keep the key outside player containers and workspaces. Missing credentials
  reject a newly enabled configuration before launch; runtime outages use the
  fallback behavior above.
- Prepare baseline, guide-only, decisions-only, and combined benchmark
  configurations using matching seeds, assignments, and player settings. Keep
  host changes on its current branch and engine changes independently reviewable
  under the repository's engine workflow.
- Record classifier usage and latency separately from gameplay and communication
  usage. Record advice exposure, action matches, overrides, queue delays,
  omissions, and fallback reasons without claiming that an action match proves
  the player followed advice.

## Validation and acceptance

- Native correctness: compare extracted selectors with native execution on fully
  observable fixtures; verify recommendations preserve costs, construction
  progress, and the conversion restriction.
- Information isolation: change hidden units, cities, resources, and enemy
  internal state while holding permitted observations fixed; recommendations
  must remain identical. Verify advice never changes live saves, caches,
  identifiers, or random state.
- Advice lifecycle: cover automatic startup delivery, filtered refresh, stale
  revisions, conflicting recommendations, unsupported actions, and timeout
  fallback.
- Diplomacy fixtures: cover acknowledgements, repeated victory-status reports,
  answered questions, useful new information, changed terms, urgent requests,
  bluffs, and ambiguous promises. Verify protected traffic always bypasses
  classification.
- Delivery correctness: cover truthful queued and omitted receipts, overrides,
  cancellation, exact audiences, original text, concurrent arrivals, duplicate
  calls, and the non-extending 30-second deadline.
- Recovery: interrupt before classification finishes, after draft persistence,
  during sending, and after host commit but before receipt persistence. Restart
  and rewind must produce no lost committed messages, duplicate deliveries, or
  leaked future decisions. Existing saved-game resume checks must still pass.
- Behavioral evaluation: replay a labeled sample from the audited games, then
  compare the four new benchmark variants. Measure unnecessary messages,
  communication activations, actionable requests missed or delayed, enacted
  negotiations, total latency, and total model usage. Evaluate the guide through
  development, stalled orders, and casualties rather than score or recommendation
  acceptance alone.
