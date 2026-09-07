# Headless load and shutdown

S01 provides a trusted, local executable that loads an existing Unciv save, reports the loaded game, and closes the runtime. It uses the selected engine checkout's core library and bundled assets. S02 adds a [Kotlin player-summary prototype](Player-summary-prototype.md), and S03 adds [owned-city observations and renaming](City-rename-prototype.md); complete player coverage and transport remain later steps.

## Run a saved game

Use JDK 21 or newer. Supply absolute paths for the save and runtime data directory:

```sh
./gradlew run --args="--save /absolute/path/to/game.json --data-dir /tmp/unciv-match"
```

The save can use the engine's JSON or compressed save format. The runtime accepts the bundled Vanilla and Gods & Kings rulesets and rejects mod dependencies. The data directory must be outside the selected engine's asset directory. The executable leaves the input save in place and does not advance turns.

For local engine development, the same override selects both code and assets:

```sh
./gradlew -PuncivDir=../Unciv run --args="--save /absolute/path/to/game.json --data-dir /tmp/unciv-match"
```

Gradle supplies the absolute asset path through `uncivai.assetsDir`. Generated application launch scripts also reference that selected checkout; they require its assets to remain available. These are development launchers, not a self-contained redistribution of the engine assets.

## Reproduce the S01 scenarios

Generate two saved scenarios from the test sources:

```sh
./gradlew generateS01Scenarios
./gradlew run --args="--save $PWD/build/generated/s01-scenarios/vanilla.json --data-dir /tmp/unciv-s01-vanilla"
./gradlew run --args="--save $PWD/build/generated/s01-scenarios/gods-and-kings.json --data-dir /tmp/unciv-s01-gnk"
```

The generator is test tooling. Production loading depends only on the host and engine core. Each scenario contains two Human players, units and cities, and unresolved choices, so verification can detect unintended player decisions during loading.

## Runtime ownership

`HeadlessRuntime.load(saveFile, dataDirectory)` exposes the loaded `GameInfo` to trusted host code and implements `AutoCloseable`. Use it with Kotlin's `use` to release the runtime on success or failure. Only one runtime may be active in a JVM because the engine uses global state. Raw engine objects are not a player-facing API or an enforced information boundary.

Each load assigns a fresh opaque epoch. Player reads, command validation, mutation, receipt recording and shutdown share the runtime's lock. If an unexpected failure occurs after a command starts mutating state, the runtime retains the cause for trusted diagnostics and rejects further player operations. It can still be closed; it does not promise rollback.

The runtime creates fresh settings in memory with automatic player decisions disabled. It does not read or save desktop settings. File services resolve bundled assets and runtime data independently, avoiding the console settings loader's relative `GameSettings.json` path. No display, graphics application, world screen, audio service, simulation, or desktop launcher is started.

Loading uses the engine's compatibility checks and transient restoration. Those operations may migrate old save data or rebuild derived fields; S01 does not promise byte-identical in-memory serialization for arbitrary older saves. Its lifecycle checks verify that the supported scenarios retain their turn, current player, units, city production and pending choices. Shutdown releases the engine worker pools and allows the process to exit normally.

Run the integration checks with:

```sh
./gradlew --no-daemon --console=plain engineInfo build
```

The lifecycle tests launch separate JVMs with display environment variables removed and a bounded exit timeout. They exercise the real saved-game loader and command-line executable, including failure paths and isolation from source assets.
