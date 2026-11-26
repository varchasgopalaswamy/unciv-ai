# Pydantic reuse in the local runtime

Pydantic AI owns agent execution. The application uses programmatic hand-off in
one async controller per game: fixed Python functions choose roles and a standard
`asyncio.TaskGroup` runs independent specialists together. The fixed game protocol
does not need a model-written workflow or a second persisted graph framework.

| Responsibility | Maintained implementation | Application responsibility |
| --- | --- | --- |
| Model/provider protocol | Pydantic AI models and provider clients | Choose the configured model and verify its capabilities before use. |
| Tools and structured output | Pydantic AI typed tools, `ToolOutput`, validation and `ModelRetry` | Bind native schemas and reject incomplete role work before readiness changes. |
| Deferred Economy waits | `HandleDeferredToolCalls` and typed deferred results | Reuse the same Economy work and provide authenticated settlement. |
| Role concurrency and joining | Python `asyncio.TaskGroup` | Define dependencies, ownership, reopen conditions and the native handoff barrier. |
| Concurrent model requests | SDK `ConcurrencyLimiter` and `ConcurrencyLimitedModel` | Share one bounded pool across the game's role and report agents; retain native job admission. |
| Long context | Harness `TieredCompaction`, `ClearToolResults`, `SlidingWindowCompaction` | Preserve current native facts and required recent exchanges. |
| Prompt caching | Pydantic AI `Caching`, Harness `WarnOnCacheBusts` | Assemble stable public prefixes and log safe cache evidence at WARNING. |
| Context and request telemetry | Harness `ReportContextUsage`, SDK instrumentation and OpenTelemetry | Bind actual run ownership and export only allowed measurements to Phoenix. |
| Authored notes and plans | Harness Memory and Planning | Scope player/role ownership and validate typed CityPlan/Strategy documents. |
| Agent history | Harness `StepPersistence` and `SqliteStepStore` | Link native operation IDs and verify ownership for private exports. |
| Usage and limits | SDK `RunUsage`, `UsageLimits` and native host accounting | Preserve game-wide spend and the original clock across attempts. |
| Provider waits | SDK error types and `RetryModelRequest` | Apply bounded delays, publish role state and keep the configured provider/model. |
| OAuth renewal | Pydantic AI subscription login and Authlib | Keep renewable credentials in the dedicated broker and issue scoped access credentials. |
| Native host protocol | Official MCP client and pooled httpx2 transport | Authenticate player identity, reject redirects, enforce visibility and reconcile uncertain native effects. |
| Boundary data and SQLite | Pydantic and SQLAlchemy Core | Define native domain records, ownership and mutation rules. |
| TLS material | cryptography | Maintain private files, stable identities and atomic provisioning. |
| Native legality and receipts | Unciv and the native JVM host | Prepare exact requests and reconcile uncertain native outcomes. |

## One role completion

The final `finish_role` structured output contains both the report and the role's
completion fields. It is the agent's terminal output tool. Code checks current
native requirements and only then accepts completion. A definite rejection is
returned through `ModelRetry` so the agent continues within the same run and
request limit. There is no later `finish_report` model request. Construction
escalation and native-legal Main deferral remain explicit structured choices.

## Context and cache handling

Role and report agents use the same capability composition in
`runtime/capabilities.py`: provider error sanitization, StepPersistence, caching,
cache warnings, provider wait policy, compaction and context reporting. Role-only
memory, Planning and deferred tools are added to that composition rather than
creating another agent loop. Planning runs after compaction. An actual overflow
retry reapplies its maintained reminder to the reduced request. `ReportContextUsage`
emits the SDK's context estimate and whether its window size is resolved or a
fallback; it does not promise an exact token count.

The runtime uses maintained Harness compaction rather than a custom transcript
summarizer. `TieredCompaction` first clears older tool results, then applies a
sliding window while retaining the required first user message and recent complete
exchanges. This uses no additional model inference. Canonical authored state and
fresh native observations retain durable facts outside the active message window.

A context-window rejection can trigger the supported compaction path and retry
only if it reduces the request. An irreducibly oversized request remains visible
as a waiting condition rather than repeatedly sending unchanged input.
`runtime/preload.py` assembles a versioned common public instruction block first,
then the role mission and enabled public strategy/civilization references. Current
observations, private memory and role tasks remain separate user input. Guides
are frozen instruction blocks rather than an `optionalReferences` user field.
Tools use a deterministic order with common tools first, without granting any
additional tools or commands.

For OpenAI models, a stable `openai_prompt_cache_key` is derived only from the
selected public model name and the common public prefix. Matching prefixes can
therefore share routing across players, turns and roles. Game IDs, native jobs,
private observations and conversation IDs do not enter that key. Session and
thread identity still belong to the original SDK conversation. OpenRouter keeps
its existing provider routing and the SDK's portable caching configuration.

Subscription caching remains an implicit provider optimization. The pinned SDK
accepts the explicit key while retaining conversation-derived session headers,
but a key is a routing hint: it does not create a cache entry, reserve a cache or
guarantee reuse. Only provider-reported cached tokens establish an observed hit.
`WarnOnCacheBusts` reports an established hit collapsing; an unknown retention
window can also mean expiry. Every emitted cache warning reaches WARNING logs
using fixed text, the reason and numeric token counts, never arbitrary warning
text or prompts. No extra model request is used to summarize or diagnose caching.

The shared request limiter is attached to models, not whole agent runs. A Main
tool waiting for Economy does not hold a model-request slot needed by Economy.
SDK queue limits, cancellation and waiting spans replace a second local semaphore;
native leases, total game limits and permission checks remain authoritative.

## StepPersistence is evidence, not orchestration

Harness StepPersistence supplies SDK-owned messages, snapshots, append-only events
and tool effects. `PlayerPersistence` adds player scope and native operation links.
The same canonical metadata binds SDK runs and persisted events to their agent,
role, native job, source/target review turns and review cycle when present, plus
the actual selected provider, model and reasoning effort. Reader code must not
infer an older attempt's model from the latest request.
Its frontier capture supports diagnosis around interrupted model/tool work.
The SQLite store is retained after a crash, but it does not resume the controller
or prove that a native effect failed. Message snapshots are not graph checkpoints.
Native receipt reconciliation remains application/host work.

Harness Memory owns notes and atomic authored operations. Planning uses its native
SQLite store for full task CRUD, bulk status updates, subtasks and dependencies.
Each role writes only its own plan; `read_role_plan` exposes other roles' plans as
read-only context. Typed city-plan and strategy tools apply direct guarded Memory
operations, retaining its operation receipts and validation without a separate
prepare/apply RPC. A plan saying that a task is complete never substitutes for
native validation.

The current local runner invokes these authorized services in process. The unused
worker HTTP gateway has been removed; it is not retained as another production
transport or a reason to keep transport-only tests. Logical paid-task identities
live in `orchestration/task_identity.py`, separate from old recovery inventories.

## Fixed local routing and advice

Optional Decisions routing and advice use ordinary instance-local async calls.
The separate broker owns immutable request identities and spending. Repeated calls
reuse the same broker result rather than generating another paid identity.
Outgoing delivery uses the original native request ID and persists its exact
payload plus code-owned task/run admission before sending. A lost reply is
settled through the host receipt. An unknown outcome is not permission to resend.
No generic scheduler, fake workflow client or activity-global service is used.

## Restart boundary

A process crash stops for inspection; explicit resume creates a fresh agent
attempt at the latest native position. Committed actions, messages, authored
state, receipts and spent usage remain. Per-attempt allowances refresh while the
original deadline and total caps stay in force. One physical launch grants one
resume authorization. Expected provider waits follow their bounded policy without
requiring an operator restart.

Temporal, its Pydantic integrations, database services and developer execution
commands are removed from this runtime. Older saves remain readable archives;
there is no converter or fallback executor. Pydantic Graph, Harness SubAgents and
DynamicWorkflow are not required for the chosen fixed programmatic hand-off.

See the [Python architecture](Python-architecture.md),
[failure boundaries](Failure-recovery.md) and [container guide](Containers.md).
