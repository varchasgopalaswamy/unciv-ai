# Session handoff

Saved 2026-09-06 after implementing S01, headless saved-game load and shutdown. This is a private continuity note, not material for the Unciv fork or its public contributions.

## Read first

- [AGENTS.md](../AGENTS.md) contains the confidentiality rule and contribution instructions.
- [Development workflow](Development-workflow.md) describes the submodule and local checkout override.
- [Headless runtime](Headless-runtime.md) documents the executable, scenario generation and lifecycle ownership.
- [Headless implementation path](Headless-player-agent-path.md) is the current approach and initial sequence, S01–S06.
- [Player API review](Player-agent-API-review.md) is the completeness inventory. The [original implementation plan](Player-agent-implementation-plan.md) remains a broader backlog; its desktop attachment and extensive View migration prerequisites are superseded for this headless route.

## User decisions

- This host repository remains private and has one maintainer. Ordinary private branches and commits are sufficient; host PRs are optional.
- Adding third-party libraries is already authorized. Prefer suitable existing libraries over reinventing functionality that is even moderately complex; this preference also appears in `AGENTS.md`.
- Never reveal the private repository's existence, name, or purpose in Unciv commit messages, including commits in the submodule or a sibling checkout. Public PR text and branch names must also contain only accurate engine behavior and validation, without private context. Keep these instructions and plans here.
- The required outcome is several agents controlling different players in one game, with every ordinary human player action and every permitted human information surface. Cover both bundled Vanilla and Gods & Kings rulesets and nation-specific abilities. Ignore mod content.
- Use structured, headless control. UI automation, screenshots, and delegating unimplemented human choices to the game's AI do not meet the requirement. Ordinary automation a human can choose remains in scope.
- Keep public engine work small and independently reviewable. The private host can advance against the maintained fork without waiting for upstream acceptance.

## Current repository state

This repository remains on `main`, tracking `origin/main`. S01 is an uncommitted host change. The session began with existing edits to `AGENTS.md` and `README.md` and an untracked handoff; those changes were preserved and extended. No engine files, engine revisions or commits were changed. Inspect current status before editing.

The submodule is `vendor/Unciv`, with URL `https://github.com/varchasgopalaswamy/Unciv.git` and integration branch `experimental-changes`. Its recorded commit is:

```text
d77a312ed6ba0d127fe4efdfbcdc9f4918c0b5fa
```

The sibling `/home/vgopalas/projects/Unciv` is a separate development checkout. At the original setup handoff it was on `combat-recording-unit-infrastructure`, at `630e1938ca54ec4835620594b5f7cb84971ce75e`, with an existing untracked `.codex/` directory. It was not used or changed for S01. Use its current branch deliberately; it is a different revision from the pin. Earlier combat-history discussions concerned other feature branches, so verify APIs against the selected checkout.

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

S01 is complete. The raw runtime and its local diagnostics are trusted host surfaces. There is no player API, enforced information boundary, gameplay command path, or MCP transport yet. Ordinary engine save migrations and derived-state restoration still apply; S01 does not promise byte-identical in-memory serialization for arbitrary older saves.

## Next implementation step

Implement **S02: player-bound, detached own-player summaries** as the next atomic change.

1. Build an in-process facade over the loaded runtime, with the host assigning each client its Human player context.
2. Return detached own-player summaries with an explicit permitted-field contract. Do not expose raw engine objects, managers or caller-selected viewer identities.
3. Exercise two separately bound scripted clients and paired worlds differing only in unrelated hidden state. Each player's permitted output must remain independent of those hidden changes.
4. Publish only the implemented query capability. Preserve the full action and information inventory for later work; an own-summary API is not complete coverage.

Then follow S03 (serialized validated commands), S04/S05 (a reusable free-great-person choice and its adapter), and S06 (thin MCP transport). S04 is independently reviewable and can be developed alongside the earlier host steps. The complete coverage inventory still applies after this initial sequence.

## Architecture constraints to retain

- Initially use one JVM per game because Unciv has global `UncivGame.Current`/GDX state. Multiple player sessions share that game's trusted host.
- The trusted host may hold raw engine state. Agents receive detached, explicitly projected player information; existing View wrappers are not automatically a safe serialization boundary.
- Bind authority to the session on the host. Serialize mutations, revalidate domain legality, reject stale requests, and handle retries without duplicate effects.
- Preserve information learned while briefly visible, including events during internal AI turns. Endpoint snapshots alone are insufficient. The headless plan describes the observation hooks and player-memory sidecars needed later.
- Options, previews, errors, object identifiers, and metadata must obey the same information policy as ordinary observations. Test with paired worlds that differ only in hidden facts.
- Player end-turn requires complete readiness checks; unsupported pending choices must block it. A runtime turn smoke test is distinct from a player-authorized end-turn API.
- Unexpected exceptions after a mutation may leave state changed. Do not promise rollback without implementing it; stop the affected game and return sanitized errors when state integrity is uncertain.
