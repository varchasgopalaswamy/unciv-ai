# Selective tool discovery

Status: implemented and evaluated in isolated live gameplay and communication
jobs on September 20, 2026. Discovery output is substantially smaller. Total input
improved modestly in the final comparison; uncached input increased, so this is
not evidence of a billing reduction. Results and limitations are recorded below.

## Goal and baseline

Reduce model input spent discovering tools by using a compact index followed by
full definitions for a small, relevant set of tools.

In the September 20 ordinary-game baseline, Rome's first discovery call printed
34 catalogue entries totaling 35,511 characters. That output remained in context
for subsequent model requests. See the
[full-game readiness diagnosis](Vanilla-readiness.md#first-live-batch-input-token-diagnosis).
Character counts describe payload size; provider usage measures actual tokens.

This plan covers tool discovery. Response deduplication, selective game-state
reads, incremental command updates, and copyable commands are tracked in the
[efficiency follow-ups](Agent-efficiency.md).

## Current flow

1. The agent service starts Codex with the job's authenticated `civ` MCP gateway.
2. Codex requests `tools/list`. The gateway fetches host definitions, filters them
   for the job's purpose, adapts descriptions and schemas, and adds service tools
   for details, memory, chat, and trade.
3. In the recorded runtime, the model can inspect `ALL_TOOLS` through JavaScript
   execution. Entries contain names and descriptions; descriptions also include
   generated TypeScript call signatures.
4. Rome searched names and descriptions with a broad regex containing `civ`, then
   printed every match. The existing registry already permits narrower discovery.

Relevant code:

- [Service startup](../src/civ_agent/agent_service.py)
- [Gateway catalogue and MCP dispatch](../src/civ_agent/proposal_gateway.py)
- [Host catalogue retrieval](../src/civ_agent/host_connection.py)

## Approach

### 1. Establish a shared discovery workflow

Add concise instructions and examples directing agents to:

- List game-tool names when exploring available capabilities.
- Select full descriptions and signatures by exact name for the immediate task.
- Reuse definitions already read within the current conversation.
- Discover additional tools as new decisions arise.
- Go directly to selected definitions when the prompt already identifies the
  required tools, avoiding an unnecessary index request.

Compact index example:

```js
text(
  ALL_TOOLS
    .filter(t => t.name.startsWith("mcp__civ__"))
    .map(t => t.name)
);
```

Selected definitions example:

```js
const needed = new Set([
  "mcp__civ__player_turn",
  "mcp__civ__player_capabilities"
]);
text(ALL_TOOLS.filter(t => needed.has(t.name)));
```

Broad searches should emit names; full definitions should be retrieved for the
selected tools. These are model instructions, so compliance must be checked in
actual traces rather than assumed from the prompt text.

### 2. Apply the guidance consistently

Trace prompt construction for scheduled gameplay, asynchronous communication,
and interactive agents. Put discovery guidance in a shared instruction fragment
and incorporate it once into each applicable startup path. Relevant entry points
include `scheduler.py`, `agent_service.py`, `chat_runtime.py`, and
`commands/agent.py` under `src/civ_agent/`.

Use examples appropriate to each job's available tools. A communication job's
examples must use tools exposed to that job.

### 3. Preserve complete tool documentation

Keep complete argument schemas, prerequisites, and consequential-action guidance
available in the selected definitions. Discoverability must cover the full tool
set available to the job, including tools needed later in the game.

Implement and measure discovery behavior first. Use those measurements to decide
whether lengthy individual descriptions need a subsequent editing pass. Preserve
native legality and player visibility throughout.

### 4. Validate actual agent behavior

Compare short runs from matched saved positions using `gpt-5.6-luna` with medium
reasoning. Use isolated run directories so the baseline and ongoing match remain
available. Include gameplay and a communication job, and exercise discovery of an
additional tool after the initial lookup.

Inspect recorded discovery calls and compare:

- Characters emitted by discovery, separating the initial index from definitions.
- Number of model requests spent on discovery.
- Total, cached, and uncached input tokens per turn or communication job.
- Repeated catalogue dumps and truncated discovery output.
- Invalid tool arguments, recovery calls, and successful task completion.

Attribute improvements cautiously: agent decisions and cache hits can vary between
runs. Evaluate total request usage as well as the size of the first lookup.

## Acceptance criteria

- Initial exploration emits a compact index or a small set of selected definitions.
- Agents successfully discover additional tools when needed.
- Repeated full-catalogue dumps disappear in the validation traces.
- Discovery savings outweigh any additional model requests.
- Tool-use correctness is preserved across gameplay and communication jobs.
- Record the comparison results and any limitations before marking this work done.

## Implementation

The shared fragment lives in
[tool_discovery.py](../src/civ_agent/tool_discovery.py). It gives exact-name lookup
and names-only exploration examples, requests at most four new definitions per
lookup, and tells the model to reuse definitions and discover more from the
observed situation. It also distinguishes game action kinds from tool names and
clarifies the existing structured `intent` argument.

The fragment is inserted once when constructing the actual model prompt:

- Scheduled gameplay and direct/custom service requests: `AgentService.start`,
  before the request, turn instructions, and private memory. The scheduler's
  `player_prompt` stays focused on gameplay, avoiding duplicate guidance.
- Asynchronous communication: `ChatRuntime.compute`, before its communication
  request and private context.
- Interactive sessions, including custom prompts and explicit resume:
  `commands.agent.run`, before the starting request and service-provided context.

The ordinary gameplay opening selects `player_turn`, `player_capabilities`,
`victory_progress`, and `chat_read`. The communication opening selects `chat_read`,
`chat_send`, `memory_search`, and `memory_update`, all exposed to communication
jobs. Subsequent lookups remain unrestricted by the gateway. The existing tool
registry, descriptions, schemas, prerequisites, and action checks are preserved.

## Live comparison

Artifacts are under `/tmp/unciv-discovery-live-20260920-1/`. Baseline services used
the Python source from commit `449dc7a`; candidate services used the working tree.
Each run had its own host, service, credentials copy, and fresh private memory.
Every run began with the identical ordinary-game round-zero checkpoint from
`/tmp/unciv-vanilla-live-20260920-1/round-checkpoints/gt1h0xdqeamas-round-0.json`.
Its comparison SHA-256 is
`c771aff31eb4aff44bb100b1646531595cdd4aa4bd6053c63793f5f708d6fdc8`.
The existing match and baseline artifacts were retained.

Gameplay used the ordinary Rome turn prompt and required a confirmed native
handoff. Communication used the same scripted France message in each isolated
game, asking Rome about a nonbinding understanding to discuss border concerns.
It required a successful model-generated reply. Provider traces confirm
`gpt-5.6-luna` with medium reasoning for every job.

### Final measured runs

| Metric | Gameplay baseline | Gameplay selective | Communication baseline | Communication selective |
| --- | ---: | ---: | ---: | ---: |
| Discovery output characters | 35,511 | 14,524 | 15,943 | 7,861 |
| Names-only index characters | 0 | 0 | 0 | 0 |
| Model requests containing discovery | 1 | 4 | 1 | 1 |
| All model requests | 26 | 27 | 6 | 6 |
| Most definitions in one lookup | 34 | 4 | 9 | 4 |
| Input tokens | 1,122,199 | 1,107,948 | 80,808 | 72,508 |
| Cached input tokens | 1,043,968 | 1,021,184 | 68,096 | 57,856 |
| Uncached input tokens | 78,231 | 86,764 | 12,712 | 14,652 |
| Output tokens, including reasoning | 3,407 | 3,261 | 1,186 | 962 |
| Player-tool calls | 34 | 30 | 6 | 5 |
| Rejected tool calls | 5 | 3 | 0 | 0 |
| Turn-overview reads | 6 | 7 | 0 | 0 |
| Truncated discovery outputs | 0 | 0 | 0 | 0 |

The final directories are `baseline-gameplay`, `selective-gameplay-final`,
`baseline-communication`, and `selective-communication-final`. Each contains
`discovery-run.json`, `discovery-metrics.json`, service history and telemetry.
Metrics include the discovery JavaScript and selected tool names, without copying
raw reasoning. Per-response provider usage reconciles with both the service's
completed record and the host's final job report.

The gameplay opening emitted four selected definitions (4,730 characters).
Later requests discovered action, unit, research, construction, and memory tools.
No final lookup printed the full catalogue; the largest batch contained four
definitions. One lookup repeated `victory_progress`, so reuse is not perfect.
Both native turn completion and the communication reply succeeded. Gameplay's
three rejected calls recovered: two invalid command arguments and one missing
observation. Communication had no rejected calls.

Discovery output fell **59.1% for gameplay** and **50.7% for communication**.
Total input fell **1.3%** and **10.3%**, respectively, despite gameplay taking one
more model request. Uncached input rose **10.9%** and **15.3%**. Agent decisions,
observations, and cache hits varied, and this is one final pair per job type;
these results establish the discovery behavior, not a guaranteed cost reduction.

### Iterations and remaining limitations

Earlier trials are retained rather than omitted from the evidence:

- `selective-gameplay`: general small-set guidance still produced a 13-definition
  batch. Discovery output fell to 15,918 characters, but total input rose to
  1,133,101 tokens and seven commands were rejected.
- `selective-gameplay-v2`: explicit four-definition batches reduced total input to
  993,051 tokens, with 15,517 discovery characters including an 884-character
  names-only index. It tried action kinds as tool names and still had seven
  rejected commands. This motivated the action-routing and structured-intent hints.
- `selective-communication`: discovery output was 11,024 characters, but a mistaken
  plan reference required recovery and total input rose to 103,248 tokens.

The final hints reduced those failures in the measured runs. They are model
guidance, not enforcement. A later run may still rediscover tools or choose a
larger lookup. Interactive prompt wiring is covered automatically; the live
comparison exercises scheduled-style service activation and asynchronous chat.

One existing runtime limitation is visible in the traces: Codex renders the
`player_command` union as `unknown` branches in its generated TypeScript signature.
The complete MCP argument schema remains in the gateway, but this generated
signature does not convey every command's argument shape. Two argument failures
remain in the final gameplay trial. Improving that presentation is separate work;
this change does not remove schema information or weaken validation.

Large game-state responses and duplicated response envelopes dominated much of
the context in this comparison. Subsequent
[response deduplication](Agent-efficiency.md#1-deliver-each-response-once) is now
implemented and live evaluated. Focused observations and command updates remain
outside this discovery change.

## Reproduction and automated validation

[live_tool_discovery.py](../tests/live_tool_discovery.py) runs one isolated job and
stops its services. The state directory must be new. For example:

```sh
.venv/bin/python -m tests.live_tool_discovery \
  --state-dir /tmp/unciv-discovery-gameplay \
  --source-state /tmp/unciv-vanilla-live-20260920-1 \
  --checkpoint /tmp/unciv-vanilla-live-20260920-1/round-checkpoints/gt1h0xdqeamas-round-0.json \
  --port 8981 --purpose gameplay
```

Use `--purpose communication` for the message exercise. For a baseline, export the
desired commit's `src/civ_agent` tree to a separate directory and pass its `src`
directory with `--agent-source`. Use a new state directory for every case.

[verify_tool_discovery_live.py](../tests/verify_tool_discovery_live.py) reconciles
usage and measures discovery from the recorded provider thread:

```sh
.venv/bin/python -m tests.verify_tool_discovery_live /tmp/unciv-discovery-gameplay
```

It uses `~/.codex/sessions` by default; `--sessions` selects a different provider
trace directory. Character counts measure emitted discovery text, not tokens.
Turn-overview reads are reported separately because a refresh is not necessarily
error recovery.

Targeted checks cover gameplay/chat prompt assembly, interactive default/custom
and resume paths, argument boundaries, and discovery measurement without counting
cumulative usage notifications twice. The focused suite and broader scheduler
checks passed 75 tests plus 22 subtests; Ruff and Pyrefly also passed.
