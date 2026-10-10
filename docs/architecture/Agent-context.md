# Agent context and tools

This describes the current model-facing interface. Earlier numbered efficiency
plans and their measurements are in the [archive](../archive/README.md).

## Startup and discovery

Each scheduled turn starts a fresh conversation. The service prepares permitted
observations through the same gateway used for explicit reads, selects private
memory using their current subjects and players named in proposed diplomatic
initiatives, and then reads the chosen records. Received message participants
also select relevant notes; prose is never mined for implied facts. Selection
stays within the memory budget and the dispatch snapshot's contribution boundary.
Selection freezes the current versions of eligible dispatched records, including
review notices; startup memory reads deliver those exact versions. Concurrent
notebook edits cannot enlarge the prepared context. Later explicit reads obtain
the latest versions.
A cached record is not treated as delivered
merely because it is in the service database.

Gameplay prepares `player_turn`, `player_capabilities`, `victory_progress` and
`chat_read`. Incoming events appear only in the chat read, not again in private
memory context. Gameplay chat omits economic/scenario context already covered by
the overview; communication prepares `chat_read(includeContext=true)`. Core tool definitions come
from the current permitted catalogue:

| Job | Prepared definitions |
| --- | --- |
| Gameplay | `player_turn`, `player_command`, `unit_details`, `movement_preview`, `construction_details`, `map_region`, `chat_send` |
| Communication | `chat_read`, `chat_send`, `memory_update` |
| Review | All permitted read-only tools: `player_turn`, `victory_progress`, `research_details`, `policy_details`, `city_details`, `unit_details`, `construction_details`, `player_details`, `map_region`, `chat_read` |

The model can call prepared tools directly. For additional tools, the shared
discovery guidance uses exact-name lookup, small relevant definition lookups,
and names-only catalogue exploration. Previously read definitions are
reused. All tools permitted for that job remain discoverable.
Reviewers receive their complete catalogue and can call those tools without discovery.
Gameplay memory reads and writes remain available on demand; selected startup
records are still delivered automatically.

Gameplay batches already-chosen independent actions in one script, awaiting each
command sequentially and retaining every receipt. Errors or unavailable state
updates stop the batch; successful earlier commands remain applied. Decisions
that depend on combat, scouting or fresh previews use another model pass.
The player reviews the results before ending the turn.

`END_TURN` ends all gameplay and memory-tool access. Optional `memory_update`
remains discoverable during play. Scheduled reviews run concurrently at a shared
round boundary after all players finish; gameplay and communication wait for the
reports. The latest completed report is supplied once as dated advice before
historical memory and need not be rewritten as a memory entry. It compares concrete
plans, an optional preference and reconsideration conditions, alongside next steps,
diplomatic initiatives and brief outlooks for each enabled victory.

Gameplay can initiate useful diplomatic contact without an incoming message.
Communication jobs pursue the civilization's objectives through questions,
counteroffers and further contacts when worthwhile. Both receive dated advice
and relevant private notes. Negotiation guidance covers cooperation, gathering
information, threats, concealment, bluffs and breaking nonbinding promises,
weighing incentives, relative gains, credibility and retaliation. Silence is
valid; jobs finish when awaiting a reply or further communication has little value.
Existing message events wake subsequent exchanges; no periodic diplomatic job
or mandatory message count is added. Private plans enter the player's context,
not outgoing messages. Useful unresolved questions and intentions use the
existing notebook; routine receipts and acknowledgements need no duplicate notes.

The command schema is a compact envelope of possible
fields; inspected choices supply copyable arguments. The server still validates
each action's exact required/permitted fields and confirmations before dispatch.

The gameplay catalogue has 30 tools; communication has 24. The older `player_summary`, `owned_cities` and `free_great_person_choice`
reads are not exposed. Capabilities list available queries and commands directly,
without a hash wrapper, protocol version or recovery/coverage flags.
`player_turn.units` is the single overview unit roster; its military entries
include strength and role. Tactical review adds risks and opportunities, while
turn review retains only its confirmed-action count. Chat owns versioned social
promises and trade summaries; the overview does not repeat them.

Inactive unit capabilities and empty optional lists are omitted; every owned
unit remains in the roster. City health/defense and worker job time are included.
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
Wonder estimates appear only under `wonderOpportunities` in the overview.
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
until all reports are ready. Opening views include starting locations and abilities.
It uses the player's model and effort, a captured permitted position and planning
catalogues, and read-only frozen detail tools. Known foreign cities are summarized
with observed capital status and visibility; current defenses require visibility.
Private notes, old strategy, action rationales and model deliberation are excluded.
The reviewer cannot act, communicate or write
memory, and cannot query the advancing game.

At dispatch, gameplay receives the latest completed report once, dated with its
source turn, before historical memory. It checks the advice against fresh facts,
chooses its own actions and can reject the recommendation. Reports arrive before
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

Cached detail projections refresh only while a running reader or an active
strategic assumption needs them. Historical queries retain their last evidence
until explicitly read again. Region and section dependencies exclude unrelated
tiles and entity sections.

Native research/policy/victory/result and trade/agreement/chat reads also register their
queries for invalidation. They expire conservatively after an observed native or
social change; the service does not fetch them all again. Relevant expired reads
appear in `invalidatedDetails`, including on full reads and communication mutation
responses. Native research and policy responses retain their observed turn number.

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
responses use the same model projection. This is a presentation change, with no
checkpoint or database format change. Already-running services pick up the code
on restart; historical agent-authored advice remains as written.

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
state. Python cache updates never gate commands or count toward a terminal
stale-retry limit. Partial memory batches require checking each edit's `accepted`
field. See the [recovery guide](../guides/Chat-memory-and-recovery.md#interruption-and-emergency-restore).

## Implementation entry points

- [Startup preparation](../../src/civ_agent/startup_context.py)
- [Job workflows](../../src/civ_agent/player_prompts.py) and [discovery guidance](../../src/civ_agent/tool_discovery.py)
- [MCP result normalization](../../src/civ_agent/mcp_results.py)
- [Model projection](../../src/civ_agent/model_view.py)
- [Copyable action templates](../../src/civ_agent/action_templates.py)
