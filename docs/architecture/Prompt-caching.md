# Prompt caching for player jobs

## Context layout

Scheduled gameplay and communication use separate messages:

1. **Developer instructions:** stable job workflow, discovery/recovery guidance,
   permitted core tool definitions and prepared-read interpretation instructions.
   This message contains no player identity,
   turn number, private memory or observation values.
2. **User input:** the optional [strategy guide](../guides/Strategy-guide.md),
   explicitly labeled as reference suggestions, then freshly read capabilities,
   the current request, current observations and communication, followed by
   selected historical strategy, handoff and knowledge.

The service passes stable instructions through `developerInstructions`. Core
schemas come from the current permitted gateway catalogue. Canonical JSON keeps
key order stable; schema changes change the prefix. Gameplay and communication
have different instructions and tools. Stable reference material precedes all
changing user data, preserving prefix reuse without promoting strategy advice to
developer instructions. Startup metadata identifies this as layout version 3.

Game data remains at user priority, including scenario text and other players'
messages. Preparation performs normal evidence/accounting reads for every
activation. It does not reuse an old game snapshot. Interactive clients receive
prepared definitions before memory while retaining their normal client context.

Scheduled jobs omit volatile environment/permission prose, project documents and
unrelated skill/plugin, app and collaboration instructions. Actual job directories,
credentials, sandbox settings, approval policy, visibility checks and fresh
conversations remain configured. See [agent context](Agent-context.md).

## Current local runner

The Pydantic AI runner assembles stable instructions and role-scoped tool schemas,
then supplies prepared observations as user input. For nonlegacy policies,
`ProviderCaching` fingerprints
the SDK-prepared instructions, tools, output contract and thinking setting. Its
body cache key is bounded by verified account, provider, endpoint and actual model.
Changing effort therefore changes our prepared key; changing user observations
does not. Provider reuse is measured separately from those fingerprints.

Subscription requests use an independent stable `session-id` by default,
scoped to verified account/provider/endpoint/serving model. There is no feature
toggle; legacy execution policies also receive stable affinity. Retention, body
cache keys and history keep their existing behavior. `thread-id`
and `x-client-request-id` come from the current request's logical conversation,
including its first request. Explicit headers win regardless of case. Unknown
accounts cannot share affinity across runs.

`tests/test_execution_policy_cache.py` checks account/model/endpoint isolation,
caller overrides and actual first/subsequent HTTP headers using `MockTransport`.
It also verifies that the default runner applies affinity to legacy jobs without
rewriting history or changing their body cache key.

## Historical client evidence

Stable ordering makes a reusable prefix possible; it does not guarantee a cache
hit. The September 21 probe of Codex 0.155.1 found no supported explicit
cache-breakpoint setting in its app-server schema; injected breakpoint fields
were dropped. The implementation preserves the client's existing caching mode.
Recheck this capability when changing the client version.

The historical capture test used separate processes/workspaces and a local canned
model endpoint. It checked stable tool/developer content before changing user
data, omission of local file text, and unchanged model/sandbox settings. That old
test is no longer present; use the current runner tests above.

## Measurement

Native SDK histories retain request instructions, tool definitions, messages and
responses. Cache metadata records full/reusable prepared-prefix fingerprints and
byte counts, verified account scope, requested retention and reported usage.
These sizes describe exposure; they are not tokenizer measurements or cache hits.

Compare opening and subsequent requests separately, and match actual selected
effort. Retain instructions, schemas, observed usage, request counts and elapsed
time when investigating reuse. Use provider input/output/cache-read counts for totals; tool replies are future
input and reasoning is already included in output. Missing cache-write counts
and subscription dollar savings remain unknown. Fewer reads, different decisions
or extra repair requests can change totals independently of caching. See
[telemetry](../reference/Telemetry.md) for token categories and overlapping times.
