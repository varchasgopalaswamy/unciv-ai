# Player-bound MCP prototype

S06 exposes the five implemented `PlayerSession` methods over a local MCP endpoint. One headless JVM owns the game, and independently authenticated clients operate their assigned players through the same validated facade. Complete player action/information coverage and end-turn readiness remain later work.

## Launch

Generate the existing two-Human-player fixtures and development launcher:

```sh
./gradlew generateS01Scenarios installDist
```

Create a host-only credential file. This example creates a new directory and refuses to overwrite an existing credential file:

```sh
python3 - <<'PY'
import json
import os
import secrets
from pathlib import Path

directory = Path('/tmp/unciv-s06-credentials')
directory.mkdir(mode=0o700, exist_ok=True)
path = directory / 'players.json'
players = [
    {'civilizationId': name, 'token': secrets.token_urlsafe(32)}
    for name in ('Rome', 'Greece')
]
descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, 'w') as stream:
    json.dump({'players': players}, stream)
print(path)
PY
```

The host configuration contains `players`, each with exactly `civilizationId` and `token`. It accepts 1–64 distinct players and credentials. Each token is a 43-character unpadded Base64URL value; generate it from 32 random bytes as above. The host validates every player binding before opening the listener. Configuration is limited to 64 KiB and must have owner-only read/write permissions on POSIX filesystems. On other filesystems, protect it with the platform's access controls. Never give the complete configuration or game save to a player client.

Start the game server from the repository root:

```sh
build/install/unciv-ai/bin/unciv-ai \
  --save "$PWD/build/generated/s01-scenarios/vanilla.json" \
  --data-dir /tmp/unciv-s06-runtime \
  --mcp-config /tmp/unciv-s06-credentials/players.json \
  --port 8765
```

The host reports `MCP-READY:http://127.0.0.1:8765/mcp` after startup. Port `0` selects an available port and reports the actual address. The server always binds `127.0.0.1`. Configure each machine MCP client with that URL and an `Authorization: Bearer <its-token>` HTTP header. Give Rome's client only Rome's credential, and Greece's client only Greece's credential. The URL contains no player identity or credential.

Ctrl-C or SIGTERM closes the listener and headless runtime. The input save is not rewritten and mutations are not automatically persisted. Omitting `--mcp-config` keeps the original load/report/exit behavior; `--port` requires serve mode. The generated launcher's engine/assets path is specific to this development checkout.

## Tools and responses

| MCP tool | Arguments | Result |
| --- | --- | --- |
| `player_summary` | None | The eleven-field own-player summary. |
| `owned_cities` | None | Runtime epoch and session handles/names for owned cities. |
| `rename_city` | `requestId`, `epoch`, `cityHandle`, `name` | The existing rename receipt. |
| `free_great_person_choice` | None | Runtime epoch and nullable pending choice, options and handle. |
| `choose_free_great_person` | `requestId`, `epoch`, `decisionHandle`, `unitName` | The existing selection receipt. |

Each command argument is a JSON string. Queries accept omitted or null arguments, or an empty object. Missing, extra, null, numeric or container-valued command fields are rejected; a caller cannot add a viewer, player ID or engine property. The SDK's schema type cannot advertise `additionalProperties: false` at the root, so the adapter enforces the exact argument whitelist at execution as well as listing the supported fields.

Optional output schemas are omitted. The pinned SDK's `ToolSchema` cannot express a top-level union of success DTOs and the error envelope, and some clients [validate structured errors against the success schema](https://github.com/modelcontextprotocol/typescript-sdk/issues/1943). The response DTO and error whitelists remain explicit and are checked through the wire tests.

Successful tool responses contain the existing detached DTO as `structuredContent`, with the same JSON in a text content block for clients that consume text. Domain failures set `isError: true` and return `{"error":{"code":"REASON","message":"Fixed message"}}` in both forms. The reasons and gameplay rules remain those of the [city command](City-rename-prototype.md) and [great-person choice](Free-great-person-prototype.md). Unexpected adapter failures, including `Error`, return a fixed `INTERNAL_ERROR` without diagnostic causes; cancellation propagates. Invalid protocol messages and unknown tools use MCP protocol errors. An array, number or string in place of the entire arguments object is a malformed MCP request, handled by the SDK before the tool adapter.

Tool discovery is static capability discovery; it does not assert that a command is currently legal. Own observations remain available out of turn. Both current-player checks, live city ownership, choice identity, epoch and retry handling remain in the existing facade. No arbitrary engine access, raw object serialization, resources, prompts, filesystem access, debug tools or end-turn operation is registered.

## Authentication and session lifetime

The host uses the official [Kotlin MCP SDK 0.15.0](https://github.com/modelcontextprotocol/kotlin-sdk/releases/tag/0.15.0) and its [stateless Streamable HTTP transport](https://github.com/modelcontextprotocol/kotlin-sdk/tree/0.15.0). Ktor 3.5.1 supplies the HTTP server; the host's Kotlin serialization and coroutine libraries are aligned with the SDK at 1.11.0. Protocol framing, parsing and HTTP transport are implemented by those libraries.

Here, “stateless” describes MCP transport sessions. Each credential still owns a persistent, distinct game `PlayerSession` for the server lifetime. Every HTTP request authenticates before reaching its SDK server. A new TCP connection or MCP initialization using the same credential retains its handles and successful-request ledger. Changing credentials selects the separately provisioned session; it never changes the actor of an existing session.

Exact successful retries retain the S03/S05 behavior, including after another command or a transport reconnect. Reusing a successful request ID for another payload or command kind conflicts. New server instances get new facade sessions; game reloads also get a fresh epoch. There is no receipt persistence across a restart and no claim of cross-crash exactly-once execution.

This endpoint uses JSON responses and no protocol session IDs or SSE streams. It rejects `Mcp-Session-Id` headers. GET and DELETE return 405 after authentication. Requests without valid credentials receive a generic 401. The endpoint validates the Host header and rejects every Origin header with 403: this prototype supports machine clients, with no browser-origin allowlist or OAuth discovery. It is a loopback development service, not a public deployment.

The SDK bounds request bodies to 4 MiB, including chunked bodies. Tokens are read only from the Authorization header, never query parameters or request arguments. Credentials, host paths and diagnostics are absent from tool results and schemas. Operational output remains in the trusted host console, separate from the HTTP responses.

## Verification

The S06 integration probe starts a real loopback listener over the generated saves for both bundled rulesets. Official SDK clients initialize independently, list tools and call the facade through HTTP. Checks inspect actual city names, placed units and entitlement counts, and exercise authority, foreign handles, strict arguments, hidden-state independence, request conflicts, duplicate calls, reconnect retries and closed/halted runtime errors. Raw HTTP checks cover the authentication and protocol boundary. Separate CLI tests cover credential loading, readiness and shutdown.

```sh
./gradlew --no-daemon --console=plain engineInfo build
```

The [headless implementation path](Headless-player-agent-path.md) tracks the remaining game coverage. S06 does not make these five tools a complete playable game or establish the privacy of future projections.
