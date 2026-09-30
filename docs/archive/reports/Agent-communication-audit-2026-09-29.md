# Agent communication audit — September 29, 2026

## Scope and status

Audit of gameplay behavior, after the corrections in the
[action availability audit](Action-availability-audit-2026-09-29.md).
**All eight findings and the smaller ambiguities are now corrected.** The original
findings below describe the behavior before the corrections. Existing unrelated
uncommitted changes in both repositories were preserved.

The audit traced native observations through compact overviews, selective reads,
model-facing redaction, command templates, receipts, history, memory and strategic
review inputs. It covered movement/escorts/combat, promotions, settlement,
workers, construction/purchases/citizens, economy, research/policies, civilization
abilities, major-player diplomacy, city-states, air missions, great people and
victory information. This is an audit of communication semantics, not a claim of
complete action coverage or a fresh privacy certification. Mod behavior is out
of scope.

Validation used the current host working tree and `vendor/Unciv`, based on
`e8a842721328abc94dab6ccbb713e5f5a280a231` with the preceding capture-preview changes.
These findings establish information loss or ambiguity; they do not prove that a
particular model made a bad decision because of it. No live LLM game was launched.

## Implemented corrections

| Finding | Current behavior |
| --- | --- |
| Worker orders | Native `plannedQueue` is shared with execution. `steps` and `totalTurns` include clearing, partial progress, retained queues and repairs. Detached simulation applies every step before estimating ongoing yields. One-time clearing grants and upkeep effects are separate. |
| Factual history | Every successful native receipt includes `actionSummary`, shared with the turn review. History and strategic reviews keep actual movement endpoints, whether an attack occurred, damage, captures and losses. Review `commandStatus` describes acceptance separately. |
| Quest mechanics | Every recognized native quest has a neutral `objective.requirement`, preserved through flavor redaction. This includes connection endpoints, territory discovery, tribute targets, majority religion and positive-score contest ties. |
| Civilian returns | Native consequences distinguish adding 45 city-state influence from setting the `ReturnedCapturedUnits` opinion modifier to 20. Prior major-opinion values are disclosed only for built-in AI owners. Repeated returns do not promise another +20. |
| Attack/ability counts | Rosters expose `attackOptionCount` and `attacksRemaining` separately; `availableGreatPersonAbilityCount` counts only usable choices. |
| Foreign-city proximity | Reviews read owned-city coordinates from `development.position`, preserving wrapped hex distances and ordering. |
| Construction names | A ruleset catalogue validates names. Unknown names return diagnostics, suggestions and a discovery read, without invented rows or commands. Known locked choices and puppet restrictions remain distinct. `detailLevel` replaces the misleading description-presence flag. |
| Delta wording | Tool instructions explicitly remove fields absent from an included replacement row; rows and sections not included remain unchanged. |

The smaller corrections are also implemented: upgrade requirements are labelled
`additionalResourceRequirements`; resource details split `unimprovedOwnedTiles`
and `unimprovedAlliedCityStateTiles`; cultural milestones expose an `AT_LEAST`
objective count; instant-heal and worker history describe actual effects; map
instructions distinguish unexplored tiles from remembered information under fog.
These change the information contract without adding gameplay rules. Old field
aliases, receipt fallbacks and payload migrations are not retained.

Regression coverage compares native preview/execution and read purity, checks
major and city-state return rewards, exercises all quest kinds through serialization
and redaction, and traces factual receipts through persisted history into bounded
review inputs. It also covers partial moves/attacks, one-time promotions, unknown
versus locked construction choices, and removal of optional fields by a delta.

### Implementation validation

- Full Python suite: **980 passed, 76 subtests passed, 2 opt-in checks skipped**.
  A subsequent focused run covering the final construction-contract edit passed
  **94 tests**. Logs: `/tmp/codex-output.coHu3N`, `/tmp/codex-output.gRxKPE`.
- Native worker, capture and combat suites: **46 passed**. Host integration
  coverage: **45 passed across two batches**, including worker execution,
  tactical consequences, all quest kinds, promotions, economy and victory
  observations. Logs: `/tmp/codex-output.kik0Yd`, `/tmp/codex-output.qRuNhD`,
  `/tmp/codex-output.oxqQM2`. The native run initially also encountered unrelated
  host formatting failures; these were corrected, and the final host run passed.
- `ruff check`, `ruff format --check`, `pyrefly check`, `spotlessCheck`, and
  `git diff --check` in both repositories passed. Pyrefly reported zero errors
  with 17 warnings. Gradle and pytest ran through `codex-log`.

## Original findings

### 1. Worker previews omit part of the order's time and yield effects

**Priority: high. Reproduced against actual native execution in a fresh game.**

[`observeWorkerDevelopment`](../../../src/main/kotlin/uncivai/player/WorkerDevelopment.kt)
uses the selected improvement's construction time and yield difference. A
queueable improvement may first require clearing a terrain feature, which
[`PlayerWorkerOperations`](../../../vendor/Unciv/core/src/com/unciv/logic/civilization/PlayerWorkerOperations.kt)
actually prepends to the order.

In a fresh Quick-speed fixture, an owned Grassland/Forest/Spices tile with the
necessary technologies produced:

| Measurement | Plantation option shown to the player | Executed complete order |
| --- | --- | --- |
| Duration | `turns: 4`, `firstJob: "Remove Forest"` | 3 turns clearing, then 4 turns planting: 7 total |
| Ongoing tile yield change | `Gold: +2` | `Food: +1`, `Production: -1`, `Gold: +2` |

`firstJob` does identify the preparatory action, but `turns` does not state its
exclusion. More seriously, the yield preview applies Plantation to the original
forested tile rather than comparing the result after both actions. It therefore
omits the food/production tradeoff of clearing. The current native UI helper has
the same limitation; copying that helper does not validate the composite order.

**Suggested correction:** expose the actual ordered steps and their remaining
work, distinguish final-step time from total work, and calculate ongoing yield
changes after all steps on a detached tile. Keep one-time clearing production
separate from ongoing yields. The receipt should identify which step started.

**Regression needed:** preview a clearing-plus-improvement order, execute all
native steps, and compare both duration and resulting yields. Include a partially
completed first step and a case without clearing.

### 2. The strategic adviser receives command labels in place of actual outcomes

**Priority: high. Reproduced through the current Python projection.**

[`memory_history.action_text`](../../../src/civ_agent/memory_history.py) mostly
formats the command name, target and selection, without consulting the receipt.
[`ReviewRuntime.capture`](../../../src/civ_agent/review_runtime.py) then keeps only
`action` and `outcome`, discarding the detailed command and receipt.

An accepted attack command can move the unit partway and report
`combat.attacked: false`. Both that case and a completed attack currently become:

```json
{"action":"Attack unit1","outcome":"confirmed"}
```

The adviser prompt calls these “Recent confirmed actions and outcomes,” but
`confirmed` means the command was acknowledged, not that its intended attack
occurred. Movement history similarly loses the requested and actual endpoints;
combat history loses damage, capture and destruction results. Current combat
reports can provide some additional evidence, but they do not make this history
an accurate account of the interval being reviewed. The adviser cannot retrieve
the full history through its read-only tool set.

**Suggested correction:** derive a bounded factual action summary from the
confirmed receipt and observed post-state. Keep acceptance separate from effects:
for example, “Moved to X; attack did not occur” or “Attacked Y; captured target.”
Include identities/locations needed to understand old handles. Continue excluding
the player's strategic rationale from the independent review.

**Regression needed:** completed and interrupted attacks/moves must produce
different adviser history. Verify it through the real review-input assembly,
including its size limit.

### 3. Flavor-text removal also removes mechanical quest instructions

**Priority: medium. Reproduced through `model_view`.**

[`model_view`](../../../src/civ_agent/model_view.py) removes every recognized
quest's `description` when an `objective` exists. However,
[`CityStateQuestObjective`](../../../src/main/kotlin/uncivai/player/CityStates.kt)
contains only a kind, issuer, optional target name and investment bonus.

The bundled Route quest says to connect **the player's capital** to the
city-state by **road**. The model instead receives essentially:

```json
{"name":"Route","objective":{"kind":"Route","issuer":"Geneva"},"influenceReward":50}
```

The location, when disclosed, does not restore the capital/road requirement.
Other descriptions explain what to do, such as finding a civilization's territory
or demanding tribute from the named city-state. A kind/name alone is not a full
replacement for those mechanics.

**Suggested correction:** preserve a neutral mechanical objective, or provide
structured completion requirements derived from the native quest. Remove the
appeal to gratitude or obligation without removing the task. Retain the existing
distinction between the quest issuer and a recent bully.

**Regression needed:** for each supported quest kind, assert that the projected
payload still explains the action, target and completion condition. Checking only
that narrative text disappeared is insufficient.

### 4. Returning rescued civilians hides a real diplomatic benefit

**Priority: medium. Confirmed by tracing the choice through native execution.**

The return choice describes which civilization receives which unit. Its original
introductory claim of gratitude is correctly removed. No factual replacement
describes the native diplomatic effects.

[`PlayerCaptureOperations.returnCivilian`](../../../vendor/Unciv/core/src/com/unciv/logic/civilization/PlayerCaptureOperations.kt)
adds 45 influence for a city-state owner, or sets the major civilization's
`ReturnedCapturedUnits` opinion modifier to 20. The latter is a set operation,
not an indefinitely stacking +20 reward. These effects are absent from the
model's return/keep choices.

The model is consequently comparing “give the unit away” against “keep it” with
part of the actual benefit missing.

**Suggested correction:** add neutral, native-derived consequence fields to the
choice, distinguishing influence from native opinion and showing the actual
change where appropriate. Do not imply that a human or agent player will feel
grateful, agree to anything, or change its strategy.

**Regression needed:** compare advertised consequences with native return
execution for city-state and major owners, including an existing opinion modifier.

### 5. `availableAttacks` counts choices, not remaining attacks

**Priority: medium. Reproduced with an existing recorded-position fixture.**

[`overview`](../../../src/civ_agent/observations.py) sets this field to the number
of native attack options for both units and cities. Unit options can include
different attack origins, as well as different targets. The preload omits the
native unit's separate `attacksRemaining` field.

The France fixture contains a Pikeman with one attack remaining, but its compact
row says `availableAttacks: 2`. This invites the interpretation that it can attack
twice. It actually has two alternatives for spending one attack.

**Suggested correction:** distinguish `attackOptionCount` from the remaining
attack allowance. Keep movement/readiness constraints separate; an unused attack
allowance alone does not mean a target is currently attackable. A distinct target
count may be more useful than counting every attack origin.

Related minor ambiguity: `greatPersonAbilities` counts all listed abilities,
including currently unavailable ones. Label it as a catalogue count or distinguish
currently usable choices.

**Regression needed:** one remaining attack with multiple targets and origins;
an exhausted unit; a city with several possible bombardment targets.

### 6. Strategic review silently drops distances to foreign cities

**Priority: medium. Reproduced with the native snapshot's city shape.**

[`review_cities`](../../../src/civ_agent/strategic_review.py) reads own-city
coordinates from `city.position`. The raw
[`TurnCity`](../../../src/main/kotlin/uncivai/player/TurnCommands.kt) stores them
under `city.development.position`. Review capture passes the raw snapshot here;
only the compact overview moves the coordinates to the top level.

Consequently, `ours` is empty and the review omits `nearestOwnCity` and
`distanceInHexes`, also losing distance-based ordering. Foreign positions remain
available, so the adviser can reconstruct distances itself, but the intended
context is absent.

The existing unit test supplies a top-level own-city position, masking the
production mismatch.

**Suggested correction:** use the native coordinate location or one shared
coordinate extractor. Test the complete native-snapshot-to-adviser path rather
than a separately invented city shape. Preserve wrap geometry and keep hex
distance distinct from travel time.

### 7. Unknown construction names look like valid but unavailable choices

**Priority: medium. Reproduced through the current selective-read projection.**

[`project_detail`](../../../src/civ_agent/observations.py) constructs a result row
for every requested name without checking whether that name exists. For an owned
city, querying `"Definitely Not A Construction"` returns a successful detail read,
`descriptionsIncluded: true`, and:

```json
{"name":"Definitely Not A Construction","canQueue":false,"purchase":null,"progress":[]}
```

The command cannot execute, but the response conflates a spelling/name error
with a real building blocked by technology, city control or another requirement.
That can send planning and error recovery in the wrong direction.

**Suggested correction:** distinguish unrecognized names from recognized but
unavailable constructions. Return a precise diagnostic and a way to discover
valid names. Keep hypothetical information for known, locked victory projects.

**Regression needed:** a misspelling, a valid locked building, an unavailable
construction in a puppet city, and a future victory project.

### 8. Delta instructions give conflicting guidance about omitted fields

**Priority: medium; contract ambiguity, not a faulty reducer.**

[`player_turn`'s description](../../../src/civ_agent/proposal_gateway.py) correctly
says that `set` replaces whole sections and collection `upsert` replaces whole
rows, then says “Omitted values stay unchanged.”

The [actual reducer](../../../src/civ_agent/state_updates.py) removes fields absent
from a replacement row. Compact unit rows intentionally omit false/empty optional
capabilities. Thus a formerly present `canGiftToCityState: true` disappears when
the replacement row omits it. Applying the final sentence recursively would
incorrectly retain that ability.

**Suggested correction:** say that sections/rows *not included in the update*
remain unchanged, while every included section/row replaces its previous value
completely. Fields missing inside a replacement disappear. One small example is
enough; the reducer need not change.

**Regression needed:** ensure generated tool instructions and a capability-removal
example agree with the existing reducer.

## Smaller ambiguities worth correcting in the same pass

These are lower-priority communication risks, not additional demonstrated
gameplay failures.

| Surface | Ambiguity | Suggested clarification |
| --- | --- | --- |
| [Upgrade resources](../../../src/main/kotlin/uncivai/player/UnitEconomyObservation.kt) | `resourceRequirements` means additional consumption after releasing the old unit's requirements. Construction uses the same name for the new item's requirements. Only a source comment explains the upgrade distinction. | Label upgrade requirements as additional/net requirements, or expose both before/after totals. |
| [Economy resource details](../../../src/main/kotlin/uncivai/player/EconomyObservation.kt) | `unimprovedTiles` includes own and allied city-state sources, while `development.unimprovedResources` lists owned tiles. The distinction is in source comments, not the returned field. The aggregate count is not currently in the compact preload. | Label the aggregate scope or split own/allied counts without exposing hidden allied tile locations. |
| [Culture victory objectives](../../../src/main/kotlin/uncivai/player/VictoryProgress.kt) | All policy branches appear as `completed: false/true` objectives, although the milestone requires a specified number of branches. The correct required count remains in the text, so this is structural ambiguity rather than an incorrect native rule. | Represent “complete N of these candidates” explicitly instead of an unqualified checklist. |
| [Turn-history prose](../../../src/main/kotlin/uncivai/player/TurnReview.kt) | “Gained Instant Heal” resembles a retained promotion; “started Plantation” can mean the worker actually started clearing. Detailed current-state fields are more precise. | Say “selected Instant Heal; healed X” and “started clearing; Plantation queued,” using actual effects. Align with finding 2's factual history. |
| [Map tool description](../../../src/civ_agent/proposal_gateway.py) | “Hidden tiles have no data” is imprecise: explored tiles under fog can contain remembered facts. | Distinguish unexplored tiles from explored tiles outside current sight; retain observation dates and visibility. |

## Distinctions that are already handled well

- Major-player demands available to send are separate from incoming messages and
  recorded promises. Native response choices retain war consequences. Trade-menu
  inventory is now distinct from a proposal or consent.
- Social promises remain attributed statements with conditions; advisories do
  not create native gameplay restrictions or adjudicate a breach.
- Settlement site eligibility is separate from current founding readiness.
  Route, escort and military-proximity summaries explain that distance does not
  establish travel time, enemy reachability or safety. Current versus remembered
  barbarian observations are distinguished.
- Construction estimates distinguish ongoing city yields, while-building effects,
  maintenance, unstaffed specialist slots, excluded completion grants, fixed
  population and hypothetical build times. Wonder estimates remain under
  `wonderOpportunities`.
- Research templates clearly distinguish replacing the queue from appending to
  it. Water resources identify Work Boats and technology requirements.
- Victory statistics explain metric meanings, disclosed-rival ranking scope,
  recorded versus current data, and why native military rating includes treasury.
- Memory records distinguish observations, beliefs, notes, handoffs and strategy.
  Reports are dated and advisory. The independent adviser excludes old strategic
  rationales; the problem in finding 2 is loss of factual outcomes, not the need
  to restore those rationales.
- Applied actions with refresh failures explicitly require a read, not repeating
  the action. Uncertain outcomes and nonrecoverable errors stop work.

No new concrete defect was established in these distinctions during this audit.
That is not a guarantee against future cases.

## Original audit validation

- Six disposable Python diagnostic cases confirmed the attack count, missing city
  distances, quest redaction, indistinguishable adviser attack history, unknown
  construction response, and actual row-replacement behavior.
- A disposable native probe created a fresh duel, previewed a Plantation on
  forested Spices, executed the full worker queue, and compared actual yields.
  It ran through `codex-log ./gradlew` with an init script adding only a temporary
  test source directory. It did not load or modify an existing match.
- The existing observation, strategic-review, model-view and state-update suites
  passed: **74 tests**, through `codex-log .venv/bin/pytest`. These passing tests
  coexist with the findings; the proposed regressions must exercise the missing
  cross-layer cases.
- Temporary diagnostics are in `/tmp/unciv-communication-audit/`. Native output:
  `/tmp/codex-output.ZBYzoa`; pytest output: `/tmp/codex-output.oNmcg6`.

The corrections supply precise data at the point of use rather than additional
strategic prompt instructions. Implementation validation is recorded above; the
disposable diagnostics in this section document the original reproductions.
