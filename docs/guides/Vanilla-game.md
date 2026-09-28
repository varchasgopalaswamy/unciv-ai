# Ordinary Vanilla game

For a complete local match, use `civ-agent game start`; see
[the full-game guide](Full-vanilla-run.md) for map, roster and resume options.
The steps below are for starting and controlling services individually.


Without `--scenario`, a new host creates an ordinary game between **Rome, Greece,
and France**. Each civilization's objective is **to win the game**. Gameplay and
communication jobs use this objective, choose their own strategy, and preserve
plans in private memory. There are no scripted diplomacy or development tasks.

The setup uses the bundled **Civ V - Vanilla** ruleset, **Quick** speed (the fastest
bundled speed), Prince difficulty, Ancient era, and a generated Small Pangaea map.
All five native victory conditions are enabled: Scientific, Cultural, Domination,
Diplomatic, and Time. Quick's time victory limit is 330 turns. Barbarians, ruins,
and natural wonders use ordinary engine settings. City-states default to zero;
`--city-states NUMBER` includes native city-states in a new ordinary game. Mods
are disabled.
Use `--built-in-ai Egypt` on the initial host command to add Egypt under native
AI control. The option may be repeated for other distinct major civilizations;
these opponents receive no agent credentials. Omit it on resume, which preserves
the saved player roster. The [full-game launcher](Full-vanilla-run.md) combines this
setup with [per-turn accounting](../reference/Telemetry.md) and unscripted live acceptance.
Each civilization starts with the engine's normal Settler and Warrior, treasury,
and starting technology. All three occupy Human slots, so the host waits for their
agents instead of substituting Unciv's civilization AI. Rome acts first, then
Greece, then France.

## Launch

From the repository, run each command in a separate terminal using the same fresh
match directory. Wait for `MCP-READY` and all three `AGENT-READY` messages before
starting the scheduler:

```sh
# Host
uv run civ-agent host start --state-dir /tmp/civ-vanilla-1 --new

# Rome service (HTTPS port 8865)
uv run civ-agent agent-server start Rome --state-dir /tmp/civ-vanilla-1

# Greece service (HTTPS port 8866)
uv run civ-agent agent-server start Greece --state-dir /tmp/civ-vanilla-1

# France service (HTTPS port 8867)
uv run civ-agent agent-server start France --state-dir /tmp/civ-vanilla-1

# Scheduler
uv run civ-agent match start --state-dir /tmp/civ-vanilla-1 --max-turns 331 --max-invocations 1200 --max-seconds 172800 --turn-timeout 600
```

The host provisions three private player credentials automatically. Each service
may take `--model MODEL --reasoning-effort LEVEL`; passing those options to
`match start` overrides all three. Set `--port` on a service if its default port is
already occupied. Use a persistent directory instead of `/tmp` to keep a match
across system cleanup.

The scheduler's limits above allow a full Quick game, including recovery attempts,
with a two-day wall-clock limit. It stops earlier when the engine reports victory
or when a blocker requires attention. Its default limits are shorter (20 rounds,
48 activations, one hour), so use the explicit limits for a full game. This setup
does not add the missing player API features listed in the [coverage ledger](../reference/Player-api-coverage.md).

`--seed NUMBER` optionally selects the terrain seed for a new game. Without it,
the engine receives a fresh time-based seed. The host prints the seed and settings.
Unciv also uses a random game ID for setup choices, so a terrain seed does not
reproduce the entire initial state. Reuse the saved checkpoint for exact state.

To include two city-states, add `--city-states 2` to the host's initial `--new`
command. Native AI controls their turns. The [city-state API](../reference/City-state-gameplay.md)
exposes met city-states and their ordinary interactions. This option is valid only
for new ordinary games; omit it on resume, which retains the saved setup. Ordinary
starting funds and units remain native; the guided acceptance fixture is separate.

## Resume and inspect

Restart the host with the same directory and omit `--new`; it resumes the saved
game and credentials. Restart the three services, then use
`uv run civ-agent match resume --state-dir /tmp/civ-vanilla-1`.

Inspect a copy of any player's current position in the desktop UI:

```sh
uv run civ-agent host inspect France --state-dir /tmp/civ-vanilla-1
```

Archives and memory live in `agents/Rome`, `agents/Greece`, and `agents/France`.
The API exposes public rules, enabled victories and the time limit in
`player_turn.rules` and the communication context. Existing saves keep their own
rules and players when resumed. To run the two-player trade exercise instead,
select `--scenario trade-cycle` in a separate match directory.
