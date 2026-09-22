# Gameplay read and recovery audit — September 22, 2026 UTC

## Conclusion

The current run provides little support for adding general multiple-target or
combined-decision read APIs as the next efficiency change. Independent reads are
already heavily batched. The clearest problems are inconsistent startup memory,
memory recovery after the agent's own actions, discovery round trips, and native
movement previews fetched too early.

The run paused on Greece's turn 11 after the agent returned without `END_TURN`.
The agent described another stale-memory rejection as terminal even though the
host had not returned `REPEATED_STALE_ASSUMPTIONS` or recorded a terminal failure.
Its successful research and movement commands remain committed.

The measurements below describe the original run. The
[implementation follow-up](#implementation-follow-up) records the subsequent
fixes and offline checks separately. No live game API/model requests were made,
and the match was not resumed or modified.

## Scope and method

- Match directory: `/home/vgopalas/.local/state/unciv-host/vanilla-26_09_21`.
- Epoch: `g3qg9x8gaxzf1k`.
- Initial fixed sample: turns 0–8, 27 completed player turns and 28 model jobs.
  Rome's turn 4 required a second job after the first returned without a handoff.
- Extended sample after the reported stop: 34 completed player turns plus
  Greece's unfinished turn 11, totaling 36 model jobs. France had not begun turn 11.
- All 36 provider traces identify `gpt-5.6-luna` with medium reasoning.
- Read the service's SQLite call records with `mode=ro`, archived service events,
  activation metadata, provider traces, and native turn telemetry. Matched every
  model-issued recorded call to its service event and execution group.
- Counted model requests from deduplicated provider `token_usage_record`
  response IDs, not from tool counts. Matched calls to executions by service-event
  timestamps and executions to provider response boundaries.
- Provider, service, and native telemetry token totals reconcile. The unfinished
  Greece turn has complete model usage but incomplete turn accounting/handoff.
- No provider tool outputs in this sample were marked truncated.

The initial extraction is in `/tmp/unciv-pattern-audit-20260922/`; the extended
extraction is in `/tmp/unciv-pattern-audit-20260922-through-stop/`. Each has
`data.json`, `summary.json`, and `sequences.txt`; supplementary analyses are in
`patterns.json`. These temporary artifacts contain calls and results, not raw
model reasoning. The read-only extraction script is `/tmp/audit_unciv_reads.py`:

```sh
.venv/bin/python /tmp/audit_unciv_reads.py 11 /tmp/unciv-pattern-audit-20260922-through-stop
```

The durable evidence remains in the match directory and the provider traces
referenced by the extraction. Temporary analysis files may be removed by the OS.

## Measurements

| Measurement | Initial turns 0–8 | Through the turn-11 pause |
| --- | ---: | ---: |
| Model jobs | 28 | 36 |
| Model requests | 248 | 334 |
| Model-issued player-tool calls | 248 | 337 |
| Service-prepared startup reads | 112 | 144 |
| Game-detail reads | 100 | 134 |
| Executions containing multiple game reads | 29 | 41 |
| Game reads in those executions | 78 | 106 |
| Executions combining map and unit reads | 22 | 26 |
| Executions used only for tool discovery | 48 | 61 |
| Executions used only for memory reads/updates | 62 | 89 |
| Memory-update calls with rejected edits | 18 | 26 |
| Rejected individual memory edits | 29 | 43 |
| Jobs with inconsistent startup plan/review copies | 13 | 17 |
| Repeated same-route previews after intervening commands | 2 | 6 |
| Input tokens, including cached input | 5,598,327 | 7,844,874 |
| Cached input tokens, included above | 4,702,464 | 6,694,656 |
| Output tokens, including reasoning | 49,582 | 70,304 |
| Reasoning output tokens, included above | 17,538 | 24,433 |

An execution is one model-emitted tool program. Grouping several reads in one
execution already avoids another model decision between those reads. Memory-only
and discovery-only execution counts describe where requests went; they are not
counts of requests proven removable.

## 1. Multiple-target reads: no observed model-round saving

All cities inspected were `city1`. In the initial sample there were no reads of
multiple distinct units of the same kind of detail between successful game
commands. Opening settler and warrior inspections were separated by founding the
city, which changed the game state.

The extended sample contains one such multiple-unit case: Rome turn 11. Model
request 2 already batches city details, Worker work/movement details, a map read,
and Warrior movement/combat details. A multiple-target endpoint would reduce
some repeated call syntax but would not eliminate a model request in that case.

This does not establish later-game behavior with larger armies or empires.
Defer this API expansion until those traces demonstrate a bottleneck.

## 2. Decision bundles: most independent reads already share a request

Through the stop, 106 game reads were grouped into 41 executions. Map and unit
details shared an execution 26 times. The three construction catalogue-to-named
detail sequences were Greece turn 0, France turn 0, and Greece turn 6. Those
allow the agent to choose candidates after seeing the catalogue; replacing them
with all candidate details would trade more context for fewer decisions.

The observed map-to-destination-preview sequences often require the agent to
select a destination from the map. A larger response cannot choose that
destination on the agent's behalf.

There is no measured recurring gap here that justifies general movement,
production, and research bundle APIs. A specific missing field could still
justify a narrow change after identifying its actual follow-up cost.

## 3. Memory consistency is the strongest concrete issue

Across the extended sample, 26 of 64 memory-update calls contain rejected edits,
affecting 18 player turns. Individual rejections are:

- 33 `STALE_ASSUMPTIONS`.
- 6 `SOURCE_NOT_DELIVERED`.
- 4 `REVIEW_EVIDENCE_FIRST`.

Twenty-five of the 33 stale-edit conflicts include a scheduled plan-review flag;
the other eight include observation-change flags without a scheduled-review flag.
Some scheduled-review cases also include observation changes. These are
host-generated version changes, not evidence of another player editing a plan.

### Startup inconsistency

Seventeen jobs contain 28 plan copies where the private startup memory says
`needsReview: []`, while the prepared overview in the same prompt already flags
that plan for review. The job's delivered memory version is consequently older
than the record that the service validates against.

For example, Greece turn 3 receives an unflagged `mem1` in private memory and a
scheduled-review flag for `mem1` in the prepared overview. Its first revision
fails with expected version 1 versus current version 2. Requests 3–6 are:

1. Attempt the revision; receive `STALE_ASSUMPTIONS`.
2. Discover `memory_read`.
3. Read the current plan.
4. Submit the revision again successfully.

The code order explains the mismatch: the gateway starts a job with a selected
memory snapshot, then startup reads advance the game turn and flag due plans.
Those reads update observation evidence without delivering the newly revised
private plan records. See `ProposalGateway.open`, `prepare_startup`,
`CivilizationState._advance_turn`, and `CivilizationState._observe_player`.

### Changes during the job

Successful actions can flag plans and advance their versions. Returning the new
overview or its review summary does not deliver the updated full plan record.
Agents can therefore see that a plan needs revision yet have an obsolete version
when attempting it. Broad assumptions citing `observation:overview` amplify this
because many otherwise unrelated actions change that evidence.

Rome turn 8 also demonstrates a second problem: after reading three current
plans, the next revision attempt rejects all three for undelivered sources.
It then reads map, city, and unit details before succeeding. Greece turn 5 makes
two consecutive revisions with an undelivered source. These require precise
source guidance and correct provenance handling, not a general entity batch API.

Recommended first change: prepare private memory and native observations
coherently, and deliver selected changed plan records with action updates when
the host changes them. Mark records read only when their actual content is
delivered. Preserve rejection of genuinely stale assumptions and unread evidence.

## 4. The turn-11 stop

Scheduler status is `paused`, with reason:

> Agent returned without a committed handoff. Resume preserves confirmed work and starts a fresh planning job.

Greece's activation is `70af8ddc-3061-4016-b80a-063331de0d91`. It made 16 model
requests, using 453,206 input tokens, including 418,816 cached tokens, and 4,748
output tokens, including 1,387 reasoning tokens.

Relevant sequence:

1. Initial memory edits encounter review/staleness checks; subsequent reads and
   revisions succeed.
2. Research changes to Bronze Working and the Mining alert is acknowledged.
3. A destination command encounters a stale plan; the plan is read and revised
   successfully, and the destination command then succeeds. The Warrior reaches
   `(5, 1)` with no movement remaining.
4. A final memory batch accepts one edit and rejects two others:
   `REVIEW_EVIDENCE_FIRST` for `mem2` and `STALE_ASSUMPTIONS` for `mem1`.
5. The agent emits a final response saying that a repeated stale-memory conflict
   halted the handoff. It never calls `END_TURN`.

No `REPEATED_STALE_ASSUMPTIONS` response or persisted `runFailure` exists. The
earlier successful revisions separate these failures; the agent appears to have
interpreted multiple stale errors during the turn as a terminal failed recovery.
Rome's first turn-4 attempt ended similarly and was later resumed in a second job.

The scheduler stopped safely on the missing handoff; the engine's confirmed work
was preserved. Clarify the prompt's recovery boundary and explicit terminal
signal alongside fixing the stale-plan delivery issue. Do not weaken the actual
terminal failure behavior to keep a broken run going.

## 5. Preview ordering creates measurable repeat work

Six of 21 movement-preview calls repeat the same unit/destination after an
intervening native command. The repeated model-visible responses are identical,
but the previous native preview has expired, so the second read is required under
the current contract.

| Player/turn | First preview request | Intervening action | Repeated preview request |
| --- | ---: | --- | ---: |
| France 0 | 8 | Queue construction | 11 |
| Greece 6 | 2 | Acknowledge alert, research and construction changes | 7 |
| Greece 9 | 4 | Adopt policy | 7 |
| Rome 10 | 3 | Queue construction | 6 |
| Rome 11 | 3 | Move the Worker after previewing both units | 5 |
| Greece 11 | 3 | Research change and alert acknowledgement | 9 |

The first previews may inform planning, so six repeats do not prove six safely
removable calls under identical decisions. However, finishing unrelated choices
before fetching the final movement preview is a concrete improvement to test.
Batching multiple units' previews can be counterproductive when executing one
movement expires the other preview. Keep native revision checks intact.

## 6. Discovery and response size

Sixty-one of 334 model requests were used only for discovery. Nineteen discover
only `memory_read`, typically after a rejection. Fixing recovery should reduce
some of these. Unit details were discovered in 34 jobs and used in 33; map reads
were discovered in 28 and used in 26. Movement previews were discovered in 28
jobs but used in only 12.

Test whether including the small, frequently used unit/map schemas in the stable
startup instructions saves total tokens and requests. Keep uncommon schemas
selective; do not preload the complete catalogue. Reduced request counts alone
would not establish savings if the new startup text costs more overall.

Thirty map reads produced 379,848 serialized JSON characters, about 54% of the
706,490 characters in game-detail responses. These figures exclude startup,
discovery, memory, and command responses. They measure serialized payload size,
not provider tokens or token cost. Map formatting is a stronger measured candidate
for response-size work than general-purpose endpoint aggregation; preserving
terrain, visibility, threats, and settlement information remains necessary.

## Revised priorities

1. Fix coherent startup memory delivery and delivery of changed plans after actions.
2. Clarify terminal recovery handling, including successful recovery followed by
   a new conflict; improve missing-evidence recovery guidance.
3. Fetch final native previews close to the commands that consume them.
4. Measure a small stable set of common read schemas at startup.
5. Evaluate compact map serialization using these actual responses.
6. Revisit multiple-target and combined-decision APIs when larger empires produce
   repeated avoidable sequential reads.

Validate correctness deterministically, then compare matched gameplay checkpoints.
These observations identify opportunities; they do not establish a speedup or
justify skipping evidence, visibility, or native legality checks.

## Implementation follow-up

Implemented in the Python service, with the existing engine pin:

1. **Coherent startup memory.** Prepare native/social observations first, refresh
   the dispatch-selected private records through accounted startup memory reads,
   then render private context once. This covers scheduled reviews, evidence
   changes and records that expire during preparation. Other cached evidence is
   not marked as delivered.
2. **Changed private records with action results.** `memoryUpdates` accompanies
   overview reads and successful native action results. It delivers newer versions
   of private records the job already knows, including flags and retirements.
   The update and response cache commit atomically; replay cannot rewind delivery.
   `reviewSources` exposes the evidence requiring review. Receiving the private
   record still does not count as reading a changed unit, map or other detail.
3. **Host-owned recovery termination.** The host already persisted per-operation
   retry state and stopped all models on a repeated failure. Prompts now explicitly
   defer to `recoverable` and the host's terminal error. Successful operations
   reset their counter; a later conflict is a new recovery. Missing-evidence
   errors now supply `requiredSources` and concrete reads, aggregating multiple
   unread sources and assumptions into one recovery response.
4. **Preview ordering and smaller discovery overhead.** Prompt and preview-tool
   guidance puts unrelated native actions before a final movement/mission preview.
   Native expiry checks are unchanged. Gameplay startup adds the frequently used
   `memory_read`, `unit_details` and `map_region` schemas to its stable core;
   movement/mission previews remain selectively discoverable.
5. **Lossless compact maps.** `map_region` defaults to shared `tileDefaults` with
   explicit per-tile overrides. Core scanning fields stay on the tile, missing
   values remain distinct from nulls, and `format: "full"` remains available.
   Fact comparisons expand defaults so representation changes alone do not flag
   plans. Region selection, fog and world-wrap behavior are unchanged.

### Offline payload measurement

Applied `compact_tiles` to each of the 30 saved map responses in the extended
sample. Serialized complete response objects using UTF-8 JSON characters,
`ensure_ascii=False`, `separators=(",", ":")`, and sorted object keys. Expansion
matched every original tile, including JSON types: **801 tiles round-tripped**.

| Response group | Original characters | With compact maps | Reduction |
| --- | ---: | ---: | ---: |
| 30 map responses | 379,848 | 146,711 | 61.4% |
| All 134 game-detail responses | 706,490 | 473,353 | 33.0% |

The full-detail comparison changes only those map payloads. It excludes startup
schema growth, memory updates, model requests and provider tokenization. These
numbers do not establish a live token-cost or turn-time improvement. The temporary
per-response measurements are `/tmp/unciv-map-compaction-20260922.json`; inputs
are the extracted call results described above.

### Regression coverage

Validation: **593 tests and 90 subtests passed; 1 opt-in installed-Codex capture
skipped**. Ruff lint/format checks, Pyrefly and `git diff --check` passed.

Tests exercise startup review/expiry in gameplay and communication, delivery of
changed plans and known retirements, refusal to cite unread evidence, selective
recovery reads, atomic rollback of evidence after response-save failure, replay
after newer private edits, and successful recovery followed by a new conflict.
Existing lifecycle tests verify that the host interrupts models without their
cooperation and preserves diagnostic state across restart. Compact-map tests
cover expansion, missing/null and boolean/number distinctions, fog, wrap, small
regions and factual evidence comparison.

Live validation remains: resume from a saved checkpoint using the updated service
and measure model requests, tokens, cache usage, command correctness and elapsed
time. The current paused match was not resumed as part of these fixes.
