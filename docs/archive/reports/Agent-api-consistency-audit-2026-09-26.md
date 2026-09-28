# Agent API consistency audit — September 26, 2026

**Follow-up:** The findings below describe the audited baseline. Their fixes are
implemented in the working tree; see [resolution](#resolution) below.

## Result and scope

The model-facing interface contains both unnecessary duplication and actual
consistency problems. This report records findings for maintainer decisions;
application behavior has not been changed.

The audit followed the Python gateway, its startup preparation, command templates,
model projection, memory context, action updates and native data producers. The
gateway exposes **33 tools during gameplay**, **24 during communication**, and
**4 after gameplay handoff**. Its gameplay command schema contains 54 action kinds.
Operator endpoints and native tools filtered out by the gateway were excluded.

| Surface | Tools reviewed | Main findings |
| --- | --- | --- |
| Turn, capabilities and result | `player_turn`, `player_capabilities`, `player_result` | Repeated rosters; overlapping capability information |
| Older summary reads | `player_summary`, `owned_cities`, `free_great_person_choice` | Redundant alternatives to the overview; outside detail invalidation |
| Entity and map details | `unit_details`, `city_details`, `construction_details`, `map_region`, `player_details` | Construction disagreement; purchase inspection mismatch |
| Independent native reads | `research_details`, `policy_details`, `victory_progress`, `movement_preview`, `mission_preview` | Uneven freshness tracking; movement/mission previews are tracked |
| Gameplay actions | `player_command` | Command templates and some recovery instructions disagree with requirements |
| Binding diplomacy | `trade_options`, `trade_read`, `trade_offer`, `friendship_offer`, `trade_decide`, `agreement_list`, `agreement_read` | Standalone freshness gap; repeated agreement/trade summaries; intent guidance |
| Social communication | `chat_read`, `chat_history`, `chat_send`, `commitment_offer`, `commitment_decide` | Message duplication, ambiguous commitment versions, growing historical payload |
| Private records and history | `memory_read`, `memory_search`, `memory_update`, `history_read` | Incoming events repeated in automatic memory context |

All tools received source review. Runtime validation was targeted: six deterministic
audit checks, nine existing native saved-position exports, and one copied position
modified to exercise resistance and a queued Settler. This was not an exhaustive
execution of every native action or a new LLM playthrough.

## Conflicts and misleading information

### C1. Construction completion time has disagreeing producers

**Reproduced using a native saved position.** Orleans is in resistance and has a
Library at the head of its queue. The same model-facing overview contains:

```json
{
  "constructionTurns": 49,
  "constructionEstimates": {
    "Library": {"production": {"turnsIfStartedNow": null}}
  },
  "inResistance": true
}
```

Queue progress calls the native `turnsToConstruction` directly; the new estimate
withholds a time during resistance or nonpositive production. Wonder opportunities
have another direct calculation: the same resistant city advertises 123 turns for
the Great Library. Agents receive different answers without a corresponding
distinction in assumptions. This includes a mismatch introduced by the recent
construction-estimate work.

Sources: [queue progress](../../../src/main/kotlin/uncivai/player/CityDevelopment.kt),
[new estimates](../../../src/main/kotlin/uncivai/player/ConstructionEstimates.kt),
[wonder options](../../../src/main/kotlin/uncivai/player/ConstructionOpportunities.kt),
[overview projection](../../../src/civ_agent/observations.py).

### C2. The invalidation contract covers only some reads

**Reproduced through the real gateway with a fake native host.** A named
`research_details` read reports `researchQueue: ["Pottery"]`. After setting the
queue to Mining, the command's state update correctly reports `["Mining"]`, but
`invalidatedDetails` is empty. The named read is never registered for invalidation.
Its native revision is stripped, leaving no turn or public state ID on that read.

This is not a failure to update the overview. It is a gap in the promise that
agents can retain inspections until listed as invalidated. It matters especially
for old availability fields and copyable research commands that embed a queue.

The same tracking omission affects `policy_details`, `victory_progress`, the older
summary reads, and standalone trade/agreement reads. Local detail projections and
movement/mission previews do participate in invalidation. Documentation explains
that victory comparisons require an independent reread, but the general tool
guidance does not clearly distinguish these two freshness contracts.

Sources: [gateway read branches and descriptions](../../../src/civ_agent/proposal_gateway.py),
[invalidation](../../../src/civ_agent/state_updates.py),
[native research/policy metadata](../../../src/main/kotlin/uncivai/player/DecisionDetails.kt).

### C3. Commitment summaries discard the version needed to interpret their status

**Reproduced with a controlled social ledger.** Native communication retains all
versions of an offer. The overview includes all those versions but drops their
`version` field. A previously accepted version and an unaccepted revision become:

```json
[
  {"proposal":"promise1","participants":["Rome","Greece"],"kind":"peace","effective":true},
  {"proposal":"promise1","participants":["Rome","Greece"],"kind":"peace","effective":false}
]
```

The underlying ledger can legitimately contain both. The summary makes them look
like contradictory statuses for one promise. `chat_read` retains the versions and
terms; the problem is the compact projection, not social-rule enforcement.

Sources: [communication view](../../../src/main/kotlin/uncivai/Communication.kt),
[overview commitments](../../../src/civ_agent/observations.py).

### C4. Some offered commands do not disclose the additional inspection they require

**Purchase case reproduced through the gateway.** Reading
`city_details(target, sections=["purchases"])` supplies an available purchase,
its cost, confirmation and `BUY_CONSTRUCTION` command. Copying it, adding intent
and accepting the confirmation still returns `DETAIL_REQUIRED`: the gateway
requires a named `construction_details` read, a full city read, or both production
and purchases sections. The offered option has no inspection hint for this.

The same pattern exists in the source for `tacticalReview.cityBombardments`:
the overview supplies `CITY_ATTACK` commands, but using one requires a separate
city combat inspection that is not attached to the offered command.

These checks do not invent native legality, but the presentation invites an extra
failed call before revealing the interface prerequisite.

Sources: [templates](../../../src/civ_agent/action_templates.py),
[inspection requirements](../../../src/civ_agent/observations.py),
[bombardment summary](../../../src/civ_agent/tactical_review.py).

### C5. A tactical label reports queued future work as current production

**Reproduced using a native saved position.** Paris has queue
`["Library", "Settler"]`; the city roster correctly shows Library first, while
`tacticalReview.citiesProducingSettlers` includes Paris. That field tests membership
anywhere in the queue. It can therefore overstate current expansion activity.

A separate, lower-priority ambiguity: `injuredMilitaryNearHostiles[].militaryOnSameTile`
includes the injured unit itself. For those military rows it does not establish
the presence of another protective unit. The field is literally true but easy to
misread as an escort cue.

Source: [tactical review](../../../src/civ_agent/tactical_review.py).

### C6. Two instructions contradict the accepted arguments

- `trade_decide` says **“Supply only its ID and decision.”** Its schema and gateway
  also require `intent`. Following that sentence literally produces
  `INTENT_REQUIRED`.
- The native `TURN_REVIEW_REQUIRED` error says to supply `reviewId`. The gateway
  removes that field from responses, rejects model-supplied bookkeeping, and
  normally injects it itself. If this native error reaches an agent, its recovery
  instruction is impossible to follow literally. This second case was identified
  by tracing the error path, not induced in a live game.

Sources: [gateway descriptions and validation](../../../src/civ_agent/proposal_gateway.py),
[trade schema](../../../src/civ_agent/tool_schemas.py),
[native error message](../../../src/main/kotlin/uncivai/player/PlayerSessionException.kt).

## Duplication and avoidable context growth

### D1. Incoming messages are delivered twice at startup

**Reproduced through `prepare_startup`.** The same incoming event appears in both
`reads.chat_read.messages` and `memoryContext.newEvents`. A unique message string
appears twice in the assembled user prompt. These are exact event copies, not a
message followed by an agent-authored interpretation.

Gameplay startup also includes overlapping turn, scenario, rules, treasury,
income, happiness and known diplomacy in `player_turn` and `chat_read.context`.
The latter is useful for communication-only activations; the duplication arises
when both are prepared together. Trade summaries occur in both reads too.

Sources: [startup preparation](../../../src/civ_agent/startup_context.py),
[chat read](../../../src/civ_agent/proposal_gateway.py),
[memory context](../../../src/civ_agent/civilization_state.py),
[communication context](../../../src/main/kotlin/uncivai/player/CommunicationContext.kt).

### D2. The overview carries overlapping unit rosters and economy totals

Across all nine saved positions, every `turnReview.units` row was an exact subset
of an existing `units` row: 38 duplicated rows in total. That repeated section
alone occupies 226–693 compact JSON bytes per fixture, before field names or
other repetition. These byte counts are not token estimates.

`tacticalReview.military` repeats handles, names, positions, health, orders and
attack counts again, while adding useful strength and role information. Tactical
treasury, income and happiness repeat the economy section. The tactical and turn
review sections are replaced wholesale when changed, so this also increases
action-update payloads when a single unit changes.

Sources: [overview](../../../src/civ_agent/observations.py),
[tactical review](../../../src/civ_agent/tactical_review.py),
[delta collection keys](../../../src/civ_agent/state_updates.py).

### D3. Construction data has several overlapping representations

The current construction's cost, stored production, production rate and time can
appear in both queue `progress` and the new estimate's `production`. City production
details, named construction details and wonder opportunities expose additional
overlapping views. Some repeat current values within a single response.

This overlap already caused C1. It also needs care: a later queued copy of a unit
can legitimately have different stored production from the first copy. Likewise,
the completion benefit and the change while building answer different questions;
those should not be treated as identical values merely because both mention yields.

Sources: [detail projections](../../../src/civ_agent/observations.py),
[estimate projections](../../../src/civ_agent/construction_estimates.py),
[city sections](../../../src/civ_agent/entity_sections.py).

### D4. Older summary APIs remain discoverable beside the canonical overview

`player_summary`, `owned_cities` and `free_great_person_choice` overlap facts
already available in `player_turn` and focused detail reads. They add alternative
names and response formats without sharing the local-detail refresh contract.

Capabilities also expose `legacyMutationsSupported`, although the gateway does
not expose the separate legacy mutation tools at all. Ordinary rename and Great
Person actions use `player_command`. The capability field describes the native
transport more broadly than the agent's actual interface.

Sources: [gateway catalogue](../../../src/civ_agent/proposal_gateway.py),
[native capabilities](../../../src/main/kotlin/uncivai/player/TurnCommands.kt),
[older tools](../../../src/main/kotlin/uncivai/mcp/PlayerTools.kt).

### D5. The default chat read also delivers a growing historical ledger

`chat_read` returns every permitted social offer version, consent and outcome,
all latest trade proposal summaries regardless of terminal status, and all
agreement summaries including ended agreements. It is prepared at every gameplay
activation. `agreement_list` repeats the agreement summaries, while separate
history/detail tools also provide historical access.

These are useful records, but current negotiation context and the entire retained
ledger are bundled together. The output has no bound tied to recent turns or
active items. The audit confirmed the source behavior; it did not measure its
late-game token cost.

Sources: [communication view](../../../src/main/kotlin/uncivai/Communication.kt),
[trade summaries](../../../src/main/kotlin/uncivai/EngineTrades.kt),
[agreement summaries](../../../src/main/kotlin/uncivai/AgreementLifecycle.kt).

### D6. General instructions repeat across several startup components

Discovery guidance, gameplay workflow and core tool descriptions repeat advice
about intent, copyable commands, prepared reads, memory being advisory and state
refresh. Most of this repetition is consistent, unlike C6. It is a lower-priority
prompt cleanup opportunity; stable prefix caching does not remove it from the
model's context.

Sources: [workflow](../../../src/civ_agent/player_prompts.py),
[discovery](../../../src/civ_agent/tool_discovery.py),
[core definitions](../../../src/civ_agent/proposal_gateway.py).

## Overlap that is intentional or already handled

- Duplicate MCP text equal to `structuredContent` is already removed at the model
  boundary. This is not the old double-JSON transport problem returning.
- Compact overview versus focused detail is useful when the latter supplies
  additional legal choices, requirements or consequences.
- Live rankings, recorded chart history and demographics describe different
  samples; their metric definitions and sampling scopes are explicit.
- Native treaties and social commitments are deliberately separate. Social
  statements remain nonbinding and can be ambiguous.
- Detailed action history, private assessments and current observations have
  different purposes. A historical fact differing from today's state is not by
  itself a contradiction.
- Construction estimates distinguish ongoing completion benefits, while-building
  effects, and rounded food accumulation. Those distinctions are documented.

## Validation artifacts

Scratch artifacts are in `/tmp/unciv-api-audit-20260926/`, including:

- `test_audit.py`: six passing checks that reproduce the catalogue, message,
  freshness, purchase, commitment-version and saved-position findings.
- `research-refresh.json`, `purchase-template.json`, `startup-duplicate.json`,
  `commitment-versions.json`: gateway/projection evidence.
- `native-overview.json` and `native/audit-resistance/initial-turn.json`: the copied
  saved-position example; the checked-in source save was not changed.
- `roster-duplication.json`: measurements across nine existing exports.

The original audit checks assert the observed problems; passing them does not
mean those problems are fixed. At the initial audit stage, only this report and
its archive index were added. The implementation and its validation follow below.

## Resolution

The subsequent implementation addresses the findings as follows:

| Findings | Change |
| --- | --- |
| C1, D3 | Queue progress, construction estimates and wonder options share one native time calculation. Resistance has no current ETA. The overview no longer has a second construction-time field. Detailed views share production accounting while retaining distinct stored work for later queued unit copies. |
| C2 | Independent research, policy, victory, result, chat and trade/agreement reads register exact queries for invalidation. Changes invalidate them conservatively without eagerly requerying them. Full reads and communication mutation responses also deliver notices. Negotiation and recovery retain notices across discarded overview snapshots. Research and policy retain the native observed turn. |
| C3 | Chat is the authoritative model-facing home for versioned promises; the ambiguous versionless overview list is removed. Effective older terms and a pending revision remain distinct and retain their wording. |
| C4 | Inspecting city purchases satisfies a purchase's inspection requirement. Named construction inspection remains another valid route. Bombardment templates include their combat inspection. |
| C5 | The Settler-production summary checks the queue head. Friendly presence excludes the subject unit and is named `otherMilitaryOnSameTile`. |
| C6 | Trade decision instructions include intent. Turn-review errors direct agents to a valid overview read; the service supplies review IDs. |
| D1 | Messages are delivered once in startup chat. Private memory context no longer repeats events. Gameplay omits the optional chat context; communication requests it explicitly. |
| D2 | Military strength and role move into the main unit roster. Tactical review retains risks and opportunities; duplicate military and turn-review rosters and tactical economy totals are removed. |
| D4 | The three older summary tools and native legacy-mutation capability flag are removed from the agent boundary. Gameplay now exposes 30 tools. |
| D5 | Default chat includes current promises, pending trades and active agreements. Historical outcomes, expired/withdrawn/superseded offers and ended agreements remain accessible through explicit history/detail reads; `includeHistory=true` requests the retained ledger. |
| D6 | General workflow and discovery text defer to tool-specific inspection, update and memory rules rather than repeating them. |

No native game rule or social obligation was added. Legacy native endpoints used
by other clients remain outside the agent catalogue. Full native audit records and
historical receipts remain available. The scripted integration-test agent now
requests communication context explicitly; this preserves the concurrent trade
and peace tests under the new API contract.

Regression coverage is in `tests/test_api_consistency.py`, the existing gateway,
startup, observation, memory, scheduler and trade suites, and
`BuildingEstimatesProbe`. Validation uses source checkouts and fake model
processes, with no paid model calls or package distribution build.

### Implementation validation

- Full Python suite: 749 tests and 91 subtests passed; two opt-in native model
  checks skipped.
- Native host checks: 15 tests passed across
  `ObservationOpportunitiesIntegrationTest`, `PlayerSummaryJsonTest` and
  `S06McpIntegrationTest`.
- Ruff lint and formatting checks passed; Pyrefly reported zero errors.
- Projecting the same nine saved-position observations reduced their combined
  compact overview size from 141,820 to 136,171 bytes (3.98%). This excludes
  savings from startup message deduplication, default chat history filtering and
  prompt changes. It measures payload size, not model tokens or turn latency.
  Scratch measurements are in `after-implementation.json` beside the original
  audit artifacts.
