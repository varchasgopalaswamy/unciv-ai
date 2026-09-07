# Session handoff

Saved 2026-09-06 after implementing S05, free-great-person discovery and selection through the Kotlin player session. This is a private continuity note, not material for the Unciv fork or its public contributions.

## Read first

- [AGENTS.md](../AGENTS.md) contains the confidentiality rule and contribution instructions.
- [Development workflow](Development-workflow.md) describes the submodule and local checkout override.
- [Headless runtime](Headless-runtime.md) documents the executable, scenario generation and lifecycle ownership.
- [Player-summary prototype](Player-summary-prototype.md) specifies the Kotlin session interface, permitted fields and validation scope.
- [City-rename prototype](City-rename-prototype.md) specifies owned-city handles, validation, serialization, receipts and mutation failure handling.
- [Free great-person selection](Free-great-person-selection.md) documents the engine helper, UI recovery and before/after comparison saves and commands.
- [Free-great-person prototype](Free-great-person-prototype.md) specifies player-bound discovery, expiring choice handles, selection validation and retry receipts.
- [Headless implementation path](Headless-player-agent-path.md) is the current approach and initial sequence, S01–S06.
- [Player API review](Player-agent-API-review.md) is the completeness inventory. The [original implementation plan](Player-agent-implementation-plan.md) remains a broader backlog; its desktop attachment and extensive View migration prerequisites are superseded for this headless route.

## User decisions

- This host repository remains private and has one maintainer. Ordinary private branches and commits are sufficient; host PRs are optional.
- Adding third-party libraries is already authorized. Prefer suitable existing libraries over reinventing functionality that is even moderately complex; this preference also appears in `AGENTS.md`.
- Never reveal the private repository's existence, name, or purpose in Unciv commit messages, including commits in the submodule or a sibling checkout. Public PR text and branch names must also contain only accurate engine behavior and validation, without private context. Keep these instructions and plans here.
- The required outcome is several agents controlling different players in one game, with every ordinary human player action and every permitted human information surface. Cover both bundled Vanilla and Gods & Kings rulesets and nation-specific abilities. Ignore mod content.
- Use structured, headless control. UI automation, screenshots, and delegating unimplemented human choices to the game's AI do not meet the requirement. Ordinary automation a human can choose remains in scope.
- S02 is a small working prototype of the observation surface. For now the runtime, facade and scripted clients remain Kotlin code in one JVM; the full action/information inventory remains required. External clients in other languages come through the later MCP transport.
- Keep public engine work small and independently reviewable. The private host can advance against the maintained fork without waiting for upstream acceptance.
- The S04 PR reproduction is supplied as a standalone JSON save, as requested: `build/pr-artifacts/free-great-person-blocked-placement.json`. Do not package that save in a ZIP when providing it to the user.

## Current repository state

This repository remains on `main`, tracking `origin/main`. The user committed S01 as `0980746`, S02 as `dcae4ce`, and S03 as `ddeea14`; S04 host integration is `02c6f8f`. The S04 engine contribution is committed as `df4f61112b89f3e6b1bd9efc299175b48261ea5c` and published on the fork's `experimental-changes` and `free-great-person-selection` branches; both remote refs were verified directly. The clean submodule is currently on `experimental-changes`. The host records that revision together with the S04 UI tooling and documentation. S05 uses the same pin and changes only this private repository. Inspect both repositories before further editing or staging.

The submodule is `vendor/Unciv`, with URL `https://github.com/varchasgopalaswamy/Unciv.git` and integration branch `experimental-changes`. Its recorded commit is:

```text
df4f61112b89f3e6b1bd9efc299175b48261ea5c
```

The sibling `/home/vgopalas/projects/Unciv` is a separate development checkout. At the original setup handoff it was on `combat-recording-unit-infrastructure`, at `630e1938ca54ec4835620594b5f7cb84971ce75e`, with an existing untracked `.codex/` directory. It was not used or changed for S01. Use its current branch deliberately; it is a different revision from the pin. Earlier combat-history discussions concerned other feature branches, so verify APIs against the selected checkout.

S04 was developed on `free-great-person-selection` in `vendor/Unciv` and is now integrated into the fork's `experimental-changes` branch. The private UI comparison script continues to use the original revision `d77a312ed6ba0d127fe4efdfbcdc9f4918c0b5fa` in `build/s04-ui/before-engine` for the before case, avoiding checkout switches or stashing edits. Copies of each selected engine's assets and isolated desktop data live under `build/s04-ui/before` and `build/s04-ui/after`. These generated directories are ignored by Git.

## Implemented and verified

- Gradle composite build consuming the selected Unciv checkout's `:core` project, with dependency substitution for `local.unciv:core`.
- Default checkout `vendor/Unciv`; explicit local override `-PuncivDir=../Unciv`. Relative paths resolve from the host repository root.
- Engine code, dependency catalog, and runtime assets use the same selected checkout. Test and Java execution tasks use its `android/assets` directory.
- Gradle wrapper copied from the pinned engine: Gradle 9.4.1. The selected engine currently supplies Kotlin 2.4.10. Host compilation targets Java 21.
- `engineInfo` prints the selected engine path, SHA, and assets path.
- [HeadlessRuntime.kt](../src/main/kotlin/uncivai/HeadlessRuntime.kt) loads an existing save through public core operations and implements `AutoCloseable`. It owns one active game, fresh in-memory settings with automatic choices disabled, bundled-only ruleset loading, explicit asset/data roots and worker-pool shutdown. It initializes no GDX application, graphics, world screen or desktop services. No engine extraction was necessary.
- [Main.kt](../src/main/kotlin/uncivai/Main.kt) supplies the executable: `./gradlew run --args="--save /absolute/save.json --data-dir /absolute/runtime-directory"`. Save and data paths must be outside source assets. The host never reads or writes desktop settings and does not rewrite the input save.
- Gradle supplies `uncivai.assetsDir` from the selected checkout. The host's file service resolves assets independently of the working directory. Generated application launch scripts reference that checkout's assets; they are development launchers rather than self-contained asset bundles.
- `./gradlew generateS01Scenarios` uses [S01ScenarioGenerator.kt](../src/test/kotlin/uncivai/S01ScenarioGenerator.kt) to write `build/generated/s01-scenarios/vanilla.json` and `gods-and-kings.json`. Each contains Human Rome and Greece, cities with open production choices, units, and free policy, technology and great-person choices. The generator is test tooling and is not a production dependency.
- [HeadlessRuntimeIntegrationTest.kt](../src/test/kotlin/uncivai/HeadlessRuntimeIntegrationTest.kt) launches separate JVMs without display environment variables. Semantic digests cover turns, player identity, units, city production and pending choices. Tests exercise natural exit, JSON and compressed saves, load failure and recovery, fresh settings, alternate working directories, real/symlink asset separation, unchanged saves and source-asset fingerprints.
- [PlayerHost.kt](../src/main/kotlin/uncivai/player/PlayerHost.kt) binds a `PlayerSession` to a host-selected Human major civilization. `summary()` accepts no viewer parameter and rechecks membership on each read. Read-only self access remains available out of turn and after defeat. Closed/replaced runtimes invalidate their sessions; loading another game cannot retarget them.
- [PlayerSummary.kt](../src/main/kotlin/uncivai/player/PlayerSummary.kt) defines eleven copied scalar fields: own civilization, ruleset, turn, `isYourTurn`, treasury, city/unit counts, nullable current research, and free policy/technology/great-person counts. `PlayerSummaryJson` uses generated kotlinx.serialization JSON; the host explicitly depends on version 1.8.1, matching the selected engine's resolved dependency, with the serialization compiler plugin matching Kotlin 2.4.10.
- [PlayerSessionException.kt](../src/main/kotlin/uncivai/player/PlayerSessionException.kt) defines fixed player-facing errors without engine causes. The query surface exposes neither raw engine objects nor mutable collections. It is an in-process API contract, not a sandbox for arbitrary Kotlin code sharing the JVM.
- [S02PlayerFacadeIntegrationTest.kt](../src/test/kotlin/uncivai/S02PlayerFacadeIntegrationTest.kt) and [S02PlayerFacadeProbe.kt](../src/test/kotlin/uncivai/S02PlayerFacadeProbe.kt) exercise two bound scripted clients on both bundled rulesets, paired unmet-opponent changes, unchanged raw serialized engine state during reads, detached results, authority checks, errors, defeat and close/reload. [PlayerSummaryJsonTest.kt](../src/test/kotlin/uncivai/PlayerSummaryJsonTest.kt) checks JSON and public Kotlin type shape, including explicit nulls and scalar-only backing fields.
- S03 extends `PlayerSession` with `cities()` and `renameCity(request)`. [CityCommands.kt](../src/main/kotlin/uncivai/player/CityCommands.kt) defines detached own-city snapshots, rename requests and receipts with generated JSON. Opaque handles are session-scoped; each runtime load receives a fresh random epoch. All player operations and shutdown share the runtime monitor.
- New rename requests revalidate the Human major-civilization binding, current player, live city ownership and arguments. Names follow the engine rename entry rules: 1–32 UTF-16 code units without `[]{}"\<>` or CR/LF, with no trimming or uniqueness check. Puppet/resistance status and free picks do not block that rename path. The only engine effect is assigning `City.name`.
- Successful request IDs are remembered for the session. Exact repeats return historical receipts even after another rename, a turn change or ownership loss; revoked player bindings still fail. IDs are 1–128 printable ASCII characters, and rejected requests do not reserve them. Reusing a successful ID with a different payload fails. S05 shares the successful-ID namespace across renames and great-person selections, with cross-command conflicts rejected. Receipts are not persisted or shared across sessions.
- Unexpected exceptions after mutation begins halt all further player operations with sanitized `GAME_HALTED`; `HeadlessRuntime.mutationFailure` retains the trusted diagnostic cause. The operation may already have changed the game. Closing remains possible; rollback is not implemented.
- [S03CityRenameIntegrationTest.kt](../src/test/kotlin/uncivai/S03CityRenameIntegrationTest.kt) runs [S03CityRenameProbe.kt](../src/test/kotlin/uncivai/S03CityRenameProbe.kt) against both saved rulesets. Checks inspect actual `City.name` and raw serialized `GameInfo`, covering authority, argument rules, hidden names, detached observations, ownership loss, reload, A/B/retry, concurrent duplicates/conflicts, serialized reads/close and failure after a write. [PlayerCommandJsonTest.kt](../src/test/kotlin/uncivai/PlayerCommandJsonTest.kt) checks DTO and JSON whitelists.
- S04 adds `GreatPersonManager.getFreeGreatPersonOptions()` and `chooseFreeGreatPerson(unitName)`. Options reuse engine equivalents/settings filtering and current Maya restrictions. Selection returns the placed `MapUnit`, consuming the entitlement and relevant Maya pool entry only on successful placement. Invalid, exhausted, cityless or failed-placement choices return null without consuming the pick. Existing placement can allocate an internal unit ID on failure; there is no general rollback guarantee.
- The human picker and automated choice delegate to the new operation. Failed/empty UI choices allow returning to the map without immediate popup reopening, while the turn button still requires the unresolved choice. Success clears that screen-local deferral. S05 exposes the engine operation through the host; no player end-turn API has been exposed.
- [GreatPersonSelectionTest.kt](../vendor/Unciv/tests/src/com/unciv/logic/civilization/GreatPersonSelectionTest.kt) contains eight independent engine tests covering real placed units, Vanilla/G&K and Mongolia equivalents, Maya pool staleness, invalid/exhausted/cityless choices, blocked placement, automated success/failure, option detachment/settings, and the mandatory turn-button action while deferred.
- S05 adds `PlayerSession.freeGreatPersonChoice()` and `chooseFreeGreatPerson(request)`. [FreeGreatPersonCommands.kt](../src/main/kotlin/uncivai/player/FreeGreatPersonCommands.kt) contains detached, serializable snapshots, decisions, requests and receipts. No entitlement is an explicit null decision; a positive entitlement with no current options remains a pending decision. Options are sorted, unmodifiable engine-equivalent names with current settings and Maya restrictions.
- Free-choice identity is coordinated per civilization by a runtime-owned tracker, shared across sessions and separate `PlayerHost` instances. Each session issues its own opaque handle. Observed changes to the free/Maya counters, options, turn or coherent own-turn flag replace the identity, and every successful selection explicitly invalidates it. Hidden foreign changes do not supply a global revision. Another selection requires rediscovery; exact successful request replay returns the original receipt. Arbitrary trusted engine writes that change and restore state between observations are not tracked.
- New selections check epoch, request-ID syntax, Human major binding, both current-player ID/object, live session decision and offered name inside the runtime monitor. Successful IDs share a typed ledger with renames; cross-command reuse conflicts. Placement failure returns `PLACEMENT_FAILED` without reserving the ID or consuming the choice; the same request can be tried after space is available. Any unexpected exception once the engine chooser is entered halts the runtime, including a failure after placement or during receipt bookkeeping.
- [S05FreeGreatPersonIntegrationTest.kt](../src/test/kotlin/uncivai/S05FreeGreatPersonIntegrationTest.kt) reuses the S04 saves and runs [S05FreeGreatPersonProbe.kt](../src/test/kotlin/uncivai/S05FreeGreatPersonProbe.kt) in a separate JVM without display variables. It selects every offered ordinary option in both bundled rulesets, verifies actual unit name/owner/map placement and counters, and covers Maya-to-ordinary transitions, blocked placement and retry, empty choices, invalid/stale/foreign requests, shared command IDs, concurrent duplicates and competing sessions with two picks, hidden foreign-state independence, reload/revocation and failure after placement. [PlayerDecisionJsonTest.kt](../src/test/kotlin/uncivai/PlayerDecisionJsonTest.kt) checks three DTO/JSON contracts, and the existing session method whitelist now covers the five supported methods.
- At the user's request, [S04UiScenarioGenerator.kt](../src/test/kotlin/uncivai/S04UiScenarioGenerator.kt) writes six UI saves to `build/generated/s04-ui` and verifies them with the real headless loader. [S04UiLauncher.kt](../src/test/kotlin/uncivai/S04UiLauncher.kt) wraps the existing desktop entry point and automatically loads a supplied save after normal initialization. [scripts/s04-ui](../scripts/s04-ui) selects before/after and a scenario. Desktop dependencies use a separate `uiRuntime` configuration; the headless production runtime is unchanged. The engine's existing Discord dependency requires the scoped JitPack repository.
- [EngineDependencyTest.kt](../src/test/kotlin/uncivai/EngineDependencyTest.kt) contains one integration smoke test. It loads both bundled rulesets with `consoleMode = true` and `noMods = true`, checks representative game definitions, and confirms no GDX application or graphics context exists.
- [build.yml](../.github/workflows/build.yml) checks out the recorded submodule revision, verifies its SHA, and builds with Java 21. A `CI=true` environment rejects local checkout overrides.
- The private [Gradle permission rule](../.codex/rules/gradle.rules) permits direct `./gradlew` commands. Shell wrappers and redirection do not match that rule.

The original setup session verified the default build and the sibling override:

```sh
./gradlew --no-daemon --console=plain engineInfo build
./gradlew --no-daemon --console=plain -PuncivDir=/home/vgopalas/projects/Unciv engineInfo build
```

Both configurations passed the original smoke test. The expected rejection of `CI=true` combined with `-PuncivDir` was also checked in that session.

S01 verification used OpenJDK 25.0.4.1 and the pinned engine:

```sh
./gradlew --no-daemon --console=plain generateS01Scenarios test
./gradlew --no-daemon --console=plain engineInfo build
```

Six JUnit tests passed: one dependency smoke test and five lifecycle integration tests, with zero failures or skips. A manual Gradle `run` loaded the generated Vanilla scenario at turn 17 with two civilizations and exited normally. S01 was not tested against the sibling override. Hosted GitHub Actions results and the complete Unciv test suite were not checked.

S02 verification used the same pinned engine and OpenJDK 25.0.4.1:

```sh
./gradlew --no-daemon --console=plain test
./gradlew --no-daemon --console=plain spotlessApply engineInfo build
```

The final build and Spotless checks passed. Ten JUnit tests passed with zero failures, errors or skips: the six S01 tests, three summary JSON/type-contract tests, and one facade integration test that runs the complete S02 probe against each bundled ruleset. Markdown source links and `git diff --check` passed. The sibling override, hosted CI and complete engine test suite were not run for S02.

S03 verification used the same pinned engine and OpenJDK 25.0.4.1:

```sh
./gradlew --no-daemon --console=plain spotlessApply engineInfo build
```

The build and Spotless checks passed. Thirteen JUnit tests passed with zero failures, errors or skips: the ten existing tests, two command DTO/JSON tests, and one S03 integration test that runs its complete probe against each bundled ruleset. After adding a final assertion that rejected IDs remain reusable, `./gradlew --no-daemon --console=plain spotlessApply spotlessCheck test --tests uncivai.S03CityRenameIntegrationTest` also passed. Local Markdown targets and `git diff --check` passed; the engine checkout remains clean. No engine extraction was needed. The sibling override, hosted CI and complete engine test suite were not run for S03.

S04 engine verification:

```sh
./gradlew --no-daemon --console=plain :Unciv:tests:test --tests com.unciv.logic.civilization.GreatPersonSelectionTest
./gradlew --no-daemon --console=plain spotlessApply generateS04UiScenarios build prepareS04UiAssets
```

Eight engine tests and the existing 13 host tests passed. All six UI saves passed loading and placement checks, including blocked placement followed by successful placement after removing the blocking Worker. The complete engine test suite and hosted CI were not run.

Real desktop auto-loading was verified for `blocked-placement` on both engine versions using `runS04Ui -PuiSmokeTest=true` (and `-PuncivDir=build/s04-ui/before-engine -PuiVersion=before` for the original). Each reached the actual great-person picker, printed `S04-UI-READY:blocked-placement.json`, and exited. The UI smoke checks verify startup; the detailed manual selection/recovery sequences are supplied for the user's review. The isolated before checkout remained clean.

After the engine commit was published, `./gradlew --no-daemon --console=plain engineInfo build :Unciv:tests:test --tests com.unciv.logic.civilization.GreatPersonSelectionTest` passed with `engineInfo` reporting `df4f61112b89f3e6b1bd9efc299175b48261ea5c`. The 13 host tests reran successfully; the unchanged eight engine tests remained up to date. Spotless, local Markdown targets and diff checks passed. This integration did not change engine source or repeat the desktop smoke checks.

S05 targeted verification passed:

```sh
./gradlew --no-daemon --console=plain spotlessApply test --tests uncivai.S05FreeGreatPersonIntegrationTest --tests uncivai.PlayerDecisionJsonTest --tests uncivai.PlayerSummaryJsonTest --tests uncivai.S03CityRenameIntegrationTest
```

All eight targeted JUnit tests passed, including the complete S05 probe and the S03 rename regression probe. The Great Admiral fixture adds an adjacent Coast tile to a derived save and reloads it before selection: coastal status is lazy in the engine, and adjacent Ocean alone does not make a city coastal. General command tests select a known land option; the ordinary-options loop still exercises every offered name. S05 required no engine change or new dependency.

The final `./gradlew --no-daemon --console=plain engineInfo build` passed against `df4f61112b89f3e6b1bd9efc299175b48261ea5c` with OpenJDK 25.0.4.1. All 17 host JUnit tests passed with zero failures, errors or skips. Spotless, local Markdown targets and `git diff --check` passed. The engine checkout remains clean. S05 did not rerun the unchanged engine tests or desktop UI smoke checks, the sibling override, or hosted CI.

S01–S05 are complete, with S04 published to the maintained fork and recorded as the host dependency. S02/S03/S05 establish limited own-summary/city observations, city renaming and free-great-person discovery/resolution; the raw runtime and its local diagnostics remain trusted host surfaces. Complete player action/information coverage, other decision handling and MCP transport remain pending. Ordinary engine save migrations and derived-state restoration still apply; S01 does not promise byte-identical in-memory serialization for arbitrary older saves.

## Next implementation step

Implement **S06: a thin MCP adapter over the tested player facade**.

1. Map the five supported session methods and their detached DTOs and fixed errors into the transport. Use a suitable maintained library; adding third-party libraries is already authorized.
2. Keep session-to-player assignment in trusted host provisioning. Client requests cannot select a viewer, access `PlayerHost` factories or obtain raw engine objects, filesystem paths, saves or diagnostics.
3. Exercise two independently bound connections against the same runtime, including observations, city rename and free-great-person selection. Test schema boundaries, foreign handles, successful-ID conflicts, exact retries, stale epochs and closed/halted runtime errors through the transport.
4. Preserve the existing in-memory receipt and epoch semantics. Do not claim replay across new sessions or crashes, or expose end-turn before complete readiness checks exist.

The S04 manual comparison commands remain available for engine regression review. Complete turn readiness and unsupported pending choices remain outside the initial command/choice slices. The complete coverage inventory still applies after S06.

## Architecture constraints to retain

- Initially use one JVM per game because Unciv has global `UncivGame.Current`/GDX state. Multiple player sessions share that game's trusted host.
- The trusted host may hold raw engine state. Agents receive detached, explicitly projected player information; existing View wrappers are not automatically a safe serialization boundary.
- Bind authority to the session on the host. Serialize mutations, revalidate domain legality, reject stale requests, and handle retries without duplicate effects.
- Preserve information learned while briefly visible, including events during internal AI turns. Endpoint snapshots alone are insufficient. The headless plan describes the observation hooks and player-memory sidecars needed later.
- Options, previews, errors, object identifiers, and metadata must obey the same information policy as ordinary observations. Test with paired worlds that differ only in hidden facts.
- Player end-turn requires complete readiness checks; unsupported pending choices must block it. A runtime turn smoke test is distinct from a player-authorized end-turn API.
- Unexpected exceptions after a mutation may leave state changed. Do not promise rollback without implementing it; stop the affected game and return sanitized errors when state integrity is uncertain.
