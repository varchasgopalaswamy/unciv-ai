# Documentation

Start with [running a full game](guides/Full-vanilla-run.md), the
[development workflow](Development-workflow.md), or the [current roadmap](Roadmap.md).
The [coverage ledger](reference/Player-api-coverage.md) records implemented and
outstanding player surfaces.

Run shell examples from the repository root unless a page says otherwise.

## Layout

| Directory | Contents |
| --- | --- |
| [guides/](#running-and-inspecting-games) | Launching, operating and inspecting games |
| [architecture/](#architecture) | Current service, storage and model-context behavior |
| [reference/](#player-and-operator-reference) | Player contracts, feature semantics and accounting |
| [scenarios/](#scenarios) | Reproducible guided acceptance exercises |
| [archive/](archive/README.md) | Historical plans, sprint handoffs, audits and measurements |

## Running and inspecting games

| Guide | Use it for |
| --- | --- |
| [Full Vanilla run](guides/Full-vanilla-run.md) | One-command unattended launch, limits, logs and resume |
| [Local launchers](guides/Local-launchers.md) | Interactive agents, host setup and desktop inspection |
| [Ordinary Vanilla setup](guides/Vanilla-game.md) | Manual multi-process launch and game settings |
| [Automatic turns](guides/Automatic-turns.md) | Scheduler, HTTPS services, model settings and recovery |
| [Match dashboard](guides/Match-dashboard.md) | TUI, live output and model-connection status |
| [Chat, memory and recovery](guides/Chat-memory-and-recovery.md) | Human communication, history and whole-match rollback |
| [Optional strategy guide](guides/Strategy-guide.md) | Advisory strategy context, CLI toggle and size measurements |
| [Saved strategic positions](scenarios/Strategic-positions.md) | Reusable failure cases, live model comparisons and opening benchmarks |

## Architecture

| Reference | Contents |
| --- | --- |
| [Python architecture](architecture/Python-architecture.md) | Services, model jobs and module ownership |
| [Kotlin architecture](architecture/Kotlin-architecture.md) | Engine boundary, input contracts and request handling |
| [Gameplay and asynchronous chat](architecture/Async-chat-architecture.md) | Cross-service flow and recovery diagram |
| [Agent context and tools](architecture/Agent-context.md) | Prepared reads, discovery, response projection and flavor filtering |
| [Prompt caching](architecture/Prompt-caching.md) | Stable instructions, variable context and cache measurement |
| [Command state updates](architecture/Command-state-updates.md) | Receipts, deterministic changes and observation freshness |
| [Structured memory](architecture/Structured-memory.md) | Records, provenance, intent, retirement and generated files |
| [Background turn memory](architecture/Background-turn-memory.md) | Handoff, concurrent reflection, finalization and checkpoints |
| [Database access](architecture/Database-access.md) | SQLAlchemy/SQLite ownership and transactions |

## Player and operator reference

### Contracts and accounting

- [Player protocol](reference/Player-api-protocol.md)
- [Coverage ledger](reference/Player-api-coverage.md)
- [Telemetry](reference/Telemetry.md)
- [Compact observations](reference/Compact-observations.md)
- [Tactical and strategic review](reference/Tactical-review.md)

### Cities, units and victories

- [Exploration, settlement and settlement diagnostics](reference/Exploration-and-settlement.md)
- [Workers and city development](reference/City-development.md)
- [City management](reference/City-management.md)
- [Economy and ordinary unit actions](reference/Economy-and-unit-actions.md)
- [Great People, research and policies](reference/Great-people.md)
- [Land and city combat](reference/Combat.md)
- [Naval gameplay and destination orders](reference/Naval-gameplay.md)
- [Air warfare and special military actions](reference/Air-gameplay.md)
- [Victory and turn decisions](reference/Victory-and-turn-blockers.md)

### Diplomacy and information

- [Encounters and diplomatic information](reference/Encounter-and-diplomacy.md)
- [Mixed human, agent and built-in AI diplomacy](reference/Mixed-player-diplomacy.md)
- [War and peace](reference/War-and-peace.md)
- [Trade negotiation](reference/Asynchronous-trade.md)
- [Third-party war trades](reference/Third-party-war-trades.md)
- [Enacted agreement lifecycle](reference/Agreement-lifecycle.md)
- [Social commitments](reference/Social-commitments.md)
- [City-state gameplay](reference/City-state-gameplay.md)
- [Informational popups](reference/Informational-popups.md)
- [Ordinary notifications](reference/Ordinary-notifications.md)

Feature references include scoped verification evidence. A passing prepared
exercise does not establish complete player coverage; consult the coverage ledger
and roadmap for outstanding work.

## Scenarios

- [Complete trade cycle](scenarios/Trade-cycle-scenario.md)
- [Three-Cornered War](scenarios/War-trades-live-scenario.md)
- [Agreements under pressure](scenarios/Agreement-lifecycle-live-scenario.md)

Naval, air, city-state and victory reproduction commands live with their feature
references above. Results from individual runs are in the [archive](archive/README.md).

## Maintaining the docs

Keep the root limited to this index, the roadmap and the development workflow.
Update the relevant guide or reference when behavior changes. Put completed task
plans and dated results in the archive, carrying any unresolved work into the
roadmap. Preserve evidence and its date; old measurements are not current
performance guarantees. Use relative links and update them when moving a page.
