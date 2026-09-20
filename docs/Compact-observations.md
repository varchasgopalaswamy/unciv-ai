# Compact observations and victory progress

The civilization service returns a compact `player_turn` overview. The Kotlin host
still creates the complete permission-filtered snapshot; the service caches it for
the current job. This reduces model input, not the engine work needed to produce a
snapshot. Projected entity details reuse the cache without another host request.
Native pathfinding, special-mission previews and tree/victory queries run on demand in the host.

The overview includes tactical and strategy reviews, city and unit rosters,
production queues, growth, empire totals, research/policy choices, known diplomatic
status, and turn-review unit activity. Alerts, decisions, ordinary notifications
and combat reports retain their full content. Agents must read popup content
before acknowledging it. [Ordinary notifications](Ordinary-notifications.md) can
be dismissed in batches after reading; agents choose which assessments to update.
Confirmed-action bodies and detailed economic/map data move to explicit reads.

| Tool | What it returns |
| --- | --- |
| `unit_details({target,sections})` | Selected unit sections listed below; omit `sections` or use `["all"]` for full details |
| `city_details({target,sections})` | Selected city sections listed below; omit `sections` or use `["all"]` for full details |
| `construction_details({target})` | Compact construction catalogue, including entries unavailable for purchase |
| `construction_details({target,names})` | Up to 16 named entries with full descriptions, costs, requirements, confirmations, queue progress and copyable commands |
| `map_region({center,radius})` | Explored tiles and frontier within radius 0–4 of an observed coordinate; respects world wrap |
| `player_details({section})` | `economy`, `diplomacy`, `research`, `policies`, `turn_history`, or `map_index` |
| `research_details({names})`, `policy_details({names})` | Existing on-demand tree catalogues and selected UI descriptions |
| `movement_preview({target,destination})` | Native adjacent route paths, estimated turn stops and costs; required before a matching `SET_UNIT_DESTINATION` |
| `mission_preview({target,mission,destination})` | Native rebase, paradrop, air sweep or nuclear preview; required before a matching special-mission command |
| `victory_progress({names})` | Victory-screen milestones, own score and turn limit, and permitted rival progress |

For example, read `player_turn`, copy a unit handle from `units`, read
`unit_details({target:"unit2",sections:["movement"]})`, and choose one of its
offered destination commands. For a distant destination use `movement_preview`
and its `SET_UNIT_DESTINATION` command. After a successful command, apply its
`stateUpdate` to the last overview and reread relevant `invalidatedDetails` before
the next action. Unchanged prior inspections remain valid. Queries for
missing entities return `available:false` without inventing their current state.

## Focused sections and commands

Every section includes the entity's identity and basic status. Multiple sections
can be requested together; order and duplicate selections are canonicalized.
Full reads retain all native fields, including future fields not yet assigned to
a section. These projections use only the permission-filtered cached snapshot.

| Unit section | Details and applicable actions |
| --- | --- |
| `movement` | Native offered paths/costs, active destination/cancellation and nearby hostiles |
| `combat` | Strength, remaining attacks and full combat previews |
| `orders` | Current order and available orders |
| `promotions` | Experience, prerequisites and promotion choices |
| `work` | Worker jobs, prerequisites, yield changes and Work Boat improvements |
| `settlement` | Founding availability, nearby hostiles and native settlement promises |
| `economy` | Upgrades, pillage, disband, costs and confirmations |
| `abilities` | Great Person abilities, spaceship assembly and city-state gifts |
| `air` | Missions, ranges, bases/capacity, transport and interception readiness |

| City section | Details and applicable actions |
| --- | --- |
| `status` | Capture choices; also satisfies the rename inspection |
| `production` | Queue, available names, progress and queue management |
| `citizens` | Growth, food, focus, worked/locked tiles, specialists and shared tiles |
| `economy` | Yields/breakdowns, buildings and sales with confirmation text |
| `purchases` | Construction/tile costs, eligibility and confirmations |
| `combat` | Full bombardment previews |

Prefer named research, policy and construction reads after selecting candidates
from the overview or a catalogue. Named research includes copyable native
prerequisite queues and free-technology choices; named construction includes
queue/purchase commands. Available entity options and native movement/mission
previews also include commands. Copy only command arguments, add your own
structured `intent`, and read any `confirmationField` and consequences before
explicitly confirming. Templates never prefill consent or intent, and native
legality is checked again during execution. Air-base options provide the exact
preview request; reading an air section alone does not satisfy a mission preview.

`DETAIL_REQUIRED` applies nothing and names the detail query needed before an
action. A construction catalogue does not establish that purchase confirmations
were read; inspect the named entry, full city, or both `production` and `purchases`.
A movement read cannot satisfy a disband, gift, promotion or settlement inspection.
Declaration warnings require
`player_details({section:"diplomacy"})`. Popup responses use the complete content
already present in the overview, as does ordinary notification dismissal. Social
commitment decisions require `chat_read`; formal trades continue to require
`trade_read`.

Argument errors return the selected command's expected shape and safe field error
codes, without echoing invalid values. `OBSERVATION_REQUIRED` supplies a concrete
refresh query. Successful gameplay actions, including alert acknowledgements,
return [state updates](Command-state-updates.md) with deterministic section/row
changes. Only changed inspections expire; native movement/mission previews still
expire on a changed revision. An argument error with `applied:false` does not
itself require another overview. Failed refreshes preserve the applied receipt
and require a full `player_turn`; they never repeat the action.

Pydantic defines the service's overview, detail input and detail result schemas.
`observation:overview` cites the game overview; each detail response has an
`evidence` ID for precisely that query and data. Cached raw data is not marked as
delivered. Previously requested detail projections are recomputed when a new
snapshot arrives so changed assumptions are flagged, without claiming the model
read those changes. Movement and mission previews are retained for an unchanged revision and
invalidated when the revision changes; they require a fresh native query rather
than reconstruction from cached map data. The compact unit roster retains the
active destination, Work Boat action availability, available air missions, transport state and automatic interception readiness. Full `unit_details.air` adds bases, remaining slots and ranges. A greeting or a change to
another entity does not invalidate
a plan depending only on the inspected section. Changes to another section of
that same unit also leave its focused evidence unchanged. Historical raw snapshots remain
available through explicit memory reads. Restart and whole-match rollback use the
same versioned SQLite evidence store.

Victory progress uses Unciv's pure milestone completion checks and victory-screen
header text, including cultural counts and spaceship-part progress. Rival next
milestones follow `VictoryScreenCivGroup`'s visibility rule; unmet living rivals
are omitted before ordering. `names` selects the displayed victory types returned
by this query. Time victory is represented by the score and turn limit, following
the UI's omission of its milestone column. The query does not acknowledge popups.

This is partial victory-screen coverage: detailed per-nation/per-policy step
buttons, demographics, global rankings and postgame exploration remain outside
this slice. It does not establish full UI or privacy parity for existing surfaces.

Regression coverage includes cached detail reads, required confirmations, unread
evidence rejection, selective assumption invalidation, restart, map-wrap distance
against the engine, full alerts, response-size reduction, native victory progress,
HTTPS delivery and unmet-rival changes.

The September 20 [efficiency evaluation](Agent-efficiency.md#tasks-2-and-5-validation-and-live-results)
records focused unit/city reads, named choices, matching commands, payload sizes,
and remaining refresh retries in a real Luna/medium opening turn.

## Live verification, September 14, 2026

A fresh seed-412 Vanilla game completed one round through the three HTTPS services
with `gpt-5.6-luna`, medium reasoning. Evidence is retained locally in
`/tmp/civ-overview-check-20260914-130526`, including `verification.json`, the native
checkpoint, per-player transcripts, receipts, private memory and diagnostics. All
verification processes were stopped afterward.

The native save reached turn 1 with Rome active. Rome and Athens each had a
Monument queued with 4 production stored; Paris had a Scout queued with 4 production.
All three researched Pottery and retained their Warriors; final movement receipts
matched native unit positions. Each player read victory progress and used the
detail queries, then committed exactly one handoff. There were no unresolved
dispatched requests. Rejected argument shapes, stale assumptions and missing reads
applied nothing; the agents recovered and completed their turns.

| Civilization | Median full snapshot | Median overview reply | Reduction | First-turn duration |
| --- | ---: | ---: | ---: | ---: |
| Rome | 30,782 bytes | 3,355 bytes | 89.1% | 206.1 s |
| Greece | 33,411 bytes | 2,795 bytes | 91.6% | 158.1 s |
| France | 39,826 bytes | 2,752 bytes | 93.1% | 152.1 s |

These compare serialized permitted snapshots with delivered overview replies from
the same run, not tokens or a controlled before/after timing experiment. Median
host turn-query time was 10–18 ms. Model calls and repeated inspection still cost
substantial time; smaller replies alone do not establish a turn-time speedup.
Greece recovered from invalid memory priorities by creating plans without topic
subjects, which limits automatic topic-specific review. A single opening round
does not establish later expansion, barbarian defense or strategic reliability.
