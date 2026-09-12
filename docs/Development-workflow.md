# Development workflow

This repository is private and has one maintainer. It contains the host, MCP adapter, plans, and integration tests. The game engine remains in the separate [Unciv fork](https://github.com/varchasgopalaswamy/Unciv/tree/experimental-changes), with `experimental-changes` as its integration branch.

Read the [repository instructions](../AGENTS.md) before making changes. Unciv commit messages and public contribution text must never reveal this repository's existence, name, or purpose. Describe each engine change accurately in terms of its reusable behavior and tests. Keep private coordination notes here.

## Checkouts and dependency

```text
projects/
├── Unciv/                 # Optional engine development checkout
└── unciv-ai/
    ├── vendor/Unciv/      # Submodule at the recorded engine commit
    └── docs/
```

The submodule URL is `https://github.com/varchasgopalaswamy/Unciv.git`. Its branch setting identifies `experimental-changes` for deliberate updates; the Git submodule entry records the exact commit used by this repository. Ordinary builds and CI use that commit, even if the remote branch has advanced. See [Git's submodule documentation](https://git-scm.com/docs/git-submodule).

After cloning this private repository, initialize the dependency:

```sh
git submodule update --init --recursive
```

Use JDK 21 or newer and the checked-in Gradle wrapper. The included Unciv build retains its own build configuration; no Android SDK is needed for this host build. The first build requires network access to resolve its existing dependencies.

```sh
./gradlew engineInfo build
```

`engineInfo` prints the selected checkout, commit and asset directory. The build imports the selected engine's dependency catalog and consumes `:core` through a [Gradle composite build](https://docs.gradle.org/current/userguide/composite_builds.html). Test and Java execution tasks use `android/assets` from that same checkout. The Gradle wrapper is copied from the initial engine revision; review wrapper compatibility when updating the dependency.

The initial smoke test loads Vanilla and Gods & Kings rules with mods disabled and no graphics backend. The [headless runtime](Headless-runtime.md) adds saved-game loading and shutdown, with separate-process lifecycle scenarios for both rulesets. The [Kotlin player-summary prototype](Player-summary-prototype.md) adds the first player-bound observation surface, and the [city-rename prototype](City-rename-prototype.md) adds serialized command execution and retry receipts. The recorded engine also supplies [free great-person selection](Free-great-person-selection.md), with manual before/after UI fixtures; the [Kotlin choice prototype](Free-great-person-prototype.md) exposes discovery and validated selection through the player session. The [MCP prototype](MCP-prototype.md) exposes these methods to independently authenticated local clients. Remaining player surfaces are tracked in the [headless plan](Headless-player-agent-path.md).

## Python package and checks

The Python application lives in `src/civ_agent/`, with a single installed
`civ-agent` entrypoint. Command parsing is in `cli.py`; command handlers are in
`commands/`. Scheduling, the HTTPS service, history, TLS, and turn archives are
importable modules rather than scripts loaded by filesystem path.

Use Python 3.12 or newer and [uv](https://docs.astral.sh/uv/). The project metadata,
runtime dependencies, and Ruff, Pyrefly, and pytest configuration live in
`pyproject.toml`. `uv.lock` records the resolved runtime and development dependencies.

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pyrefly check
uv run pytest
uv build
```

Ruff checks and formats Python; Pyrefly checks the package and tests; pytest runs
the launcher, scheduler, history, and HTTPS regression suites. Those tests use
temporary match directories and fake model processes. They do not require a live
game or model account. The existing unittest test cases run under pytest alongside
new pytest tests. `uv build` produces a wheel and source archive in `dist/`.

Add dependencies with `uv add PACKAGE`, or development tools with `uv add --dev
PACKAGE`, and include both metadata and lockfile changes. Apply formatting with
`uv run ruff format .`. CI runs the locked checks and package build; the local
prek hooks check Python formatting, lint, and types when relevant files change.

Host commands discover the JVM checkout from the working directory or editable
source tree. An independently installed package can select it with `--repo-dir`
or `CIV_AGENT_REPO`. Agent services need only the Python package and their runtime
tools, credentials, and certificates. See [local commands](Local-launchers.md).

## Work on the engine locally

Use an explicit override to select a sibling checkout or a dedicated engine worktree:

```sh
./gradlew -PuncivDir=../Unciv engineInfo build
```

Relative paths are resolved against this repository's root. The override selects both code and assets, and can exercise uncommitted engine changes. Check `engineInfo` and the selected checkout's Git status to distinguish local results from validation against the pin. CI rejects `uncivDir` overrides.

Develop engine changes on focused feature branches based on `experimental-changes`. The existing sibling checkout may already be on another feature branch; choose a suitable branch or worktree explicitly. Avoid switching another task's checkout just to run a host build.

## Integrate an engine change

1. Implement and validate a cohesive engine change in its own branch. Keep its public commit messages and PR text confined to engine behavior and validation, with no references to this private project.
2. Develop the corresponding private host change using the local checkout override. Public engine tests and explanations must be sufficient for independent review; host integration tests remain here.
3. Publish the engine commit to the fork before recording it in the host. A host commit cannot make an unpublished engine commit available to another checkout.
4. Select the exact published engine commit in the submodule, then run the default build. If the engine change was squashed or rebased when merged, use its final commit.
5. Stage the submodule pointer together with the corresponding host changes and commit in this private repository. A host PR is optional. Submit upstream engine PRs independently of this private workflow.

For step 4, replace `<engine-commit>` with the chosen full commit SHA:

```sh
git -C vendor/Unciv fetch origin experimental-changes
git -C vendor/Unciv switch --detach <engine-commit>
./gradlew engineInfo build
git add vendor/Unciv
```

If the desired commit is still on an engine feature branch, fetch that branch instead. The ordinary submodule update command checks out the recorded commit; use `--remote` only when intentionally choosing a new dependency revision, then review and record that change.

The workflow in [build.yml](../.github/workflows/build.yml) checks out submodules at their recorded revisions, checks the engine SHA, and runs the default build. It is ready for private GitHub hosting; its first hosted run will occur after these setup files are pushed.
