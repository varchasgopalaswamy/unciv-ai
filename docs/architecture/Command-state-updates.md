# State updates with command receipts

Successful gameplay actions return `stateUpdate` beside their native receipt.
The service refreshes its player-bound snapshot internally. An agent normally
reads `player_turn` once at the start, then uses these updates and focused detail
reads for the rest of its turn.

This is implemented in [state_updates.py](../../src/civ_agent/state_updates.py) and
the [proposal gateway](../../src/civ_agent/proposal_gateway.py). Native gameplay
rules and the underlying host protocol are unchanged.

## Deterministic changes

`player_turn` returns a complete compact overview with a `stateId`. Subsequent
updates use two levels of changes:

1. **Overview sections:** replace a section only when its JSON value changes.
   A changed research queue does not repeat rules or victory text.
2. **Collections with stable IDs:** send added/changed rows and removed IDs for
   units, cities, alerts, notifications, known civilizations and city-states.
   Each changed row replaces its previous row completely. Optional `order`
   preserves native ordering when membership or order changes.

Other lists and nested sections are replaced as whole values. This keeps updates
readable without requiring models to apply numeric array-index edits or deeply
nested merges. Small repeated fields within a changed row are an intentional
tradeoff. Comparisons preserve JSON types, including `true` versus `1`.

An illustrative response fragment, omitting the native receipt's fields:

```json
{
  "stateUpdate": {
    "kind": "delta",
    "baseStateId": "s-before",
    "stateId": "s-after",
    "set": {"researchQueue": ["Pottery"], "canEndTurn": true},
    "removed": [],
    "collections": {},
    "invalidatedDetails": []
  }
}
```

In a collection update, `{"key":"handle","upsert":[...],"removed":["unit1"]}`
replaces complete rows identified by `handle` and removes `unit1`. Omitted
sections/rows stay unchanged. Empty values in `set` explicitly clear a section;
top-level `removed` deletes fields absent from the new view.

`stateId` is opaque and derived from the job, epoch, native revision and delivered
overview. Agents never supply it as command authorization. A delta must match the
receiver's `baseStateId`; a repeated update already at its `stateId` is a no-op.
For a missing or mismatched base, read `player_turn` to resynchronize. The module
includes a reference reducer. Full explicit reads remain available at any point.

## Inspections and evidence

The service compares previously inspected projections with their new permitted
values. Unchanged projections remain inspected. Changed or removed projections
appear in `invalidatedDetails` as exact queries; reread them only when needed for
another action. An unrelated city's production change therefore need not expire
a unit's movement inspection.

Native movement/mission previews expire whenever the native revision changes.
Their routes and consequences require another native query.
Finish unrelated research, production and alert actions before fetching the final
preview, then issue its command. Ordinary independent reads can still be batched;
batching several final previews before any movement can waste the later previews.

Independent native reads (`research_details`, `policy_details`, `victory_progress`, `player_result`)
and social/trade reads (`chat_read`, `trade_options`, `trade_read`, `agreement_list`,
`agreement_read`) return their exact `query`. They expire conservatively whenever
an observed native revision or social context changes. Their content cannot be
reconstructed from the cached overview, so the service sends invalidation notices
without automatically requerying them. Reread only information needed for a later
decision. Historical memory and immutable action history are not current-state
inspections and do not participate in this invalidation.

Explicit `player_turn` reads and communication mutation responses can also carry
top-level `invalidatedDetails`. This is response metadata, excluded from the
overview and its state ID. Negotiation/recovery cleanup preserves pending notices
until the next response can deliver them. A full overview does not itself refresh
an independent native or trade read.

Only the overview reconstructed from the delivered base and delta counts as newly
read. Internally refreshed details remain unread until requested. Changed detail
cannot support a current assumption without a new read; an older delivered version
remains citeable as historical evidence.

Overview evidence and the replayable response commit in one SQLite transaction.
Changed private records already read by this job also arrive atomically in
top-level `memoryUpdates`, alongside `stateUpdate` or an explicit overview. Each
entry contains its private `version` and public `record`, including review flags
and `reviewSources`. This list is response metadata, not part of the overview or
its `stateId`; the reference reducer removes it from explicit-read bases.
Keep the newest private record by ID on replay. An updated record does not deliver
its cited evidence; relevant changed details still require explicit inspection.
A failed response write rolls back the new observation evidence. The native
action receipt was already saved separately and survives. Internal projection,
validation or persistence errors stop the run with `STATE_UPDATE_FAILED` and
durable diagnostics; they are not treated as routine refresh outages.

## Visibility and events

Updates compare permitted observations, never hidden world state. Removals mean
absence from that view; they do not assert destruction or reveal a hidden
destination. New exploration changes the compact map summary and invalidates
affected inspected regions. Terrain and visibility details remain selective
through `map_region`.

Changed alerts/notifications retain full content; unchanged ones need not repeat.
Native feedback, combat results, purchase outcomes and dismissed notification
bodies remain in receipts, including events not inferable from the final snapshot.
A refresh can include intervening changes; the receipt remains the source for
what the command actually did.

## Recovery and handoff

| `stateUpdate.kind` | Meaning |
| --- | --- |
| `delta` | Apply changes to the matching base; inspect new decisions and relevant invalidated details |
| `full` | Receipt recovery has no trusted in-memory baseline; use its complete `overview` |
| `unavailable` | Retain the successful receipt. For a transport outage, follow the `player_turn` recovery query; if the response has terminal `STATE_UPDATE_FAILED`, stop for operator inspection |
| `turn_ended` | Stop the gameplay job; ordinary `END_TURN` does not query a post-handoff overview |

Retrying a call ID returns its saved response without executing the action or
rolling the service's current observation backward. Interruption after the native
receipt but before the response can recover with a fresh read/full overview,
without repeating the action. Cancellation clears the in-memory baseline and
inspections, retaining their invalidation notices for the next response. Restart and rollback require fresh jobs/baselines; existing epoch
checks reject the abandoned timeline.

Gameplay trade/friendship settlements, social commitment changes and explicit native diplomatic actions also
refresh state. Native-AI offers return pending without waiting for the AI's turn.
Ordinary communication sends do not force a game-state read; communication jobs
receive invalidation notices after mutations and retain their explicit-read behavior. A negotiation that rejects a command still
requires a fresh decision. Unknown command outcomes continue through durable
receipt reconciliation.

## Live evaluation — September 20, 2026

A Luna/medium job used the same saved Rome opening as the tasks 2+5 comparison.
Artifacts: `/tmp/unciv-updates-gameplay-20260920-1`. Rome completed its native turn,
including research, founding, construction and native destination movement. The
isolated services stopped afterward.

| Measurement | Tasks 2+5 | With command updates |
| --- | ---: | ---: |
| Model requests | 22 | 19 |
| Model-facing player-tool calls | 31 | 20 |
| Explicit overview reads | 7 | 1 |
| Host overview reads, including internal refreshes | 7 | 7 |
| Rejected tool calls | 2 | 0 |
| Input tokens | 520,326 | 370,174 |
| Cached input tokens | 449,280 | 335,104 |
| Uncached input tokens | 71,046 | 35,070 |
| Output tokens, including reasoning | 3,160 | 2,475 |
| Reasoning tokens, included above | 1,141 | 822 |
| Agent job elapsed seconds | 123.8 | 103.1 |
| Cumulative player-tool handling seconds | 1.64 | 1.44 |
| Printed response characters | 60,293 | 33,409 |

Six deltas reconstructed their persisted permitted snapshots exactly, followed by
one handoff response. Deltas totaled **7,945 characters**, versus **19,852** for
equivalent full updates including the same invalidation lists: **60.0% smaller**.
All 20 printed responses matched audit records; none was truncated or duplicated.
Provider usage, service accounting and host turn reports reconciled. Seven detail
inspections expired; the agent refreshed relevant choices and finished without errors.

The host made the same number of overview reads. The change removed model refresh
requests and repeated text. Strategies and model latency varied between these
single runs, so the results do not establish a stable speedup or cost reduction.
Full-game acceptance remains separate.

Validation passed **509 tests plus 90 subtests**, Ruff and Pyrefly. Coverage includes
delta reconstruction, removals, unchanged inspections, visibility, new alerts,
failed/malformed refreshes, atomic response writes, interruption/replay, terminal
results and restart/rollback boundaries.

Reproduce with `tests.live_tool_discovery`, a new state directory, the same
checkpoint and `--purpose gameplay` (Luna/medium). Then:

```sh
.venv/bin/python -m tests.verify_state_updates_live /tmp/unciv-updates-gameplay-20260920-1
```

The verifier writes `state-update-metrics.json`, reconstructs delivered deltas
against persisted player-visible snapshots, and records model-facing counts
separately from internal host calls. It exports no game text or raw reasoning.
