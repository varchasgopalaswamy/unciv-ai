**Headless player agents without completing the View refactor**

Proposed 2026-09-06, based on source inspection at `630e1938c`. This is an alternative deployment and implementation strategy to the [original implementation plan](Player-agent-implementation-plan.md), using the same [action and information inventory](Player-agent-API-review.md). S01 load/shutdown, the S02 Kotlin own-summary prototype, S03 validated city renaming, and S04 free-great-person selection are complete. The S04 engine contribution is published to the maintained fork and recorded as the dependency; S05 and later stages remain pending.

The required outcome remains **every ordinary player action and every ordinary player information surface** for the bundled Vanilla and Gods & Kings rulesets, including nation-specific abilities. The interface is structured and headless. Screenshots, screen automation, omitted gameplay systems, and automatic AI selection of otherwise unsupported human choices do not satisfy this outcome. Ordinary player-selectable automation remains available.

The main saving is that the trusted adapter can own access control, detached observations, command validation and orchestration. Completing the existing View hierarchy, migrating all UI callers, attaching to a running desktop screen, and upstreaming the entire player API are not prerequisites. The full gameplay coverage work remains necessary, wherever it lives.

**Recommended boundary**

```text
Isolated agent A ── player-bound MCP connection ──┐
Isolated agent B ── player-bound MCP connection ──┼── Trusted match host
Isolated agent C ── player-bound MCP connection ──┘     ├── Player API adapter
                                                      │   ├── explicit output projections
                                                      │   ├── validated command handlers
                                                      │   └── per-player observation/decision state
                                                      └── Unciv engine and full GameInfo
```

Use one JVM process per match initially because Unciv uses globals such as `UncivGame.Current` and `Gdx`. Multiple agents connect to that one game; they do not each receive an engine instance or a full save. The MCP transport can live in that JVM or in a separate trusted frontend. An unrestricted engine connection between those trusted components must never become an agent tool.

The host is trusted with hidden information, just as the engine is. Agent processes receive only permitted serialized results and their own credentials; they cannot read host saves, logs, memory, sidecar state, administrative endpoints or another player's session. Do not expose arbitrary Kotlin execution, reflection, raw-object lookup, arbitrary unique execution or generic property access. Keeping hidden information out of the prompt is insufficient if the agent can retrieve it through another tool.

MCP supplies the tool protocol and supports structured results; the implementation supplies game-specific access control. See the official [MCP tool specification](https://modelcontextprotocol.io/specification/2025-11-25/server/tools). None of this depends on an Unciv class being named `View`.

**What already exists**

| Existing code | What can be reused | Remaining limitation |
| --- | --- | --- |
| [ConsoleLauncher](../vendor/Unciv/desktop/src/com/unciv/app/desktop/ConsoleLauncher.kt) | Initializes `UncivGame(true)` and runs simulations without a rendering application. | Its experiment setup is hardcoded; it is not a general player server or proof that every Human action works headlessly. |
| [DesktopLauncher](../vendor/Unciv/desktop/src/com/unciv/app/desktop/DesktopLauncher.kt) `--creategame` branch; [GameStarter](../vendor/Unciv/core/src/com/unciv/logic/GameStarter.kt) | Existing console bootstrap and ordinary game construction. | The host needs configurable creation/loading, assets/settings initialization, first-turn setup, persistence and error handling. |
| [GameInfo.nextTurn](../vendor/Unciv/core/src/com/unciv/logic/GameInfo.kt) | Processes ordinary AI/barbarian turns and stops at the next Human player. | It does not perform all UI choice/readiness checks. Simulation flags must remain off for externally controlled players. |
| [UncivFiles](../vendor/Unciv/core/src/com/unciv/logic/files/UncivFiles.kt) | Game serialization, compatibility checks and transient restoration. | Host checkpoints must also preserve adapter-owned observations and decisions consistently. |
| Gameplay managers, [Battle](../vendor/Unciv/core/src/com/unciv/logic/battle/Battle.kt), [TradeLogic](../vendor/Unciv/core/src/com/unciv/logic/trade/TradeLogic.kt) | Most gameplay algorithms and many mutation primitives. | Low-level methods do not universally enforce everything the human caller checks. |
| [UnitActions](../vendor/Unciv/core/src/com/unciv/ui/screens/worldscreen/unit/actions/UnitActions.kt) | Existing action eligibility and execution logic. | The source explicitly notes that some factories require WorldScreen; invoking a closure is not a general headless player API. |

Keep externally controlled slots as `PlayerType.Human`; do not introduce an Agent enum solely to stop built-in automation. Keep `WorldScreen` absent. A standalone host can serialize execution and advance its own authoritative `GameInfo` without first extracting the desktop clone/upload/reload coordinator. Tests with mocked graphics demonstrate test support, not a need to create invisible screens in production.

**Minimum engine-facing work**

These are categories of necessary behavior, not a claim that five PRs can deliver complete parity. Some can be implemented entirely in the downstream host. Upstream only the small reusable pieces that cannot be cleanly obtained from existing public engine operations.

1. **A supported console runtime entry point.** Build on the existing console path. Initialize ordinary settings, bundled rules, file services and a new or loaded game without textures, input, screens or audio. Provide deterministic lifecycle ownership and non-UI error reporting. Reuse serialization and transient setup. The launcher/server can initially live downstream; extract shared bootstrap only where duplication or a real runtime dependency requires it.

2. **Callable gameplay operations for logic currently embedded in UI callbacks.** Reuse an existing engine method when it already performs the complete operation. Otherwise move the smallest relevant eligibility/options/commit operation into a graphics-independent helper and let the existing UI call it. For example, [GreatPersonPickerScreen.confirmAction](../vendor/Unciv/core/src/com/unciv/ui/screens/pickerscreens/GreatPersonPickerScreen.kt) grants the unit, decrements free picks and updates the Maya pool; [AlertPopup.addRecapturedCivilian](../vendor/Unciv/core/src/com/unciv/ui/screens/worldscreen/AlertPopup.kt) contains the return/keep effects and diplomatic rewards. Neither choice can be dropped. [UnitActionsFromUniques](../vendor/Unciv/core/src/com/unciv/ui/screens/worldscreen/unit/actions/UnitActionsFromUniques.kt) can open a promise-breaking confirmation during ordinary city founding; expose that confirmation as data. Do not switch Human slots to AI to bypass these paths.

3. **A headless choice and turn-readiness contract.** Combine the relevant checks in [NextTurnAction](../vendor/Unciv/core/src/com/unciv/ui/screens/worldscreen/status/NextTurnAction.kt) with pending interactions currently opened by [WorldScreen.update](../vendor/Unciv/core/src/com/unciv/ui/screens/worldscreen/WorldScreen.kt). Include research/production, policies and their permitted deferral, free technology/great-person choices, all religious choices, city conquest/disposition, civilian return, diplomatic requests/promises, trade responses, votes and game-end choices. Not every reminder is mandatory, and not every popup blocks all other actions: preserve the human flow's actual constraints. Options and resolvers can be ordinary functions; an elaborate new decision framework is unnecessary. The adapter can assign session/epoch-scoped decision IDs, validate them against live state, and persist any state not reconstructible from the game.

4. **Observation hooks where the final state loses human-observable facts.** Current state cannot recover a unit's original owner after capture, an attack origin after movement/destruction, or what was visible before an effect removed sight. Hook the relevant movement, visibility and combat boundaries, including internal automated actions. Record only facts and disclosure moments available to a human; engine-only intermediate states must not become player observations. A callback only after an entire end-turn call is insufficient. Existing notification/history records can be reused where they retain the required information. The adapter may store the resulting per-player knowledge outside `GameInfo`, avoiding a mandatory new save schema.

5. **Pure information calculations where a screen is the only implementation.** Reuse existing calculations with explicit player context. Extract small helpers for remaining choices, forecasts, tooltips, breakdowns, overviews and disclosure rules that currently require constructing a screen. Alternatively, an adapter can initially port a simple screen calculation with parity tests and a recorded source reference. The entire View hierarchy need not be hardened first, but an unsafe existing getter is not automatically a permitted export.

The current `AttackRecorder` branch supplies unit-recording infrastructure; source references show it is not yet wired into ordinary combat execution. The larger combat-history branch is useful reference material. Full record integration for the required combat types remains work; centralizing every notification or landing the larger branch's View/UI changes is not a prerequisite for a trusted observer hook. Any disclosure bug used by both human and agent consumers still needs a focused correction.

**What stays in the adapter**

The adapter owns session-to-player binding, serialized game execution, ownership/phase/argument checks, safe entity handles, operation receipts, permitted projections, observation memory, decision handles and MCP schemas. S03 uses the runtime's shared lock for execution; a dedicated queue is not required for the in-process prototype. No complete game object or live View crosses the agent boundary. A projection can be a plain function producing data records; no inheritance hierarchy is required.

Every mutation resolves its entities against the authoritative game when it runs. Validate both player authority and the complete operation's domain rules. A trade proposal cannot supply another agent's consent. A city obtained through spy inspection grants reads, not city management. Costs, choices and execution preconditions must be rechecked; a previously returned action option is not lasting authority.

Start with one writer and sequential game turns. Reads use a committed per-player snapshot, or wait until the current mutation completes. Use request IDs and cached receipts to prevent retries from repeating purchases or end-turn. Bind handles and receipts to a game epoch; reject stale work after reload. On an unexpected mutation exception, halt further commands for that game and report a sanitized failure while retaining diagnostics for the host. Do not claim rollback or safe automatic retry of partially executed gameplay.

Persist a checkpoint bundle containing the engine save and adapter observation/decision state from the same committed point. Loading a bare older save cannot invent missing past knowledge; provide only facts actually recoverable from its persisted data. Reload policy and epoch changes must prevent mixing two histories. Regular player undo, if enabled by the match rules, must preserve already learned information and obey the same restrictions for humans and agents; it cannot silently be omitted in a parity claim. Save export/reload, debug/spectator administration and filesystem access belong to the match host, not an ordinary participant.

Privacy requires more than removing unexplored tiles. Audit foreign unit fields and invisibility, resource reveal technologies, remembered terrain/improvements/ownership, trade disclosures, spy inspections, notifications and links, rankings/victory settings, action options, previews and errors. Preserve intentionally public aggregates and diplomatic disclosures. Unknown is different from empty. A guessed unavailable handle must not reveal whether a hidden object exists. Player cursors and list counts must not encode hidden events or inventories.

Do not expose an unrestricted `canMove`, pathfinder, battle simulation, AI evaluator or dry-run clone as a planning tool. Planning must use the human's permitted knowledge; real execution may use the engine's full state and report only the discoveries made by the attempted action. Merely inspecting an option must not consume randomness or reveal future combat rolls.

**Complete coverage through a compact interface**

Few MCP tools can dispatch many typed operations. For example:

```text
describe(domain)                         -> static capability/schema/rules reference
observe(sections, region?, cursor?)       -> permitted summaries, details or deltas
options(subject, operationFamily?)       -> permitted choices, costs and confirmations
preview(command)                         -> the corresponding human planning information
act(requestId, observationToken, command)-> receipt, observations and pending choices
resolve(requestId, decisionId, choice)   -> receipt and resulting observations
wait(cursor)                             -> permitted update / own-turn readiness
```

`command` is an explicitly supported tagged data type, not arbitrary code or a model-selected method name. End-turn is a validated command. Static capability discovery must be distinct from current eligibility. Action and decision responses preserve all parameters a human can choose; they cannot reduce a multi-option action to the first enabled closure.

| Required domain | Operations and information that must be mapped |
| --- | --- |
| Map and units | Every movement/order, selection-equivalent inspection, embarkation, swaps/escorts, automation/cancel, transport/rebase/paradrop/port changes, upgrades, promotions, disband/gifts; own details and permitted foreign sightings. |
| Civilian and special units | Founding, improvements/roads/repair/pillage, great-person actions, religious actions and all bundled nation-specific actions and parameters. |
| Combat | Ground/naval attacks, city bombardment, siege setup, air strikes/sweeps/interception outcomes and nuclear use; human-visible targeting, previews and reports. |
| Cities and economy | Production queues and reorder/cancel, purchases, workers/specialists/locks/focus, tile purchases, buildings/sales, naming, raze/annex/puppet/liberate; all city, empire and yield breakdowns. |
| Progression and religion | Research queues/free picks, policies/branches/deferral, great-person choices including Maya restrictions, pantheon/founding/enhancement/beliefs/name/icon, faith purchases and public religion reports. |
| Diplomacy and trade | Contact, war/peace, treaties/promises/denouncements, proposals/counteroffers/accept/decline/withdrawal, gifts, city transfers, incoming decisions and permitted offer information. |
| City states and espionage | Influence/protection/tribute/gifts/improvement gifts, quests, marriage and other nation-specific interactions; spies/destinations/actions/coups and the precise permitted inspection information. |
| Overviews and lifecycle | Civilopedia/rules, notifications/history and permitted links, resources, wonders, global politics, demographics/charts, victory progress, votes/results, permitted resignation/continuation and undo. |

Use the original inventory as the completeness ledger; this table groups implementation work and is not a replacement for its individual entries. Every normal human control/information surface needs a mapping and acceptance evidence. A few agents completing one easy match does not establish full parity, and no row can be marked complete by falling back to UI automation or AI choice.

Keep responses small by default: changes, urgent decisions and an empire summary, with complete details available on request. Cache bundled rules and static schemas in the client. Paginate the permitted collection, support bounded regions and sections, and compute deltas after filtering for that player. These are presentation choices; no player information is discarded solely to save tokens. Any later command batching must stop at new discoveries or choices where a human could reconsider.

**Atomic development and upstreaming**

Maintain a downstream host against a pinned Unciv revision and a focused source patch stack. No upstream merge needs to precede local integration. Reuse the existing feature inventory, but remove dependencies that existed only to migrate unrelated UI/View consumers or attach to a desktop WorldScreen.

Start with the trusted host and its player facade, exercising the facade directly from a scripted client before adding MCP transport. Develop the host in a separate repository depending on the maintained Unciv fork's `core` module. The [current build](../vendor/Unciv/build.gradle.kts) already separates core and desktop dependencies. Keeping a host module in the Unciv checkout is also possible, but sharing a repository is not required.

| Repository | Responsibility |
| --- | --- |
| Maintained Unciv fork | Existing game engine and bundled rules, plus focused headless operation extractions, observation hooks and any necessary library/bootstrap support. Keep these changes suitable for individual upstream PRs. |
| Agent host | Headless executable, player authorization, explicit information projections, command handlers, decision tracking, execution queue, checkpoint sidecars, MCP transport and integration/privacy/coverage tests. |

The repository now pins the maintained fork with the `vendor/Unciv` submodule and supports an explicit local checkout override. See the [development workflow](Development-workflow.md) and private [contribution instructions](../AGENTS.md). The build uses a [Gradle composite build](https://docs.gradle.org/current/userguide/composite_builds.html) with explicit dependency substitution to its `:core` project. This supports a source dependency before publishing a library. Gradle's included build does not fetch or pin Git revisions; the checkout mechanism must do that. The current core build does not configure Maven publication. Later, publishing a versioned core artifact with dependency metadata and matching runtime assets can replace the source checkout without changing the player API.

Keep code and bundled assets on the same fork revision. [Ruleset loading](../vendor/Unciv/core/src/com/unciv/models/ruleset/RulesetCache.kt) uses filesystem paths under `jsons/`; the current core JAR does not package the assets from `android/assets`. The implemented [S01 runtime](Headless-runtime.md) resolves those assets through a file service bound to the selected checkout and disables mod loading. Consume core with its declared runtime dependencies and declare libraries the host directly uses; avoid a dependency on the desktop launcher or production use of test fixtures. The setup includes both the initial ruleset-loading smoke test and separate-process load/shutdown scenarios for two Human players in each bundled ruleset.

A separate Kotlin module can use public core APIs; needed `internal` or UI-bound operations still require narrow callable helpers in the fork. Repository separation does not remove that engine work, the full human action/information coverage requirement, or the host's privacy obligations. The trusted host may hold raw engine objects, while agents receive only its explicitly projected protocol data.

For linked work, make one engine PR for a reusable operation or hook and a corresponding private host commit for its handler and tests. Host PRs are optional for the sole maintainer. The host commit pins the fork commit containing that engine change, and integration CI verifies that exact pair. Public engine contribution text must stand alone and must not disclose the private repository or its purpose. Update the pin deliberately when integrating upstream changes and rerun the affected parity and privacy scenarios. Upstream acceptance can proceed independently of local host development.

The first batch has these reviewable boundaries:

| Step | Deliverable | Depends on | Acceptance evidence |
| --- | --- | --- | --- |
| [x] **S01** | Standalone host repository/build with a pinned fork dependency and matching runtime assets, plus headless bootstrap and load/shutdown path. Use a saved scenario with two Human players; initialize ordinary assets/settings/transients without starting the desktop application. | — | Implemented and locally verified 2026-09-06 against the pin: both bundled scenarios load and exit without display/GL or changed player state. Failure recovery and asset/data isolation pass. No engine extraction was required. See [runtime verification](Headless-runtime.md) and [handoff](Session-handoff.md). |
| [x] **S02** | In-process player facade with host-assigned player contexts and detached own-player summaries. | S01 | Two scripted clients receive their respective summaries in both rulesets. Queries have no viewer argument; copied scalar results and serialized output pass contract checks. Unrelated hidden-state changes preserve output, and close/reload expires sessions. This exposes only the implemented query capability; see the [prototype contract](Player-summary-prototype.md). |
| [x] **S03** | Serialized command path demonstrated by renaming an owned city, including ownership/turn checks, request receipts and stale-epoch rejection. | S02 | Both rulesets pass actual engine-state checks for successful renames, rejected commands, ownership loss and close/reload. A/B/retry and concurrent duplicates preserve historical receipts without repeating effects. Unexpected mutation failures halt player operations. See the [city-rename contract](City-rename-prototype.md). No end-turn operation is exposed. |
| [x] **S04** | Extract free-great-person selection, including Maya restrictions, into a callable validated operation; make the human picker delegate. | — | Published on `experimental-changes` and recorded at `df4f61112`. Eight engine tests cover both rulesets, equivalents, Maya restrictions, invalid/stale selections, placement failure, automation and the mandatory turn-button action after deferring the picker. The helper retains choices on failed placement. Manual UI fixtures and comparison commands are in the [selection guide](Free-great-person-selection.md). |
| [ ] **S05** | Connect that choice to headless option/decision discovery and resolution. | S03, S04 | A scripted client chooses among all permitted options; stale, repeated and invalid resolutions cannot consume extra picks. No AI fallback or popup construction. |
| [ ] **S06** | Thin MCP adapter over the tested facade and handlers. | S05 | Two independently bound connections exercise the same observations, command and choice through MCP. Transport exposes no extra engine access. |

This batch proves the runtime, privacy boundary, mutation path and UI-extraction pattern. It is not a playable-subset substitute for the required complete game. After it, proceed through the original coverage inventory, pairing each additional headless command or query with only the engine extraction/hooks it actually needs. Add player end-turn only with readiness checks that reject unresolved unsupported blockers. Keep runtime-only turn smoke tests separate from a player-authorized end-turn endpoint.

| Work item | Atomic boundary and linked work |
| --- | --- |
| Runtime bootstrap | One PR for reusable console initialization and ordinary game creation/loading, with a no-display smoke test. Host launch/configuration can be a linked downstream PR. |
| Execution queue | One downstream PR for player-bound requests, single-writer ordering, stale epochs and receipts, demonstrated with one existing operation. It does not claim all command coverage. |
| Choice contract | One PR for minimal choice/readiness data with one existing choice; complete each other choice as a separate linked extraction/adapter PR. Do not merge a premature end-turn bypass. |
| Example choice extraction | A free-great-person/Maya choice is one cohesive PR; civilian return is another; conquered-city disposition can be another. Each includes legality and effects, its UI delegation where extracted, and headless parity tests. |
| Unit operation extraction | Split by cohesive operation: e.g. founding including promise confirmation, promotion, upgrade, or improvement selection. Pair each with its adapter options/command and permitted result; do not make all unit actions one PR. |
| Observation instrumentation | Split normal combat, city combat, air interception/sweep, nuclear effects and movement/visibility when they touch distinct mechanics. Link each hook with its downstream projection and history test. No requirement to move unrelated notification formatting. |
| Information projection | One own-summary, city detail, trade offer, spy inspection or overview slice per PR, split further when its disclosure policy differs. Link a pure engine helper extraction only if that slice actually requires one. |
| Checkpoint bundle | One downstream PR for saving/restoring matching engine and player-knowledge state, with restart/epoch tests. |
| MCP transport | One downstream PR exposing completed handlers with player-bound connections. Capability support is explicit throughout development. |
| Completion gate | A coverage audit plus full headless domain scenarios after every inventory entry is implemented; this PR contains no missing gameplay implementations. |

This is a slicing pattern, not a ten-PR estimate. The individual domain handlers still take work. The upstream contribution is concentrated on reusable engine operations and observation hooks, while the network interface and most privacy projections can evolve independently downstream. Broad View hardening and shared human/API presentation can follow later.

Validation for each slice must exercise the real headless command, its allowed information, invalid authority/arguments, and any resulting choices. Use paired game states differing only in hidden facts to check equivalent permitted query/preview/error outputs; repeat after contact, detection, capture, spy loss and reload. Test brief visibility and effects inside end-turn, not just command endpoints. Compare gameplay effects with the existing human handler and inspect every remaining inventory entry. Add representative multi-agent matches through victory, but retain targeted scenarios for rare built-in choices and advanced units.
