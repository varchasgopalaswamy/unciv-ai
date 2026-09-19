# Gameplay, asynchronous chat and recovery

The civilization-service block is repeated for Rome, Greece and each additional
player. Every service has its own jobs, credentials and private memory. Host and
service network endpoints use HTTPS; local Codex process control uses a Unix
WebSocket.

```mermaid
flowchart TB
    subgraph Operator["Operator tools"]
        Scheduler["Scheduler<br/>Turns, leases and job budgets"]
        Recovery["RoundCoordinator<br/>Round checkpoints and emergency restore"]
        Manifests[("Complete round manifests<br/>Host snapshot and private-memory references")]
        Scheduler --> Recovery
        Recovery --> Manifests
    end

    subgraph Civilization["Civilization service — repeated for each player"]
        Control["AgentService · HTTPS /agent<br/>Activation and process lifecycle"]
        Chat["ChatRuntime<br/>Independent event receiver and batched dispatch"]
        Memory[("CivilizationState · private SQLite<br/>Evidence, plans, jobs, inbox and exact receipts")]
        Gameplay["Codex gameplay job<br/>Fresh selected context"]
        Conversation["Codex communication job<br/>Can run outside the player's game turn"]
        Gateway["ProposalGateway · HTTPS /mcp<br/>Job permissions, credentials, request IDs,<br/>dependency checks and retries"]
        Files["Readable exports<br/>SOUL, strategy, relationships,<br/>chat, turn summaries and receipts"]

        Control --> Chat
        Control -->|"Dispatch without waiting for completion"| Gameplay
        Chat -->|"New private messages or public mentions"| Conversation
        Chat -->|"Persist events and cursor"| Memory
        Memory -->|"Selected snapshot"| Gameplay
        Memory -->|"Selected snapshot"| Conversation
        Gameplay <-->|"HTTPS tool calls and results"| Gateway
        Conversation <-->|"HTTPS tools; communication and memory writes"| Gateway
        Gateway <-->|"Short state transactions"| Memory
        Memory -->|"Generate memory and receipt views"| Files
        Control -->|"Capture job history"| Files
        Chat -->|"Capture communication history"| Files
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
    Scheduler <-->|"HTTPS authority and budgets"| OperatorAPI
    Gateway <-->|"HTTPS observations, commands and receipts"| PlayerAPI
    Chat <-->|"HTTPS event long polling and job permits"| PlayerAPI
    Recovery -.->|"HTTPS: fence, snapshot or restore every service"| Control
    Recovery -.->|"HTTPS: fence, snapshot or restore the host"| OperatorAPI
```

Only application code waits for events. Gameplay and communication computations
can overlap while memory transactions and action approval remain serialized.
Communication jobs can inspect permitted game information, but cannot issue
gameplay commands. Models select actions and entity IDs; the gateway supplies
transport credentials, request identities and observed versions.

Normal interruption preserves confirmed work. The dashed recovery paths represent
coordinated checkpointing and explicit whole-match restoration; restore creates a
new epoch, rotates credentials, restores all private memory views and retains usage
accounting. The manifest is published only after every component is durable.

See the [implementation guide](Chat-memory-and-recovery.md) for launch commands,
memory layout, verification and remaining coverage/isolation limits.
