# Prompt caching for player jobs

## Context layout

Scheduled gameplay and communication use separate messages:

1. **Developer instructions:** stable job workflow, discovery/recovery guidance,
   permitted core tool definitions and prepared-read interpretation instructions.
   The optional [strategy guide](../guides/Strategy-guide.md) sits between the
   workflow and core definitions. This message contains no player identity,
   turn number, private memory or observation values.
2. **User input:** freshly read capabilities, the current request and selected
   private memory, followed by current observations and communication.

The service passes stable instructions through `developerInstructions`. Core
schemas come from the current permitted gateway catalogue. Canonical JSON keeps
key order stable; schema changes change the prefix. Gameplay and communication
have different instructions and tools.

Game data remains at user priority, including scenario text and other players'
messages. Preparation performs normal evidence/accounting reads for every
activation. It does not reuse an old game snapshot. Interactive clients receive
prepared definitions before memory while retaining their normal client context.

Scheduled jobs omit volatile environment/permission prose, project documents and
unrelated skill/plugin, app and collaboration instructions. Actual job directories,
credentials, sandbox settings, approval policy, visibility checks and fresh
conversations remain configured. See [agent context](Agent-context.md).

## Cache support and validation

Stable ordering makes a reusable prefix possible; it does not guarantee a cache
hit. The September 21 probe of Codex 0.155.1 found no supported explicit
cache-breakpoint setting in its app-server schema; injected breakpoint fields
were dropped. The implementation preserves the client's existing caching mode.
Recheck this capability when changing the client version. The
[recorded probe and live comparison](../archive/reports/Prompt-caching.md)
retain the version-specific evidence and its limits.

The opt-in capture test uses separate processes/workspaces and a local canned
model endpoint. It checks stable tool/developer content before changing user
data, omission of local file text, and unchanged model/sandbox settings. It does
not contact a paid model or measure cache hits:

```sh
CIV_TEST_NATIVE_CODEX="$(command -v codex)" .venv/bin/pytest -q -s tests/test_prompt_cache_native.py
```

## Measurement

Histories retain the exact service-supplied developer/user text and their roles.
Activation `startup` metadata includes `layoutVersion`, `instructionsSha256`,
`instructionCharacters`, `userCharacters` and `totalPromptCharacters`. The
existing `characters` field measures prepared definitions/reads plus selected
memory and the optional strategy reference, excluding workflow instructions.
These are character counts, not tokenizer measurements or proof of a cache hit.

For a matched live comparison, retain the opening checkpoint, roster, model,
reasoning effort and turn limit. `tests/compare_vanilla_live.py` reports first-
and later-request usage separately, including uncached input and reported cache
writes; missing write counts remain `null`. Compare absolute input, uncached
input, request counts and elapsed time. Fewer reads and different decisions can
reduce usage independently of cache reuse. See [telemetry](../reference/Telemetry.md)
for token categories and overlapping time measurements.
