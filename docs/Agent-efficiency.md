# Agent efficiency follow-ups

Status: tasks 1, 2, 4 and 5 are implemented and live evaluated on September 20, 2026.
The original task 3,
[selective tool discovery](Selective-tool-discovery.md), is also implemented and
live evaluated. This document retains the original numbering.

The [input-token diagnosis](Vanilla-readiness.md#first-live-batch-input-token-diagnosis)
identifies duplicate result representations, large observations, repeated refreshes,
and command retries. Preserve native Unciv rules, player visibility, consequential
action inspections, and recovery while reducing those costs.

## Task checklist and order

- [x] **1. Deliver each response once.** Present one payload to the model.
- [x] **2. Use selective reads.** Improve use of existing filters and add focused
  unit/city sections.
- [x] **4. Reduce command–refresh cycles.** Return fresh player-visible state
  updates with successful command receipts.
- [x] **5. Make commands easier to issue correctly.** Extend copyable command
  arguments beyond combat.

These four follow-ups are complete, including deterministic changes in task 4.
The [three-round opening comparison](Opening-efficiency-results.md) now measures
all efficiency changes together: nine turns finished in 12m 40s versus 16m 12s,
with 55.5% fewer input tokens and 30.8% fewer player-tool calls. All accounting and
25 state-update reconstructions passed; the linked report includes per-turn
variation and remaining memory-review retries.
Full-game acceptance and measurement over longer matches remain outstanding.

The subsequent [prompt streamlining pass](Prompt-streamlining.md) consolidates
the startup workflow and moves detailed guidance into the relevant tool definitions.
Its corrected three-round replay completed all nine turns with 7.3% fewer input
tokens and 9.0% fewer player-tool calls, but 2.3% longer elapsed time. The report
includes the initial recovery-prompt failure, correction and remaining retries.

## 1. Deliver each response once

Previously, the gateway included the same JSON in MCP `content[].text` and
`structuredContent` for every client. Printing the full result exposed both copies
to the model. Audit-history normalization removed this duplication from its
display but did not change the live model input.

### Scope

- Make the model-facing adapter present `structuredContent` once for ordinary
  game-tool responses, without also printing its duplicate text representation.
- Fall back to text/content when structured content is absent. Preserve error
  status, distinct warnings, and any nonduplicate text or other content.
- Preserve MCP compatibility and the original audit records. Confirm the actual
  runtime integration point before changing transport fields; changing only the
  audit viewer or relying on agents to remember a printing convention is insufficient.
- Apply the behavior consistently to scheduled gameplay, asynchronous
  communication, and interactive sessions.

### Acceptance

- Captured model-facing results contain one copy of each payload, with the same
  game information. Verify live delivery as well as adapter behavior.
- Cover success, errors, absent structured content, and distinct additional
  content without dropping decision-relevant information.
- Full audit records and MCP clients remain usable. Measure emitted response
  size and provider usage separately; smaller responses do not imply that total
  input or cost is halved.

### Implementation

The shared [MCP configuration](../src/civ_agent/agent_settings.py) sets
`http_headers={"X-Civ-Result-Format"="structured"}` for Codex. Scheduled gameplay
and asynchronous communication use it through `CodexProcess`; the interactive
launcher uses it for new and explicitly resumed sessions. Static HTTP headers are
a [supported Codex configuration](https://developers.openai.com/codex/config-reference).

The [gateway](../src/civ_agent/proposal_gateway.py) normalizes results at the
authenticated response boundary after call handling, without changing stored
payloads. Clients without that exact opt-in retain the original text-plus-JSON
format. Authentication, tool/output schemas, command validation, and receipts are
unchanged. The header selects representation; it grants no additional access.

[mcp_results.py](../src/civ_agent/mcp_results.py) shares the existing audit
normalizer with live delivery. It removes only unannotated text blocks whose
parsed JSON exactly matches `structuredContent`, preserving JSON value types.
Distinct text, annotated blocks, images/audio/resources, metadata, and `isError`
remain. Responses without structured content keep their original content.
Source objects and durable data are not mutated; audit normalization retains its
previous behavior.

An ordinary model-facing reply therefore remains a valid MCP envelope with one
game payload, for example:

```json
{"content":[],"structuredContent":{"error":"DETAIL_REQUIRED"},"isError":true}
```

This works even when an agent prints the entire result. It requires no additional
prompt instructions or model-side printing convention.

### Validation and live results

Automated checks passed **88 tests plus 26 subtests**, covering result preservation,
real HTTPS with the MCP SDK in both formats, unsupported-header fallback,
authentication, errors, cross-format receipt replay without a second action,
audit records, interactive/resume configuration, gameplay/chat scheduling, and
measurement. Ruff formatting/lint and Pyrefly passed. No engine changes or package
build were needed.

Two isolated Luna/medium jobs used the same round-zero checkpoint as the selective
discovery comparison, with SHA-256
`c771aff31eb4aff44bb100b1646531595cdd4aa4bd6053c63793f5f708d6fdc8`.
Rome completed its native turn; the communication job replied successfully. Both
services were stopped afterward. Artifacts are in
`/tmp/unciv-response-gameplay-20260920-1` and
`/tmp/unciv-response-communication-20260920-1`.

| Metric | Gameplay | Communication |
| --- | ---: | ---: |
| Complete printed results matching audit records | 36 | 8 |
| Duplicate JSON text blocks | 0 | 0 |
| Printed error responses, included above | 4 | 2 |
| Serialized envelope characters | 87,171 | 2,313 |
| Equivalent legacy characters for those same payloads | 183,397 | 4,608 |
| Character reduction for identical payloads | 52.5% | 49.8% |
| Model requests | 35 | 9 |
| Total input tokens | 1,154,796 | 110,526 |
| Cached input tokens | 1,081,856 | 98,048 |
| Uncached input tokens | 72,940 | 12,478 |
| Output tokens, including reasoning | 4,378 | 1,502 |
| Reasoning output tokens, included above | 1,913 | 391 |

Character comparisons use consistent compact JSON serialization of the captured
envelopes and the equivalent legacy representation of the same payloads. They do
not estimate tokens. Provider usage reconciled with service and host telemetry.

Gameplay had 39 audited responses. One oversized printed batch was truncated,
leaving three responses unavailable for complete model-output comparison. Their
full payloads remain in the audit. Large observations and batches still need task
2; deduplication does not remove the runtime's output limit. All eight chat
responses were captured completely. Boundary exceptions can leave a call-result
database entry empty while its error is retained in MCP audit history: one in
gameplay, two in chat. The verifier accounts for both stores.

Compared with the earlier selective-discovery runs, these jobs made more model
requests (35 versus 27 and 9 versus 6) and used more total input despite smaller
responses. Uncached input decreased. Agent decisions and cache hits varied; these
runs establish single-payload delivery, not a repeatable turn-cost reduction.
Interactive/resume wiring is covered automatically; live model runs cover service
gameplay and asynchronous communication.

Reproduce using [live_tool_discovery.py](../tests/live_tool_discovery.py) with fresh
state directories and the commands in the
[discovery reproduction instructions](Selective-tool-discovery.md#reproduction-and-automated-validation).
Then run:

```sh
.venv/bin/python -m tests.verify_tool_results_live /tmp/unciv-response-gameplay-20260920-1
.venv/bin/python -m tests.verify_tool_results_live /tmp/unciv-response-communication-20260920-1
```

The [verifier](../tests/verify_tool_results_live.py) writes `response-metrics.json`,
compares complete printed payloads to durable audit/call records, reports
truncation, and reuses the existing model-settings/completion/accounting checks.
It exports counts and sizes without copying game payloads or raw reasoning.

## 2. Use selective reads

Existing filters include `research_details(names=["Mining"])`,
`construction_details(target="city1", names=["Monument"])`, local `map_region`
queries, and `player_details(section=...)`. Unit and city inspections now accept
explicit focused sections while retaining full reads. See
[compact observations](Compact-observations.md) for the current contract.

### Scope

- Improve instructions and examples: start with compact choices, then request
  named details for relevant candidates. Keep complete catalogues and long-term
  planning information available on demand.
- Add focused unit/city sections. Implemented calls include
  `unit_details(target="unit2", sections=["movement"])` and
  `city_details(target="city1", sections=["production"])`. Omitted sections or
  `["all"]` retains the previous complete read.
- Keep opportunity signals in the overview so agents discover promotions,
  bombardment, abilities, and other available actions. Retain costs, consequences,
  native availability, and required confirmations in the relevant detailed section.
- Scope evidence and required-read checks to the fields actually delivered.
  Inspecting movement must not count as reading disband or gift consequences.

### Acceptance

- Focused reads return the requested information with smaller payloads; full
  information remains reachable through explicit reads.
- Named research/construction and focused entity reads appear in live traces
  without preventing useful strategic exploration.
- Visibility, stale-detail handling, and consequence-inspection checks continue
  to work. A section read cannot authorize an unrelated action's required inspection.

### Implementation

[entity_sections.py](../src/civ_agent/entity_sections.py) defines explicit unit and
city projections and their action-inspection mappings. Identity/basic status
accompanies each section. Native costs, paths, unavailable reasons, warnings and
confirmations stay beside their options. Multiple sections combine; full reads
retain future fields. The overview's promotion count now counts available choices
instead of keys in the promotion-status object.

Only delivered projections become evidence. Refreshing the underlying snapshot
recomputes prior projections for change detection without marking them read.
Changes to an unrelated section do not stale a plan based on the focused section.
The gateway requires the applicable section for each command, with full reads as
compatible supersets. Construction requires a named read or city production plus
purchases, preserving offer/confirmation coverage. Destination and mission
previews still require the exact native query and current revision.

Shared scheduled, communication and interactive instructions now recommend named
choices and focused reads. Tool descriptions give section meanings and preserve
broader catalogues for strategic planning.

## 4. Reduce command–refresh cycles

Previously a successful command invalidated the cached observation and required
another `player_turn`. Successful gameplay actions now return a refreshed
`stateUpdate` with the native receipt.

### Scope

- After executing a command, refresh the permitted snapshot internally and
  return its receipt with a compact update: what changed, new alerts or pending
  decisions, and observations that need refreshing.
- Use deterministic changes to avoid repeating unchanged sections: replace
  changed overview sections and upsert/remove collection rows by stable ID.
  Full overviews remain available for startup and recovery.
- Derive updates only from player-visible state. Represent additions, changes,
  and removals explicitly; loss of visibility must not disclose hidden movement
  or imply destruction. Preserve relevant command events and notifications that
  a final-state comparison alone could miss.
- Associate updates with the resulting observation revision. Register only
  delivered information as read, and invalidate affected stale details/previews.
  An internal snapshot refresh does not establish that the agent inspected it.
- Preserve durable command receipts and replay behavior. If a command succeeds
  but refreshing fails, report the applied receipt and require a refresh without
  executing the action again. Handle end-turn handoff without exposing the next
  player's state. Keep full overview reads for startup, recovery, and explicit use.

### Acceptance

- An agent can execute successive actions with fewer explicit `player_turn`
  requests, while still performing required inspections and turn review.
- Cover changed/removed entities, new discoveries, lost visibility, pending
  decisions, stale previews, handoff, and visibility boundaries.
- Cover refresh failure after a committed action, receipt replay, reconnect,
  restart, and rollback without duplicate actions or unread evidence being accepted.
- Measure model requests and visible response size separately from internal host
  reads: refreshing internally can retain the host read while saving a model round trip.

### Implementation and validation

The [command-update contract and evaluation](Command-state-updates.md) describe
base-state matching, deterministic section/row changes, unchanged inspection
retention, invalidation of changed details/previews, atomic evidence/response
delivery, receipt recovery and handoff. Removals assert absence from the permitted
view. Native receipts and full alert content remain intact.

A matched Luna/medium opening completed with **one explicit overview read**, down
from seven, while the host still made seven overview reads internally. Model
requests fell from 22 to 19, input from 520,326 to 370,174 tokens, and elapsed time
from 123.8 to 103.1 seconds in this sample. All six deltas reconstructed their
permitted snapshots; delta bodies were 60.0% smaller than equivalent full updates.
No tool calls were rejected. Single runs do not establish a stable speedup or
cost reduction.

Validation passed 509 tests plus 90 subtests, Ruff and Pyrefly. Live artifacts and
reproduction commands are in the linked contract. No engine or package build was needed.

## 5. Make commands easier to issue correctly

Combat options already include copyable command arguments. Extend that pattern
to other choices so agents do not have to reconstruct action-specific shapes.
For example, a research detail could include:

```json
{
  "name": "Mining",
  "command": {
    "kind": "SET_RESEARCH_QUEUE",
    "queue": ["Mining"]
  }
}
```

### Scope

- Start with research and construction; extend the same pattern to relevant
  movement previews, improvements, promotions, purchases, and other choices.
  Keep templates limited to requested or relevant options to control payload size.
- Use native offered choices, actual entity handles, and the validated command
  schema. Include availability, costs/consequences, and any required inspection
  or preview request alongside the option.
- Let agents select the strategy and supply their own intent and valid plan
  references. Do not prefill consequential-action confirmations or manufacture
  consent. Revalidate native legality when executing a copied command.
- Make argument errors identify the expected action shape and recovery step.
  The existing runtime renders parts of the command union as `unknown`; copyable
  arguments should remain useful even when that generated signature is incomplete.

### Acceptance

- Representative options provide valid argument shapes, including research's
  `queue`, construction's `target`/`selection`, and structured `intent` guidance.
- Copying a template still requires applicable inspections, explicit confirmations,
  current affordability, and native legality. Stale templates fail safely.
- Live traces show whether malformed commands and recovery calls decline; record
  added template size alongside any savings from fewer retries.

### Implementation

[action_templates.py](../src/civ_agent/action_templates.py) adds validated arguments
after selecting observation fields. Named research includes the engine's full
prerequisite path; named policy/construction options and available movement,
orders, promotions, worker jobs, purchases, citizen controls, queue management,
capture options, unit economy, abilities, gifts and spaceship assembly expose
their applicable commands. Native movement/mission previews expose matching
destination commands; air bases expose a required preview request.

Broad research/policy/construction catalogues avoid per-option command expansion.
Templates do not supply intent or confirmation booleans. Confirmation metadata is
separate from arguments. Templates convey native options and never establish
legality or bypass inspection, revision, confirmation or affordability checks.

Malformed commands return a schema for the selected kind, missing/invalid fields,
and structured-intent guidance. Missing observations return an actionable refresh
query. Argument errors explain that they applied nothing and do not themselves
require an overview read. These responses are durable and replayable.

The measured communication retry loop is also addressed: `note.plan` is explicitly
an optional existing plan ID, with a precise reference error; notes do not accept
`sources`. Memory shape errors explain the selected content schema and beliefs'
delivered-source requirement. Invalid values and arbitrary field names are not
echoed. Evidence and memory review requirements remain enforced.

## Tasks 2 and 5: validation and live results

Automated coverage exercises section isolation and canonical queries, full reads,
native warnings, available/unavailable templates, exact destination previews,
native prerequisite queues, scoped evidence invalidation, refresh/replay behavior,
safe argument errors, memory references and delivered-source checks. Gateway
tests confirm copied purchases still pass through native rejection and explicit
confirmation handling. Existing engine mechanics were not changed.

Validation passed **496 Python tests plus 90 subtests**, Ruff lint/format and
Pyrefly. No engine or package build was needed.

The same saved round-zero position and scripted communication question used for
task 1 were run again with **gpt-5.6-luna, medium reasoning**. Artifacts:

- `/tmp/unciv-selective-gameplay-20260920-1`
- `/tmp/unciv-selective-communication-20260920-1`

Rome completed its native turn. The communication job sent its reply and saved a
note on its first attempt. All 35 printed tool replies matched durable audit
records, with no duplicated JSON or truncated output. Provider usage and tool-call
counts reconcile with service/host job reports. Both runs stopped their services.

| Metric | Task 1 gameplay | Tasks 2+5 gameplay | Task 1 communication | Tasks 2+5 communication |
| --- | ---: | ---: | ---: | ---: |
| Model requests | 35 | 22 | 9 | 5 |
| Player-tool calls | 39 | 31 | 8 | 4 |
| Host-tool calls attributed to Rome, including service traffic | 93 | 87 | 22 | 17 |
| Overview reads | 10 | 7 | 0 | 0 |
| Total input tokens | 1,154,796 | 520,326 | 110,526 | 57,808 |
| Cached input tokens | 1,081,856 | 449,280 | 98,048 | 43,008 |
| Uncached input tokens | 72,940 | 71,046 | 12,478 | 14,800 |
| Output tokens, including reasoning | 4,378 | 3,160 | 1,502 | 618 |
| Reasoning tokens, included above | 1,913 | 1,141 | 391 | 118 |
| Elapsed seconds | 121.9 | 123.8 | 46.0 | 20.9 |
| Malformed gameplay commands | 2 | 0 | — | — |
| Memory-update attempts | 2 | 1 | 5 | 1 |

Host counts include communication subscriptions, reporting, heartbeats and startup/
shutdown traffic across each isolated run. They measure a different layer from
model-facing player tools; each layer retains its own per-API breakdown.

The gameplay trace contains four focused unit reads and one focused city read,
two named research queries and two successful named construction queries. Their
five focused responses total **14,664 characters**, versus **43,273** for equivalent
full reads from the same permitted snapshots, a **66.1% reduction**. Both sides
include the new command templates. Across delivered observations, command
templates/inspection metadata add **4,412 characters**. Four issued actions match
previously delivered templates: founding, research, construction and movement.
Matching arguments establish usable offers, not whether the model literally
copied them. No extra permissions derive from that match.

Gameplay still attempted two detail reads after queueing construction without an
overview refresh. Both returned `OBSERVATION_REQUIRED`; the agent recovered. Task
4 addressed these refresh cycles in the subsequent evaluation above. The earlier
communication run had four failed memory-update
attempts (two shape errors and two rejected changes); the new run had none.
Rejected changes are now counted separately from top-level tool errors so partial
memory results are not missed.

This is one matched pair per job type, with different strategic decisions and
cache behavior. It demonstrates actual focused reads and fewer retries in these
runs, not a stable cost or speed improvement: gameplay time was similar and
communication uncached input increased. It does not establish full-game readiness.

Reproduce with [live_tool_discovery.py](../tests/live_tool_discovery.py), using new
state directories and the same checkpoint/options as the task-1 runs. Then run:

```sh
.venv/bin/python -m tests.verify_selective_actions_live /tmp/unciv-selective-gameplay-20260920-1
.venv/bin/python -m tests.verify_selective_actions_live /tmp/unciv-selective-communication-20260920-1
```

The verifier writes `selective-action-metrics.json`, reuses the provider/audit
checks, reconciles tool counts, and compares focused projections with full reads
of their persisted permitted snapshots. It retains per-tool timing/counts and
exports measurement summaries without game text or raw reasoning.

## Shared measurement

Compare bounded runs from matched saved positions using `gpt-5.6-luna` with medium
reasoning. Include gameplay and, where applicable, communication and interactive
delivery. Preserve baseline artifacts and verify successful task completion.

Record total, cached, and uncached input; output/reasoning usage; model requests;
player-tool and host API counts; response characters; explicit overview reads;
rejected commands/recovery; and elapsed time. Reconcile provider usage with the
existing service/host telemetry. Character counts describe payload size, not exact
token attribution. Report variability and failures rather than treating one run
or a smaller payload as proof of lower cost.

These efficiency tasks do not close the separate Ancient-start-through-native-end
acceptance requirement in [full-game readiness](Vanilla-readiness.md).
