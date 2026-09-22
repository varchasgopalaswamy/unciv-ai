# Agent context and tools

This describes the current model-facing interface. Earlier numbered efficiency
plans and their measurements are in the [archive](../archive/README.md).

## Startup and discovery

Each scheduled turn starts a fresh conversation. The service prepares permitted
observations through the same gateway used for explicit reads and then refreshes
the selected private memory records. A cached record is not treated as delivered
merely because it is in the service database.

Gameplay prepares `player_turn`, `player_capabilities`, `victory_progress` and
`chat_read`. Communication prepares its chat context. Core tool definitions come
from the current permitted catalogue:

| Job | Prepared definitions |
| --- | --- |
| Gameplay | `player_turn`, `player_command`, `memory_update`, `memory_read`, `unit_details`, `map_region` |
| Communication | `chat_read`, `chat_send`, `memory_update` |

The model can call prepared tools directly. For additional tools, the shared
discovery guidance uses exact-name lookup, at most four new definitions per
lookup, and names-only catalogue exploration. Previously read definitions are
reused. All tools permitted for that job remain discoverable.

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
required inspection. Discovery, action templates and prompts are guidance; native
legality, authority and observation checks still apply.

Successful commands return receipts and [state updates](Command-state-updates.md).
Deterministic changes preserve unchanged sections and identify added/removed
entities and stale inspections. The service maintains its snapshot internally;
the agent does not need a new overview after every successful command. A movement
preview expires with native revision changes, so unrelated commands should finish
before fetching the preview for the next movement action.

## Native flavor and player-authored language

The model projection removes audited stock dialogue, quotations and Civilopedia
flavor while preserving gameplay effects, costs, requirements, legal choices,
consequences and player-visible identities. For example, `availableDemandsToSend`
describes actions the reader may issue; it is not evidence of a received demand.

Player chat, negotiated promises, private plans and their ambiguity are preserved.
Unknown content is retained when its mechanical meaning lacks a structured
replacement. Notifications and combat prose remain when they carry event facts.
The host does not decide what a social promise means or impose penalties.

Filtering applies consistently to fresh observations, receipts, replay, history,
evidence and startup context. Original host records remain available for trusted
audit and human display. The [field audit](../archive/reports/Model-flavor-text-audit.md)
records the inspected surfaces and validation. Existing agent-authored beliefs
are not silently rewritten by a projection update.

## Memory and recovery

Agents reconsider plans while playing and batch useful durable changes after
handoff. A fresh `intent.reason` can explain an action without first revising a
stored plan. [Structured memory](Structured-memory.md) describes evidence and
version checks; [background memory](Background-turn-memory.md) describes the
post-turn tools, frozen evidence and checkpoint behavior.

The host tracks stale retries per operation. Agents follow the supplied recovery
reads; only a terminal `REPEATED_STALE_ASSUMPTIONS` response declares recovery
exhausted. A successful operation resets its counter. Partial memory batches
require checking each edit's `accepted` field. See the
[recovery guide](../guides/Chat-memory-and-recovery.md#interruption-and-emergency-restore).

## Implementation entry points

- [Startup preparation](../../src/civ_agent/startup_context.py)
- [Job workflows](../../src/civ_agent/player_prompts.py) and [discovery guidance](../../src/civ_agent/tool_discovery.py)
- [MCP result normalization](../../src/civ_agent/mcp_results.py)
- [Model projection](../../src/civ_agent/model_view.py)
- [Copyable action templates](../../src/civ_agent/action_templates.py)
