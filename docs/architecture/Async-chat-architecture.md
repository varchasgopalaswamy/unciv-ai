# Gameplay, asynchronous chat and recovery

The civilization-service block is repeated for Rome, Greece and each additional
player. Every service has its own jobs, credentials and private memory. Host and
service network endpoints use HTTPS; local Codex process control uses a Unix
WebSocket.

```mermaid
flowchart TB
    subgraph Operator["Operator tools"]
        Scheduler["Scheduler<br/>Turns, leases and job budgets"]
        Recovery["TurnCoordinator<br/>Capture and rewind the active turn"]
        Manifests[("Current turn boundary<br/>Host snapshot and private-memory references")]
        Monitoring[("Monitoring SQLite<br/>Events, run state and telemetry")]
        Export["Explicit history export"]
        Files["Readable reports<br/>Conversations, notebooks and telemetry"]
        Scheduler --> Recovery
        Recovery --> Manifests
        Scheduler --> Monitoring
        Monitoring --> Export
        Export --> Files
    end

    subgraph Civilization["Civilization service — repeated for each player"]
        Control["AgentService · HTTPS /agent<br/>Activation and process lifecycle"]
        Chat["ChatRuntime<br/>Independent event receiver and batched dispatch"]
        Memory[("CivilizationState · private SQLite<br/>Evidence, assessments, jobs, inbox and exact receipts")]
        Gameplay["Codex gameplay job<br/>Fresh selected context"]
        Conversation["Codex communication job<br/>Can run outside the player's game turn"]
        Gateway["ProposalGateway · HTTPS /mcp<br/>Job permissions, credentials, request IDs,<br/>observed host versions and retries"]
        Journal[("Private event journal<br/>Authenticated stream and saved note context")]

        Control --> Chat
        Control -->|"Dispatch without waiting for completion"| Gameplay
        Chat -->|"New private messages or public mentions"| Conversation
        Chat -->|"Persist events and cursor"| Memory
        Memory -->|"Selected snapshot"| Gameplay
        Memory -->|"Selected snapshot"| Conversation
        Gameplay <-->|"HTTPS tool calls and results"| Gateway
        Conversation <-->|"HTTPS tools; communication and memory writes"| Gateway
        Gateway <-->|"Short state transactions"| Memory
        Memory -->|"Read-only notebook export"| Export
        Control -->|"Capture job history"| Journal
        Chat -->|"Capture communication history"| Journal
    end

    subgraph Host["Trusted Kotlin host"]
        OperatorAPI["Operator API · HTTPS /operator"]
        PlayerAPI["Authenticated player API · HTTPS /mcp<br/>Permitted observations and recipient-filtered events"]
        Executor["GameExecutor<br/>Serialized state changes, turn authority,<br/>engine legality and visibility checks"]
        Game["Unciv game state"]
        Ledger["Communication state<br/>Statements and offers<br/>Explicit consent<br/>Confirmed engine outcomes"]
        Checkpoint[("Atomic host checkpoint<br/>Native game, shared ledger and command receipts")]

        OperatorAPI --> Executor
        PlayerAPI --> Executor
        Executor <--> Game
        Executor <--> Ledger
        Executor --> Checkpoint
    end

    Scheduler <-->|"HTTPS control and status"| Control
    Journal -->|"Authenticated event pages"| Scheduler
    Executor -->|"Changed telemetry rows after checkpoint"| Monitoring
    Scheduler <-->|"HTTPS authority and budgets"| OperatorAPI
    Gateway <-->|"HTTPS observations, commands and receipts"| PlayerAPI
    Chat <-->|"HTTPS event long polling and job permits"| PlayerAPI
    Recovery -.->|"HTTPS: fence, snapshot or restore every service"| Control
    Recovery -.->|"HTTPS: fence, snapshot or restore the host"| OperatorAPI
```

Only application code waits for events. Gameplay and communication computations
can overlap while memory transactions and action approval remain serialized.
Communication jobs can inspect permitted game information, but cannot issue
gameplay commands or act on native AI trade/friendship offers. Those offers and
their outcomes stay in the gameplay inbox and do not wake communication jobs.
Shared human/agent negotiations still wake their participants asynchronously.
Models select actions and entity IDs; the gateway supplies
transport credentials, request identities and observed versions.

Explicit resume rewinds an interrupted active turn. The dashed recovery paths
capture and restore the game, shared communications and every player's private
state together. Restore creates a new epoch, rotates credentials and retains
usage accounting. Completed turns stay committed; pending strategic reviewers restart
from frozen inputs. The current boundary is published only after every component
is durable. See [turn recovery](Turn-recovery.md).

See the [implementation guide](../guides/Chat-memory-and-recovery.md) for launch commands,
memory layout, verification and remaining coverage/isolation limits.
