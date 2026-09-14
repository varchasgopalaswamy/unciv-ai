# Python architecture

The Python service coordinates model jobs, player memory and HTTPS calls to the
trusted Kotlin host. Pydantic owns input shapes and tool schemas, the official MCP
Python SDK owns the client protocol, and cryptography provisions TLS identities.

```mermaid
flowchart TB
    Scheduler[Turn scheduler] -->|HTTPS| Service[Civilization agent service]
    Service --> Models[Pydantic request and result models]
    Service --> Jobs[Gameplay and diplomacy jobs]
    Jobs --> Codex[Codex process]
    Codex -->|HTTPS tool calls| Gateway[Player tool gateway]

    Gateway --> Schemas[Pydantic tool schemas]
    Gateway --> Guards[Permission and stale-state checks]
    Guards --> SDK[Official MCP Python client]
    SDK -->|HTTPS| Host[Trusted Kotlin game host]
    Host --> Engine[Unciv engine]

    Gateway <--> Memory[SQLite memory, commitments, and receipts]
    Service --> History[Turn archives and summaries]
    Service --> Mailbox[Continuous event receiver]
    Mailbox <-->|HTTPS through MCP client| Host
    Mailbox --> Memory
    Memory -->|Relevant incoming events| Jobs

    TLS[cryptography: certificates, keys, and host keystore]
    TLS -.-> Service
    TLS -.-> SDK
    TLS -.-> Host
```

## Boundaries and ownership

[schemas.py](../src/civ_agent/schemas.py) validates activation, chat connection and
interactive-session requests. [result_schema.py](../src/civ_agent/result_schema.py)
allowlists the nested final player view before it can be written into a workspace.
Both reject unknown fields and coercions such as a boolean or numeric string used
as a turn number. API errors expose fixed codes, without the submitted input.

[tool_schemas.py](../src/civ_agent/tool_schemas.py) defines the Python-owned trade,
chat, commitment, history and memory tools. Each model supplies both its advertised
JSON schema and its runtime validation. Optional arguments remain omitted when
forwarded. Engine tool schemas still come from the host, which validates game
commands against the actual state. Identity, authorization, stale assumptions,
memory provenance and durable receipts remain explicit domain checks.

[memory_records.py](../src/civ_agent/memory_records.py) provides Pydantic models for
typed plans, beliefs, notes, SOUL, decision intent and memory responses. SQLite
revisions retain lifecycle and provenance; [memory_history.py](../src/civ_agent/memory_history.py)
builds validated summaries from receipts. [memory_exports.py](../src/civ_agent/memory_exports.py)
regenerates Markdown and removes stale managed projections. The
[structured memory guide](Structured-memory.md) documents the tool contract.

[host_connection.py](../src/civ_agent/host_connection.py) uses the official MCP
client with a pooled, authenticated HTTPS client. One background task owns the SDK
context; requests can run concurrently across the event listener and tool handlers.
Gameplay connections close with their jobs, chat connections with their runtime,
interactive connections with their sessions, and CLI connections with their command.
The client negotiates using `initialize`, the protocol implemented by the Kotlin
host. It rejects redirects, verifies the configured CA and hostname, and sanitizes
SDK wire diagnostics. A failed command is never automatically resubmitted: the
gateway reconciles its durable request ID and receipt with the host.

Each agent service also writes operator diagnostics to
`agent-services/<Civilization>/diagnostics.jsonl`, announced at startup. The
standard-library logging queue keeps file writes off the HTTPS event loop;
rotation retains the current 5 MiB file and three backups, all owner-only.
Diagnostics remain outside the model workspace and are not part of memory or
round rollback. They append across restarts, with a new service instance ID and
the Python, MCP, HTTP and Pydantic versions recorded at startup.

For `HOST_PROTOCOL_ERROR`, find `host_call_failed` and follow its `connectionId`
and `requestId`. Gameplay calls include the activation ID and game turn. The
surrounding `host_http_request` / `host_http_response` records show MCP request
IDs, HTTP status, response content type/length and header latency. Call records
show total elapsed time, the failure stage, or success/rejection; session records
show initialization, reconnection and shutdown. `mcp_event` preserves SDK failure
details before the public error is sanitized. Exceptions retain their types,
causes, exception groups, stack locations, OS/TLS/MCP error numbers, and JSON or
validation diagnostics. Raw exception messages, source lines, local variables,
headers, credentials and game/chat payloads are excluded. Public tool errors
remain unchanged.

A failed response does not establish whether a command was enacted. Compare its
request ID with the durable receipt; receipt reads also appear in this log. This
instrumentation adds no command retries or model calls. Diagnostic records are
best-effort troubleshooting data; native checkpoints and receipts remain the
authority for what happened in the game.

The gateway's HTTPS server remains in aiohttp. Its request IDs participate in the
durable tool-call cache. Codex's app-server connection also remains separate: it
uses WebSocket over a Unix socket, not MCP. Open-ended upstream Codex event records
retain their existing filtering and history handling.

[tls.py](../src/civ_agent/tls.py) generates RSA keys, the local certificate authority,
server certificates with DNS/IP names, and the JVM host's encrypted PKCS#12 keystore
using cryptography. It verifies validity, signing authority, server purpose, names,
key correspondence and keystore identity. Existing private-file checks, provisioning
locks, atomic writes, stable identities and combined CA trust bundles still apply.
Provisioning requires no OpenSSL executable.

## Concurrency and recovery

```mermaid
sequenceDiagram
    participant Events as Event receiver
    participant State as Civilization memory
    participant Model as Model job
    participant Gateway as Tool gateway
    participant Host as Kotlin host
    State->>Model: Snapshot with relevant state and policy versions
    par Model computation
        Model->>Model: Plan actions
    and Event reception
        Events->>Host: Await incoming events in code
        Host-->>Events: New message or confirmed outcome
        Events->>State: Persist event and update relevant versions
    end
    Model->>Gateway: Proposed action
    Gateway->>State: Validate dependencies and persist request identity
    alt Assumptions still valid
        Gateway->>Host: Execute validated command over HTTPS
        Host-->>Gateway: Authoritative receipt
        Gateway->>State: Persist receipt
        Gateway-->>Model: Confirmed result
    else Relevant assumptions changed
        Gateway-->>Model: Required observations for a fresh decision
    end
```

Model computation and network waits do not hold the memory writer. `ServiceIO`
serializes short state operations and drains accepted writes during cancellation.
Formal trade negotiation pauses gameplay while communication jobs continue; the
wait happens in code. Checkpoint and rollback behavior is described in
[Chat, memory and recovery](Chat-memory-and-recovery.md).

Pydantic command variants in `game_commands.py` define each action's arguments and
reject invalid combinations before dispatch. `tactical_review.py` validates compact
reviews derived from permitted observations. The host supplies visible hostile
proximity using engine map distances; the Python gateway adds combat command
arguments, review prompts and explicit tactical deferral. See
[tactical and strategic review](Tactical-review.md) for behavior and regression coverage.

Library references: [Pydantic strict validation](https://pydantic.dev/docs/validation/latest/concepts/strict_mode/),
[MCP client transports](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/client/transports.md),
and [cryptography certificate verification](https://cryptography.io/en/stable/x509/verification/).

The Kotlin host's parsing, validation, checkpoint records and communication wakeups
are described in the [Kotlin architecture](Kotlin-architecture.md). `HostConnection`
wraps communication operations in the MCP `request` argument; civilization code
continues to supply the operation fields directly.
