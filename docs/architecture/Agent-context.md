# Agent context and tools

This describes the current model-facing interface. Earlier numbered efficiency
plans and their measurements are in the [archive](../archive/README.md).

## Startup and discovery

Each scheduled activation starts a conversation and retains that thread across
structured decisions, including receipt-based continuations. The service prepares permitted
observations through the same gateway used for explicit reads, selects private
memory using the role and current subjects, and then reads the chosen records. Received message participants
also select relevant notes; prose is never mined for implied facts. Selection
keeps accepted strategy and relevant city plans alongside a bounded supporting
notebook and the dispatch snapshot's contribution boundary. Omitted pending
contributions and review notices have counts and `memory_search` retrieval
instructions; omission never acknowledges or resolves them.
Selection freezes the current versions of eligible dispatched records, including
review notices; startup memory reads deliver those exact versions. Concurrent
notebook edits cannot enlarge the prepared context. Later explicit reads obtain
the latest versions.
A cached record is not treated as delivered
merely because it is in the service database.

Gameplay prepares `player_turn`, `player_capabilities`, `victory_progress` and
`chat_read`. Incoming events appear only in the chat read, not again in private
memory context. Gameplay chat omits economic/scenario context already covered by
the overview; communication prepares `chat_read(includeContext=true)`. The stable decision schema contains the complete permitted catalogue; startup
text supplies consequences, prerequisites and observation guidance.

Every key required by the selected strict-schema variant must appear. To leave
a native optional argument unspecified, send `null`; otherwise supply its value.
The host removes null optional arguments before dispatch. For optional arguments that themselves
accept null, `{ "value": null }` explicitly clears the value. Optional memory
patch fields set to plain `null` remain unchanged. Instructions request compact
JSON and `recoverBaseline: null` for ordinary inspections.

Communication starts with `view="communication"` selected for subsequent
`player_turn` reads. Its opening still uses the small social context rather than
preloading an empire overview. The communication view retains empire constraints,
native relationships, pending decisions, threats, uncertainty, every owned city
and unit in summary form, and visible foreign forces and cities. Settlement and
construction catalogs, recruitment menus, city management details and old progress
logs remain available through focused reads or `view="full"`. Omitting `view`
preserves the selection; switching it resets the delivered overview baseline.
Gameplay commands shown in permitted observations remain unavailable to the
communication role, with ordinary role ownership identified separately.

Communication also prepares exact `trade_read` results for up to two pending
shared offers associated with new event proposal IDs or participants. Explicit
proposal references take priority. Old conversation excerpts, own message echoes
and the full audience of a public message do not trigger extra trade inspections.
Every unprepared offer retains its inspection command in the pending index.
Successful preloads establish the same inspected version as an explicit read;
failed reads do not authorize decisions, and changed terms require reconsideration.
The decision schema always includes the permitted trade operations.

The existing bounded coordination snapshot supplies active accepted war goals to
communication. Pending goal revisions have counts and exact retrieval commands;
they do not replace accepted objectives. Oversized or omitted episodes retain
history counts and `war_goals` retrieval. The read operation remains in the stable schema even when no episode is active. Native peace can end an episode without
rewriting its authored history. Communication startup omits instructions for
gameplay-only command discovery, role completion and internal requests.

Managed Main, Economy and Military start with `player_turn(view="main")`,
`view="economy"` and `view="military"` respectively. These projections apply to
startup, subsequent reads, command updates and recovery. Main gets empire choices,
diplomacy, victory progress, civilians and escorts, city summaries and specialist
coordination. Economy gets selected cities, economic constraints, worker activity,
threats and current intent. Military gets its units, bombardment, opponents,
protection needs, supply constraints and active operations. Detailed options are
preloaded only for entities the receiving role can act on.
Economy also preloads citizen and production details for up to two selected
cities with an empty queue, starvation or unassigned citizens. Existing unit
and construction preloads remain; ongoing city work keeps its overview, and
other details remain explicitly readable. Worker-only passes preload no cities.

Every view retains pending decisions, relevant threats, native constraints and
uncertainty. `roleBrief` identifies omissions and focused retrieval tools;
`view="full"` retains all permitted observations. Missing information is unknown,
not evidence of absence. Ordinary role ownership checks also generate command
labels: metadata sits beside copyable command arguments and identifies direct
actions, requests to another role and Main's existing `city_override` for cities.
Economy receives actionable details for ordinary unescorted workers. Assigning
escort duty transfers both units to Main; release returns their ordinary roles.
Role concurrency, native advice and gameplay legality keep their existing rules.

All scheduled Main, Economy, Military, Communication, strategic review and peace
consultation `turn/start` calls carry an `outputSchema`. Models choose an envelope
with `inspect`, `act`, `finish`, `wait`, or phase-specific `submit_report`. Read and
action batches contain 1–12 already-decided operations; waits and completion stand
alone. Command variants, inspections, memory amendments, coordination and diplomacy
arguments derive from canonical typed contracts. There is no program or arbitrary
JSON argument escape. Schema objects are closed; wire optionals are explicit nulls.
For nullable optional values, `{value: null}` clears and null omits; defaults are
applied by canonical validation. Shared definitions and removed titles reduce schema
size without removing constraints or descriptive gameplay guidance.

The host validates the complete response before dispatching any child. It executes
ordered actions through the existing gateway, stops on failure, uncertain outcomes,
changed ownership/prerequisites or required observations, and identifies unexecuted
children. Host observations inside a batch cannot grant unseen preview credit.
Receipts return through retained `toolOutput` on the same thread. No operation
selection/discovery request is required and state-dependent options stay in reads.

Definite input and permission rejections return unapplied error receipts. This
includes unknown delivery IDs, unavailable tools or role coordination, and native
diplomatic actions without a current observation. Chat-history queries advertise
the store's 200-character limit. Agreement reads overtaken by a newer lifecycle
event return `AGREEMENT_CHANGED` with a fresh-read request, preserving the newer
evidence. Job/epoch fences, credential conflicts, storage failures and uncertain
execution still stop work; they cannot be converted into a new action attempt.

Scheduled Codex processes use game decision base instructions and disable execution,
MCP, plugins and skill injection. Inherited MCP servers are explicitly disabled on
thread start/resume; an empty configuration table alone does not erase them.
A secret loopback provider gate preserves Codex authentication and forwards each
generation with its exact strict schema, `tools=[]` and `tool_choice="none"`.
The gate normally permits one generation attempt per host decision. A disabled-tool
continuation, connection retry, or stream retry cannot spend a second request
inside that decision. Blocking fails the activation without dispatching an action;
it does not initiate another repair turn. One automatic compaction attempt is
separately allowed and counted. Only completed, validated decisions reach the game.
Incomplete/refused output cannot dispatch.

Before the current input reaches the context ceiling, managed conversations
refresh at a completed decision boundary once input reaches 80% of the reported
window. The old process closes before a fresh thread receives current permitted
facts and the original task. The job, native action journal and workspace request
budget remain; preview credit and delivery baselines refresh. Usage accumulates
across all threads, and incomplete trace coverage stays marked incomplete. This
avoids relying on automatic compaction requests that omit the required schema.

A terminal HTTP 400 containing the provider's specific prompt-policy rejection
can receive one unchanged retry per activation workspace. The gate reserves this
extra attempt durably before forwarding; it also spends the ordinary request
budget. The rejected response is never delivered as model output. The schema,
instructions, conversation and disabled tools remain identical. A second rejection
ends the affected work; specialist restarts and strategic-review retries cannot
multiply the allowance. Other HTTP errors, model-authored refusals, partial
streams and uncertain execution do not qualify. This transport exception does not
grant another host decision or alter confirmed game receipts.

The gate uses HTTP streaming through a custom Codex provider configuration with
`supports_websockets=false` and automatic request/stream retries disabled. OpenAI
uses an internal `civ_openai` transport alias with Codex-managed OpenAI authentication
and the same model catalog; durable settings continue to name `openai`. Thread
start and resume both select this configuration. The built-in provider's model
metadata can prefer WebSockets despite disabled feature flags; its rejected probes
previously caused five retries before HTTP fallback. Real Codex tests use Luna and
Terra and reject any WebSocket probe, including after a process restart.
Threads, receipts, models, priority and reasoning settings remain unchanged. Provider bodies
and credentials pass through memory and are not logged by the gate. The existing
Codex trace and usage handling retain their own behavior.

Each activation workspace has a durable ceiling of 128 provider attempts. For an
isolated evaluation, set `CIV_MODEL_REQUEST_BUDGET` to an absolute SQLite path and
`CIV_MODEL_REQUEST_LIMIT` to the total allowed attempts across all roles and
processes. Reservations precede forwarding, survive restarts and are never refunded
after uncertain execution. Reusing the file with a different limit fails.
`tests.live_strategy_positions --max-model-requests N` creates a shared suite
budget and refuses variants without the gate. This is distinct from the admitted
job limit and the periodically checked token threshold. Failed attempts and
compactions spend the request budget; blocked continuations do not reach the
provider. Reserving a request that is interrupted before transmission may count
an unsent attempt conservatively.
These limits cover Codex generation and compaction. The separate Decisions
backends retain their existing closed-choice contracts and cost budgets.

Independent memory edits, operation notes and Economy requests continue in one
batch. Revising the same memory record or operation again requires its receipt;
newly created memory cannot authorize an unseen citation. Ownership transfers,
changed accepted war goals, failed actions and stale observations retain their
execution fences. No intermediate host observation grants inspection credit.

In managed gameplay, each role submits `finish`; the host advances the native
turn after all required roles finish. See [economy management](Economy-manager.md).
Final review sends unresolved construction choices to Economy once. If its
completed pass leaves the same native choices unresolved, Main receives the
exact pending choices and decides whether to request specific work, take a city
override, or explicitly defer where native rules permit. The controller does
not repeatedly reopen Economy or select deferral itself.
Managed catalogues omit native turn advancement. Optional `memory_update`
remains available in the schema during play. Scheduled reviews run concurrently at a shared
round boundary after all players finish; gameplay and communication wait for the
reports. Main receives a compact dated review summary; specialists receive current
accepted strategy and relevant requests. `history_read(turn=T, includeReview=true)`
retrieves the exact completed review and its evidence references. Reviews compare
concrete plans and retain their original recommendations in history.

Gameplay can initiate useful diplomatic contact without an incoming message.
Communication jobs pursue the civilization's objectives through questions,
counteroffers and further contacts when worthwhile. Both receive dated review summaries
and relevant private notes. Negotiation guidance covers cooperation, gathering
information, threats, concealment, bluffs and breaking nonbinding promises,
weighing incentives, relative gains, credibility and retaliation. Silence is
valid; jobs finish when awaiting a reply or further communication has little value.
Existing message events wake subsequent exchanges; no periodic diplomatic job
or mandatory message count is added. Private plans enter the player's context,
not outgoing messages. Useful unresolved questions and intentions use the
existing notebook; routine receipts and acknowledgements need no duplicate notes.

The command schema contains exact role-filtered variants with common action
metadata outside the command union. Server validation and native legality checks
still apply. Decisions backends retain their separate closed-choice contracts;
they are not counted as strict generative responses.

Tool catalogues follow the job's permissions. The older `player_summary`,
`owned_cities` and `free_great_person_choice` reads are not exposed. Capabilities list available queries and commands directly,
without a hash wrapper, protocol version or recovery/coverage flags.
`player_turn.units` is the single overview unit roster; its military entries
include strength and role. Tactical review adds risks and opportunities, while
turn review retains only its confirmed-action count. Chat owns versioned social
promises and trade summaries; the overview does not repeat them.

Inactive unit capabilities and empty optional lists are omitted; the full view
retains every owned unit, while role views select relevant units and identify the
omitted roster. City health/defense and worker job time are included.
`changesSincePreviousTurn` compares the previous gameplay observation with the
opening observation, then remains stable throughout that turn. It references
existing notifications/reports instead of repeating their contents, and lists
new construction names separately from `availableSinceChoice`. Missing owned
units and cities retain their names and last known positions. They are not
automatically described as destroyed; consumption/gifting can also remove them.
Worker-job comparisons include both job and location, so finishing a Farm and
starting another elsewhere still reports the old job. Unconfirmed job endings
are not called completions.

The overview also prepares research/culture progress, city yields and specialist
use, city-generated Great Person progress, compact resource balances, city-state
benefits, exploration frontiers and expansion resources/happiness estimates.
`civilizationProfile` supplies the selected ruleset's own unique ability and
unique units/buildings from the opening turn, including future unlocks, mechanical
conditions, inherent promotion effects and comparisons with ordinary replacements.
The same profile enters frozen strategic reviews. Current research updates its
technology conditions and obsolescence; unchanged profiles are omitted from action
deltas. Native personality settings and preferred victory types are not included.
Research is limited to eight candidate technologies, exploration to four frontier
groups, and settlement to three separated candidate centers. Full catalogues and
maps remain available through focused reads. Identical settlement budgets are
grouped, and unchanged sections are omitted from command deltas. These summaries
use the existing startup reads; they add no opening model/tool requests. See
[compact observations](../reference/Compact-observations.md#opportunities-at-turn-start)
for visibility boundaries and estimate assumptions.

Explored map tiles retain observed terrain, features and infrastructure across
fog and checkpoint reloads. `observedTurn` dates those facts; null mutable fields
mean the host has no permitted observation, including explored tiles in a loaded
save that have not yet been seen by the host. Improvement names retain Unciv's
native remembered information, including permitted remote discoveries; pillage
state remains unknown when the host has not observed that improvement.
Native visibility callbacks capture intermediate movement and automation steps,
so discoveries remain known even if they are fogged before the next host read.
Explicit distant reveals have a separate callback: a city-state's revealed
capital becomes remembered terrain/city information without exposing units or
granting ongoing sight.
New resource-revealing technologies update explored settlement resources and
scores using remembered terrain and infrastructure, without refreshing hidden
ownership or development. The processed technology set survives checkpoints.

The settlement shortlist includes only sites that pass the native rules on explored
tiles, even when an undiscovered city causes the restriction. Hidden city identities
remain concealed. It selects spaced sites by score. Resource groups
separate unclaimed potential, overlap with existing cities, and foreign ownership.
Candidate happiness budgets use native calculations on a detached map of known
terrain. Missing surrounding tiles qualify site-dependent building requirements
without suppressing city/population costs or the whole building list.
Economy receives `availableWonders`, a names-only list of wonders available to
queue in at least one city. `construction_details(target=<city>, names=[<wonder>])`
retrieves costs, benefits, prerequisites and estimates on demand. The full view's
`wonderOpportunities` also retains queued and blocked wonders.
Compact completion estimates omit zero yield changes (including formatted zeros);
omitted changes mean zero at displayed precision. Named construction reads retain
the complete native estimates.

The 2026-09-27 deterministic comparison reduced prepared core definitions from
27,210 to 10,516 characters (61%). The saved France turn-143 overview decreased
from 68,878 to 59,794 characters (13%), including the new facts above. Omitting zero
yield changes accounts for 1,967 characters of that reduction. Its shortlist now
includes a confirmed eligible site alongside two uncertain sites. These are
serialized character measurements, not token counts or a live gameplay benchmark;
they describe the earlier prompt layout. Memory writing is now optional during
play; there is no post-turn writing schema or mandatory strategy write.

A [shared strategic review](Structured-memory.md#routine-strategic-review)
runs before the first move on turn 0, then after all players finish every Kth
round, starting at K (`--review-interval`, default 5). Reviews run concurrently
from each civilization's permitted view. Gameplay and communication stay paused
until all reviews have published advice or safely finished as unavailable.
Opening views include starting locations and abilities.
It uses the player's model and effort, a captured permitted position and planning
catalogues, and read-only frozen detail tools. Known foreign cities are summarized
with observed capital status and visibility; current defenses require visibility.
Private notes, old strategy, action rationales and model deliberation are excluded.
The reviewer cannot act, communicate or write
memory, and cannot query the advancing game.

New reviews record a private factual baseline of war relationships, native treaty
status and owned city/unit identities. Changed facts replace automatic
recommendations with a dated reconsideration notice and retrieval reference. The
notice persists across restarts until a new review; checkpoint rewind restores
the earlier boundary. Older reviews without baselines remain retrievable with an
explicit unknown-baseline notice. A factual change does not adjudicate an authored
assessment, retire accepted plans or constrain native-legal actions. Exact social
promises retain their existing nonbinding semantics.

Main checks the summary against fresh facts, chooses its own actions and can
reject the recommendation. Reports arrive before
the next own gameplay turn starts, rather than midway through its conversation.
Players need not copy them into a strategy entry. Only one review can be pending
for each civilization.
Relevant diplomatic initiatives can be marked acted, deferred or rejected with a
reason in the existing end-turn intent. This optional private history field adds
no model call and does not constrain gameplay.

Default chat includes effective promises and their latest pending revisions,
pending trades and active agreements. It retains exact terms and version IDs.
Active agreement summaries include current give/receive terms and expiry, without
repeating representation labels or cancellation explanations on every row.
Use `chat_history`, `trade_read` and `agreement_read` for selected earlier records,
`agreement_list` to find older agreement IDs, or explicitly request
`chat_read(includeHistory=true)` for the retained ledger. History is not deleted.

[Prompt caching](Prompt-caching.md) describes how stable workflow/schema text
precedes changing player context. The [strategy guide](../guides/Strategy-guide.md)
is optional and advisory. Startup reads and model-issued calls have separate
[accounting](../reference/Telemetry.md).

## Responses, focused reads and commands

The model adapter removes text blocks that exactly duplicate `structuredContent`
while retaining the MCP envelope, error state, metadata and distinct content.
Responses without structured data retain their original content. This normalization
does not drop warnings or imply that every response is only a bare JSON object.

Use focused unit/city sections, named research/construction details and local map
regions for the next decision. Options expose copyable command arguments and any
suggested detail read or required native preview. Routine detail reads and intent
annotations are optional. Discovery, action templates and prompts are guidance; native
legality, authority and observation checks still apply.

Successful commands return receipts and [state updates](Command-state-updates.md).
Deterministic changes preserve unchanged sections and identify added/removed
entities and stale inspections. The service maintains its snapshot internally;
the agent does not need a new overview after every successful command. A movement
preview expires with native revision changes, so unrelated commands should finish
before fetching the preview for the next movement action.

Another role's action fences the old observation until a fresh native read.
The service retains the previous facts for an exact comparison, preserving
unchanged projected detail inspections and invalidating changed ones. Native
previews and external reads still expire conservatively with revisions;
ownership transfers invalidate detail authority. This does not automatically
retry a rejected action or approve a choice made before changed facts arrived.
Economy and Military retain the same comparison baseline across their own
successful actions, allowing exact overview deltas instead of a full overview
after each command. Refresh failure still discards the baseline and keeps the
confirmed receipt.

Military can add `reviewAfter=true` to a `player_command` to receive a fresh,
paginated `militaryCompletion` with its successful receipt and state update.
This host option never reaches the native command. A failed action has no review;
a failed subsequent refresh retains the confirmed action and requires recovery,
never repetition. The returned review supplies the same basis and hold-decision
contract as `military_review`. That read is also available in `inspect_bundle`.
Completion-recovery replies already supply the first page; only remaining pages
or changed facts require another read.

Cached detail projections refresh only while a running reader or an active
strategic assumption needs them. Historical queries retain their last evidence
until explicitly read again. Region and section dependencies exclude unrelated
tiles and entity sections.

Native research/policy/victory/result and trade/agreement/chat reads also register their
queries for invalidation. They expire conservatively after an observed native or
social change; the service does not fetch them all again. Relevant expired reads
appear in `invalidatedDetails`, including on full reads and communication mutation
responses. Native research and policy responses retain their observed turn number.

## Compact payload delivery

Startup layout 12 describes the following formats. Native observations, legality,
inspection registration and durable action receipts retain their existing
checks. Compression affects delivery, not which facts the engine returns.
`player_turn` preserves the current view when `view` is omitted or `current`;
`view="full"` explicitly expands any focused role view. Recovery reads therefore
preserve the selected presentation. Structured observation and notebook reads
leave transport selection to the host. Their operation-level `recoverBaseline`
field accepts `missing` or `mismatched` to request complete inline recovery data;
null leaves transport automatic. Recovery does not change the selected content
or inspection freshness. The gateway maps explicit recovery to `delivery="full"`
and retains `delivery="auto"` for other clients. Sections, construction names, map format,
overview view and city scope independently select the information. The legacy
`full` boolean remains accepted but is omitted from advertised input schemas;
contradictory explicit recovery options are rejected.

New activations select their role's default view. Switching views resets delivery
baselines; archived history, existing checkpoints and exact replayed receipts stay
readable. Recoverable, unapplied commands require reconsideration against fresh
observations. Confirmed actions must never be repeated; uncertain execution must
be reconciled before retrying.

After a prepared conversation starts, an explicit `player_turn` still fetches
fresh native state, but may return `stateUpdate` instead of a complete root
overview. `unchanged` retains its data, adopts the new `stateId` and still applies
`invalidatedDetails`. A delta uses the same reducer as an action receipt. The
service chooses the smaller complete alternative. Startup, a new conversation,
view/scope changes and `delivery="full"` supply a complete overview. Clients
without prepared context retain full root responses until explicitly opting in
through a delivery option. Host review uses full data. A role resume uses compact
delivery only while the conversation retains its model-delivered baseline.
Host-only reads, missing baselines, context resets and view/scope changes require
full recovery. Replaying an old full receipt cannot restore this eligibility.
`apply_player_turn` consumes both root and update forms.

Projected detail reads (`unit_details`, `city_details`, `construction_details`,
`map_region`, `player_details`) have a `detailUpdate` with conversation `scopeId`,
`sequence` and `stateId`. `full` supplies the selected `data`; `unchanged` confirms
revalidation and retains it; `delta` includes JSON Patch operations against the
matching `baseStateId`. Preserve each query's baseline, ignore older sequences,
and request that same read with `delivery="full"` if the baseline is unavailable.
`map_region`'s `format` controls tile representation independently of this flag.
Missing baselines, compaction and a new conversation force full delivery.
Baselines advance only after the reply is durably recorded; retries retain their
original response. Startup details become baselines when the conversation starts.
An optional `baseQuery` reuses a delivered query for the same tool and target:
city/unit sections can differ, and construction names can differ only with the
same horizon. Apply the update to the destination query without changing its
source. Reject stale destination sequences before resolving the source; a missing
or advanced source requires a full recovery read. Reuse never registers another
query as inspected.

Overview updates retain their existing `set`, `removed` and keyed `collections`.
For changed overview objects, a delta may also carry `patch`: apply these
JSON Patch operations to the overview after the other changes. The overview
`baseStateId` still guards the entire update. JSON Patch is implemented with the
`jsonpatch` library and uses standard escaped JSON Pointer paths and ordered
operations. Full reads remain available.
The service compares complete serialized alternatives and uses a patch only when
smaller and exactly reconstructable, including JSON types. Coordination updates
retain their separate existing representation.

Notebook reads and current searches include `memoryDelivery`, with the scope,
sequence and ordered items carrying each record's ID, version and state hash.
A `full` item has a complete object in `records`; `unchanged` and `delta` refer
to the matching read baseline. `records` never contains partial record objects.
Startup notebook metadata seeds exactly the records in the startup message.
Writes and automatic `memoryUpdates` invalidate affected read baselines, while
version watermarks prevent stale receipts from restoring older records.
Historical searches return complete historical evidence and never seed current
baselines. Missing matches do not delete records. Recover with
`memory_read(ids=[...], delivery="full", includeRetired=true)`; search also accepts
`delivery="full"`.

Repeated, exact construction estimate explanations may be shared once in
`constructionExplanations` within a startup message or reply. An object's
`explanationRefs` identifies omitted fields and their local explanation IDs.
Expand these before applying detail or overview updates. The registry contains
the complete caveats and applies only to that message. Only allowlisted service
constants at construction fields are shared; authored text is preserved.
Full recovery reads include the original explanations directly.

Exact repeated native objects, arrays and strings at allowlisted presentation
fields may also use `sharedValues` and a field value of
`{"sharedValueRef":"v1"}`. Registries are local to one message and contain at most
16 values; sharing is used only when the complete serialized response is smaller.
Authored messages, notebook entries and strategic judgments stay inline. A
`stateUpdate` can contain its own registry so it remains independently consumable.
Expand references before checking hashes or applying any update.
`expand_response`, `apply_player_turn`, `apply_overview_update` and
`apply_detail_response` implement this order. Prepared startup messages include
the registry and interpretation guidance; legacy unprepared clients do not
receive this new sharing format. Full recovery returns inline values.

`decisionFeedback.sharedFacts` stores identical factual objects once. A choice's
`fieldRefs` maps omitted field names to their exact shared object. The
`expand_feedback` reference consumer restores the complete original feedback;
sharing adds no interpretation or causal attribution.

An accepted notebook edit can contain `recordUpdate` instead of `record`.
`base="submitted"` references that edit's submitted content, and `base="record"`
references the last complete record delivered with that ID in this conversation.
Apply `set` field replacements and `removed` deletions. IDs, versions, permission
errors, warnings, normalized values and service-authored metadata remain
authoritative. Multiple edits in a batch use preceding successful results;
failed edits never update the baseline. Receipts are committed in the same
transaction as the edits. Missing record baselines require
`memory_read(delivery="full")`.

The scheduled interface uses `inspect` batches. Every child retains ordinary
visibility, evidence and freshness checks, and a failure stops remaining reads.
Review reads use frozen evidence. The internal gateway's `inspect_bundle` endpoint
remains available to host code; it is absent from the model decision schema.

Managed Main can submit `wait` alone when its next choice depends on
economic work. Empty `requestIds` waits for the current pass; otherwise IDs must
be unique, known requests (at most 100). Only a receipt with state `waiting` arms
controller suspension; `attention`, `resolved`, `idle` and `not_running` return
immediately and Main must continue its decisions. Unread coordination messages
trigger immediate attention only when they are newly delivered in the bounded
working set. New inbox messages precede older messages, so archived messages
omitted from that set cannot prevent suspension indefinitely. The durable
waiting receipt is followed by controller suspension; no gateway lock remains held. Main remains unfinished
and cannot end the native turn while suspended. Dispositions, pass completion,
attention or new actionable events, negotiation, and a bounded 300-second wait
wake it. Resume retains the conversation and model settings, supplies a fresh
full overview/coordination state and accepted outcomes, and never repeats a
successful action. `WAITING_ROLE` releases compute capacity and is accounted
separately from negotiation wait. Routine requests retain their priority.

Economy completion requires an explicit choice for each remaining idle worker.
`roleCompletion.idleWorkers` lists Economy-owned workers with movement remaining
and no current job, destination or standing order. After choosing work or travel,
Economy lists any workers it deliberately leaves idle in `finish.arguments.workerSkips`,
with `targets` and a nonblank `reason` for each group. The host requires exact,
unique coverage against the current native position and retains the existing
observation-freshness checks. Workers under Main's escort control are excluded.
When none remain, an empty finish needs no skip entries.

A skip records the decision for the current pass without issuing a native order;
deliberate holds remain permitted. Missing, duplicate or inapplicable entries
return `ECONOMY_WORKER_SKIPS_REQUIRED` and the current idle-worker list without
marking the review complete. Confirmed completion receipts replay without
re-execution. Unchanged skipped workers do not repeatedly wake Economy that turn;
idle workers are reconsidered on a later turn or when relevant facts change.

Managed command schemas contain only the role's allowed variants and their
fields. Specialist `finish_role` exposes request dispositions without Main-only
fields; review `player_details` omits `turn_history`. Construction horizons
require at least one construction name in both the advertised schema and runtime
validation. These contracts retain native legality and action checks.

Startup text no longer repeats schema listings. Stable role schemas share identical
`$defs`, omit wire defaults and redundant titles, and preserve readable field names.
Unsupported schema constructs fail compilation. `command_help` and execution-tool
discovery are absent from the scheduled interface.

The private accounting ledger additionally counts full detail characters, delta
characters, unchanged-detail replies and compact memory updates. These counters
are not model context or estimates of billed tokens.

## Controller details and the model boundary

Model-facing descriptions explain gameplay choices, terms, availability and how
to use the tools. They omit scheduling details that could distort a player's
decisions, such as avoiding a beneficial trade because it pauses gameplay.

| Surface | Omitted from the model view |
| --- | --- |
| Trade and friendship tools | Gameplay suspension, background replies, polling and wait mechanics |
| Trade records and opportunities | Routing mode, native request IDs, wall-clock deadlines, counteroffer budgets and queued-response instructions |
| Overview and chat | Negotiation hold state and event wake flags |
| Capabilities | Protocol/recovery/coverage flags and the capability hash wrapper |
| Private records | Authoring job/purpose metadata (`basis`); creation/update turns remain |
| Turn and memory prompts | Artifact paths, background scheduling and concurrent-player explanations |
| Guided scenarios | Job ownership and wait mechanics; gameplay milestones remain |

Exact trade terms, game-turn durations, proposal versions, available responses and
outcomes remain visible. Players can still see that an offer is pending or has
expired. Action confirmations, observation freshness and actionable error recovery
also remain. Player-authored messages, promises and private assessments are not
rewritten to remove words that resemble implementation details.

The controller retains raw routing and recovery data in its receipts and save
records. In particular, it distinguishes built-in-AI offers from shared
negotiations before projecting the response for the model; interrupted receipt
replay follows the same path. Fresh responses, historical evidence and cached
responses use the same model projection. The structured decision journal uses memory format 14 and requires a fresh state
directory. Existing games retain their pinned runtime; do not upgrade them in place.
Historical agent-authored advice remains as written.

## Native flavor and player-authored language

The model projection removes audited stock dialogue, quotations and Civilopedia
flavor while preserving gameplay effects, costs, requirements, legal choices,
consequences and player-visible identities. For example, `availableDemandsToSend`
describes actions the reader may issue; it is not evidence of a received demand.

Player chat, negotiated promises, private plans and their ambiguity are preserved.
Unknown content is retained when its mechanical meaning lacks a structured
replacement. Capture choices retain unit recipients/conversions, city-control
effects, availability and commands, without promises of gratitude. Native AI
opinion modifiers retain their names and numerical amounts without stock speeches.
Notifications and combat prose remain when they carry event facts. Known native
notification templates with narrative wording are rendered as factual events
before translation and visibility filtering; native saves retain the original
notification text. This includes city-state rewards and requests, ancient-ruin
effects, religious-unit attrition and the Long Count free-person choice.
The host does not decide what a social promise means or impose penalties.

Native diplomacy uses the current host's structured identities, duration and
consequences. Stock dialogue is removed without reconstructing missing fields
from older host responses.

Filtering applies consistently to fresh observations, receipts, replay, history,
evidence and startup context. Original host records remain available for trusted
audit and human display. The [field audit](../archive/reports/Model-flavor-text-audit.md)
records the inspected surfaces and validation. Existing agent-authored beliefs
are not silently rewritten by a projection update.

## Memory and recovery

Agents use current observations before historical memory. The notebook separates
a provisional strategy from a next-turn handoff and retained knowledge. Handoff
edits cannot refresh strategy dates or clear strategy notices. Actions may include optional
`intent` with a reason and historical strategy references.
[Structured memory](Structured-memory.md) describes evidence and
version checks; [background reviews](Background-turn-memory.md) describes the
post-turn tools, frozen evidence and checkpoint behavior.

The host validates gameplay revisions and proposal versions against authoritative
state. Delivery caches cannot authorize commands against newer native state or
grant inspection credit for unseen results. Partial memory batches require checking each edit's `accepted`
field. See the [recovery guide](../guides/Chat-memory-and-recovery.md#interruption-and-emergency-restore).

Decision identity and all child operation IDs are durable before dispatch. Restart
replays confirmed receipts and reconciles pending native receipts before proceeding;
uncertainty cannot trigger a new action identity. A rewind fences the old epoch.
Each role/negotiation resume and final handoff review obtains a new host observation
ID. These reads cannot use model-usage counters or handoff-pass numbers: both can
remain unchanged while the position or delivery options change. The shared helper
only reads `player_turn`; it cannot assign replacement identities to actions.
Economy-wait refreshes remain bound to their distinct durable wait-operation IDs.
Military completion binds dispositions to delivered review pages; zero remaining
targets needs no invented review basis. Social promises remain exact, nonbinding
player text.

Excessive whitespace, repetitive output, or an HTTP 200 generation stream that
fails during an upstream read rejects the entire generation. The process interrupts that
turn and withholds its terminal notification until interruption is acknowledged
and completion is confirmed. Late text from the rejected turn cannot become a
decision. The controller may then reserve one recovery request per activation
and epoch, durably before generation, using the same thread, strict schema and
delivered receipts. Output stalls and interrupted streams share this one retry.
A second failure stops the activation; specialist
restarts cannot multiply the allowance. Strategic reviews share their existing
two-attempt allowance with semantic corrections. Failed advisory reports retain
their existing unavailable-report behavior. Unconfirmed interruption, transport
failures outside that confirmed generation-stream case, and uncertain action
execution do not qualify for this recovery.

The failed decision and recovery retain distinct identities linked by `retryOf`;
recovery usage is marked as repair cost. Confirmed actions are never replayed as
new operations. A successful retry does not establish complete usage for the
interrupted request: reconciliation must cover every admitted provider request.

Chat acknowledgment uses `messages[].body.message`, not an event's numeric `id`.
It is advertised only for Main gameplay with message routing enabled.
Unknown or undelivered message IDs produce durable, recoverable rejection receipts
and stop the rest of the batch. A rejected acknowledgment changes no handled-message
state. Host reads and earlier reads in the same batch cannot grant acknowledgment
credit until their results reach the model. Unexpected host/store errors still stop
execution as uncertain, with their cause distinguished from model transport failures.

`civ-agent match failures` correlates generation, validation, gateway and native receipt
failures, schema fingerprints and operation identities. Its detail view retrieves
arguments and native evidence without credentials or raw reasoning. Partial batches
are separate incidents, not duplicate failed child calls. Usage by role and model phase uses thread
total differences, includes internal continuations, and reports missing usage as
unknown. Schema/instruction/observation characters are sizes, not billed tokens;
provider metrics distinguish transport attempts from completed inference responses.
The decision journal also records gate reservations by provider request identity, including
compactions and failed attempts. Reports distinguish these counts from unknown
historical attempts and from billed tokens; request budget failures have their
own category.
Prompt-policy rejections and retry reservations retain separate provider request
IDs in the decision journal. Reports count each rejected attempt once, including
recovered attempts, and exclude the duplicate terminal wrapper. Unreported usage
for rejected requests stays explicitly unknown. Runtime configuration warnings
are categorized separately from connection failures and do not manufacture a
subsequent connection-recovery notice.
See the [structured decision report](../archive/reports/Strict-structured-decisions-2026-10-30.txt)
for enforcement evidence, the live screen's request-budget overrun and rollout limits.

## Implementation entry points

- [Decision contracts](../../src/civ_agent/decision_contracts.py), [strict schema codec](../../src/civ_agent/strict_schema.py) and [host loop](../../src/civ_agent/structured_decisions.py)
- [Startup preparation](../../src/civ_agent/startup_context.py)
- [Job workflows](../../src/civ_agent/player_prompts.py) and [discovery guidance](../../src/civ_agent/tool_discovery.py)
- [MCP result normalization](../../src/civ_agent/mcp_results.py)
- [Model projection](../../src/civ_agent/model_view.py)
- [Copyable action templates](../../src/civ_agent/action_templates.py)
