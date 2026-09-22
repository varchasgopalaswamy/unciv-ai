# Workers and city development

This slice adds ordinary worker jobs, citizen assignment, construction queue editing,
and the city/research information needed to make those decisions. It uses the selected
engine's bundled rulesets and shared engine operations used by the desktop callers.
See [the protocol](Player-api-protocol.md) for the exact command fields.

## Try it

Launch the host and a player with the [local scripts](../guides/Local-launchers.md), or use the
[automatic scheduler](../guides/Automatic-turns.md). A useful player request is:

> Use the civ MCP tools to develop my civilization. Inspect the city yields, growth,
> construction progress, research and worker options in player_turn. Build a worker,
> improve useful tiles, and manage production and citizens where appropriate. Use
> only offered actions, re-observe after each command, and preserve exact request IDs
> and payloads for retries. End this turn once. Report unsupported decisions.

An interactive session plays one turn per request; the scheduler supplies later turns.
Use `uv run civ-agent host inspect Rome` to examine the committed position in the desktop UI.

## Semantics and evidence

| Surface | Behavior and source | Validation |
| --- | --- | --- |
| Worker picker | `WorkerImprovementPicker` shares the desktop improvement picker reports, required technology/feature removal, displayed yield changes and time estimates. `PlayerWorkerOperations` validates the owned active worker and current tile. | Engine `PlayerWorkerOperationsTest`; host `WorkerDevelopmentProbe`: ownership, turn, hidden resources, query immutability, preview precision, stale commands and recovery. |
| Worker orders | `START_WORKER_JOB` starts an offered ordinary improvement, feature removal or road, including picker prerequisite queues and ordinary repair. Selecting the current job preserves progress. `STOP_WORKER_JOB` cancels work when offered. | Engine worker tests and existing `WorkerAutomationTest`; host checkpoint/receipt tests. |
| City yields and growth | `development` contains rounded top-level yields, UI-precision breakdowns, food storage, growth/starvation estimates, population and unassigned citizens. Breakdown trees are copied before display-only merging. | `CityDevelopmentProbe` compares UI calculations, varies hidden resources, and checks repeated queries leave engine state unchanged. |
| Tile yields | Visible map tiles use the world tile display's integer precision. Owned-city workable tiles use the city display's rounding. Live map yields are omitted in fog because the general engine calculation uses current improvements rather than remembered ones. | Host paired-world and precision tests. This is not a complete tile tooltip implementation. |
| Citizens | `SET_WORKED_TILE` matches city tile-icon assignment, requiring free population when adding work. Removing work also unlocks the tile. `SET_TILE_LOCKED` matches the separate lock control. Each tile advertises available changes. | Shared `PlayerCityOperations` and `CityView`; engine `PlayerCityOperationsTest`, host `CityCommandProbe`: own/foreign tiles, center tiles, population, puppets/resistance, stale revisions and retries. |
| Production queue | Append, remove and move use shared operations. Entries have indices so duplicates remain distinguishable. Removal preserves accumulated production and follows the UI's empty-queue behavior. Moves use the offered destination indices, including perpetual-construction restrictions. | Engine city tests and host command/restart scenarios. |
| Research | Current technology, rounded science, overflow, stored science, technology costs and displayed turn estimates follow the research UI calculations. | Host city-development scenarios; existing queue and free-technology validation. |
| Repeated development | Scripted HTTPS players play twenty rounds with a process restart after ten, build workers, complete improvements, grow cities and continue production/research. Exact end-turn receipt replay does not advance the game twice. | `DevelopmentRunIntegrationTest`; this is scripted acceptance, not a live-model completed match. |

Commands require the current player's opaque handles and revision. Action flags are
disabled while paused or waiting. Invalid or unsupported orders do not choose an
alternative action. Accepted worker orders can take several game turns to finish;
inspect the following observations and checkpoint rather than interpreting acceptance
as immediate completion.

The [city management slice](City-management.md) adds specialists, great-person
progress, focus/growth controls, building sales and shared-tile reassignment.
[Economy](Economy-and-unit-actions.md), [combat](Combat.md) and
[war/peace](War-and-peace.md) have their own bounded implementations. Worker
automation, route-to-destination, instant improvements, construction placement and
full Civilopedia descriptions remain outstanding. The coverage ledger tracks the
broader UI inventory and privacy review.
