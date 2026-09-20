# unciv-ai

A private headless host and MCP adapter project for agents playing Unciv, depending on a separately maintained Unciv fork. Agents must eventually have every ordinary human player action and information surface while receiving only information their player is allowed to know. Scope covers the bundled Vanilla and Gods & Kings rulesets; mod content is excluded.

The repository provides a headless executable and authenticated HTTPS MCP server. The player interface supports explored-map observations, current-turn and multi-turn destination movement, settlement, land/naval/air combat, carrier rebasing, paradrops, nuclear strikes and Work Boat improvements, worker jobs, city yields and citizen assignment, construction queues, research, policies, free Great People, informational alerts including first contact, known-civilization identity, validated war declarations and peace negotiations, and validated turn advancement. Checkpoints persist game state and command receipts together. A supervisor starts a fresh Codex conversation for each game turn through HTTPS agent services, carrying forward selected structured memory and following the host's committed turn changes.

**This is an incomplete gameplay implementation.** Further city controls, remaining unit actions, religion/espionage, broader diplomacy, full desktop integration, and agent filesystem/network isolation remain outstanding. Consult the [coverage ledger](docs/Player-api-coverage.md) rather than treating a passing turn-flow test as full parity.

The [encounter slice](docs/Encounter-and-diplomacy.md) lets players inspect and
acknowledge first contact, then continue scheduled turns. It adds an initial view
of known rivals and tests encounters and recovery across checkpoint restarts.
Ordinary [informational popups](docs/Informational-popups.md) include their full
prose, quotes and civilization-specific technology descriptions. Agents are
instructed to read and note their content, and acknowledgement requires a current
`player_turn` observation.

The [war and peace slice](docs/War-and-peace.md) adds legal diplomatic options,
war-message acknowledgement, and peace offers with gold and resource terms,
including accept/reject/counteroffer and durable retries. Gameplay launchers and
`civ-agent match start` accept `--model NAME --reasoning-effort LEVEL`.

The [trade negotiation slice](docs/Asynchronous-trade.md) adds canonical
economic and peace proposals, friendship, embassies, Open Borders, research
agreements, defensive pacts and city transfers. Agents accept the exact stored
terms they inspected, including outside their gameplay turn. The current player
opens negotiation and gameplay pauses until it closes. The default limit is three
shared counteroffers and five minutes; configure `--trade-counteroffers` and
`--trade-timeout` at host launch. Chat and communication jobs stay responsive. Use
`--scenario trade-cycle` for the [complete trade exercise](docs/Trade-cycle-scenario.md),
including counteroffers, terminal decisions, paid peace, every supported bilateral item,
and native-save verification with `civ-agent match verify-trade-cycle`.
`civ-agent trade` provides human inspection and decisions.

[Third-party war trades](docs/Third-party-war-trades.md) add paid and joint
declarations against mutually known majors, with private consent, native war
effects and durable target notifications. The [sprint handoff](docs/Sprints.md)
records completed deliveries, including the [air warfare sprint](docs/Air-gameplay.md).

Read [AGENTS.md](AGENTS.md) for confidentiality and contribution instructions, and [Development workflow](docs/Development-workflow.md) for engine branches, local overrides and dependency updates.

With JDK 21 or newer:

```sh
git submodule update --init --recursive
./gradlew engineInfo build
```

To develop against a sibling engine checkout:

```sh
./gradlew -PuncivDir=../Unciv engineInfo build
```

Install the Python package and development tools with [uv](https://docs.astral.sh/uv/).
Python 3.12 or newer is required:

```sh
uv sync --locked
uv run civ-agent --help
```

By default, new games use **Vanilla rules and Quick speed**, with **Rome, Greece,
and France** each trying to win. Start the host, three services and the supervisor
in five terminals:

```sh
uv run civ-agent host start --state-dir /tmp/civ-vanilla-1 --new
uv run civ-agent agent-server start Rome --state-dir /tmp/civ-vanilla-1
uv run civ-agent agent-server start Greece --state-dir /tmp/civ-vanilla-1
uv run civ-agent agent-server start France --state-dir /tmp/civ-vanilla-1
uv run civ-agent match start --state-dir /tmp/civ-vanilla-1 --max-turns 331 --max-invocations 1200 --max-seconds 172800 --turn-timeout 600
```

Wait for the host and services to report ready; choose a fresh directory for a new
match. The [ordinary-game guide](docs/Vanilla-game.md) covers setup, model choices,
limits and inspection. The only scripted scenario remains
[trade-cycle](docs/Trade-cycle-scenario.md), selected explicitly with
`--scenario trade-cycle`. See [chat, memory and recovery](docs/Chat-memory-and-recovery.md)
for human chat and whole-match rollback. Interactive `agent start` requires its
civilization's service and uses a scoped proposal gateway. Credentials, request
IDs, observation versions and retry reconciliation are handled by code; models
select gameplay choices and entity IDs.

The [Python architecture diagrams](docs/Python-architecture.md) show the service,
concurrent model jobs, memory, and HTTPS boundaries, including Pydantic validation,
the official MCP client, and cryptography certificate provisioning. The
[Kotlin architecture](docs/Kotlin-architecture.md) covers Clikt, Networknt input
validation, typed communication records, and coroutine subscriptions.

All Python commands use the `civ-agent` entrypoint: `host start`, `host inspect`,
`agent start`, `agent-server start`, `match start/resume/status/pause/stop/manual`,
`chat send/inbox/follow/ledger/offer/accept/withdraw/reconsider`, `match rollback`,
and `history export`. Add `--help` after any command for its options. The former
Python scripts have been replaced by this package; existing match directories and
saved credentials retain their layout.

For automatic turns, start one HTTPS service per civilization and run the supervisor.
See [automatic turns and HTTPS setup](docs/Automatic-turns.md) for commands, limits,
pause/resume, and separate-machine configuration.
Player archives use readable paths such as `agents/Rome/turn_0/`: `commands.jsonl`
contains detailed calls and receipts. `summary.json` stores typed decisions,
confirmed outcomes and memory assessments; `summary.md` renders these in at most
200 words under the API's turn number. Final model prose stays in the transcript.
Fresh conversations use selected private memory and new events. See the
[structured memory plan](docs/Structured-memory-plan.md) and
[memory tools](docs/Structured-memory.md) for lifecycle and intent examples.
Normal recovery retains confirmed actions, messages and notes, then reconciles
uncertain responses before starting fresh computation. Explicit `match rollback`
restores all players and the game to a coordinated round checkpoint under a new
epoch. The scheduler displays
live player output and saves responses, exposed reasoning summaries and tool history; see [history locations and existing-run export](docs/Automatic-turns.md#live-output-and-saved-history).

To verify the agents' actions visually, run `uv run civ-agent host inspect Rome` or
`uv run civ-agent host inspect Greece`. This exports the checkpoint and opens a separate
inspection copy in the desktop UI. Omit the civilization to view the actual active
player. See [inspection options](docs/Local-launchers.md#inspect-what-the-agents-did).

To generate and load an internal lifecycle test fixture:

```sh
./gradlew generateS01Scenarios
./gradlew run --args="--save $PWD/build/generated/s01-scenarios/vanilla.json --data-dir /tmp/unciv-s01"
```

- [Player protocol](docs/Player-api-protocol.md): current tools, request examples, checkpoints, and limitations.
- [Economy and unit actions](docs/Economy-and-unit-actions.md): finances/resources, gold purchases, upgrades, pillage/disband and a prepared exercise.
- [City management](docs/City-management.md): focus/growth, specialists and great-person progress, building sales, shared tiles and a two-city exercise for each civilization.
- [Great People](docs/Great-people.md): usable abilities, on-demand research/policy details, and an exercise for both players to generate and use people.
- [Local launchers](docs/Local-launchers.md): start the host and interactive Codex players without copying tokens.
- [Naval gameplay and destination orders](docs/Naval-gameplay.md): route previews, persistent movement and sea-resource improvements.
- [Exploration and settlement](docs/Exploration-and-settlement.md): launch a fresh duel using existing player credentials, movement semantics, and validation.
- [Implementation design](docs/Player-api-plan.md): responsibilities, milestones, and acceptance gates.
- [Coverage ledger](docs/Player-api-coverage.md): implemented and outstanding action and information surfaces.

Engine changes under development live in the selected checkout. A clean checkout at the recorded submodule revision does not include uncommitted engine changes.

The [basic combat slice](docs/Combat.md) adds land/city attacks, tactical orders,
promotions, capture choices and durable combat reports with terminal result delivery.
