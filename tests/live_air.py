"""Opt-in guided air warfare game, using the ordinary scheduler and three real model services.

First generate a fresh checkpoint with generateAirLiveFixture. This wrapper adds
player objectives and owns process cleanup; it never issues a gameplay command.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from civ_agent import scheduler
from civ_agent.commands.host import ensure_players
from civ_agent.monitoring import path_for, read_record
from civ_agent.tls import write_private_json

MODEL = "gpt-5.6-luna"
EFFORT = "medium"
PLAYERS = ("Rome", "Greece", "France")


def objective(civilization: str, turn: int | None) -> str:
    common = (
        "This is a guided Vanilla air warfare acceptance exercise with Rome, Greece and France. "
        "Inspect unit_details.air for available missions, ranges, bases, transport and automatic interception. "
        "Call mission_preview(target,mission,destination) with REBASE, PARADROP, AIR_SWEEP or NUCLEAR_STRIKE, "
        "read it, then use REBASE_UNIT, PARADROP, AIR_SWEEP or NUCLEAR_STRIKE with matching target/destination. "
        "Nuclear launches require confirmAction=true after reading consequences. Native nuclear attacks may "
        "declare war; do not declare that war separately. Air and guided-missile strikes use offered ATTACK "
        "commands from unit_details.combat.attacks. Preview damage excludes interception; read actual receipts "
        "and combatReports. Interception is automatic, so unused/Skipped fighters retain native readiness. "
        "Carrier movement uses movement_preview then SET_UNIT_DESTINATION; the aircraft travel aboard it. "
        "Use destinations, never manual tile-by-tile paths. You have funded Gold, Oil, Aluminum and Uranium. "
        "Milestones take priority over conquest. Do not attack or move milestone units unnecessarily. "
        "You may negotiate your own exercise limits in structured chat; keep messages brief. Trade and native "
        "peace use the trade flow. Keep research/production running, resolve ordinary choices, inspect turn "
        "review and END_TURN when done. On resume inspect current state/receipts and skip completed actions. "
    )
    roles = {
        "Rome": (
            "Turn 0: REBASE Carrier wing from Rome onto Carrier at (-3,-2), then move Carrier to (0,-2) "
            "using a destination preview/order. Verify both units arrived and Carrier wing is transported. "
            "Upgrade Reserve wing from Fighter to Jet Fighter and disband Retired bomber using their native "
            "economic options/confirmations. Declare war on Greece. In this order: use Strike wing (Bomber) "
            "to ATTACK Strike target at (3,0), then use Sweep wing for AIR_SWEEP at (3,0), then Guided strike "
            "to ATTACK Missile target at (3,1). Interception should affect the bomber and sweep. Keep Forward "
            "observer in place. Turn 1, after restart: verify Carrier wing remains aboard at (0,-2), then use "
            "that Fighter to attack a visible Greek target at (3,0) or (3,1) if offered. Do not move Carrier. "
            "Turn 2: REBASE Carrier wing back to Antium at (-1,0) and verify transported=false. Leave the other "
            "milestone units in place after their objectives. Do not repeat completed upgrades or disband another plane."
        ),
        "Greece": (
            "Keep both Interceptor fighters based at Athens (7,0), unused or Skip them to preserve automatic "
            "interception. Inspect reports of Rome's bomber, sweep and later carrier-based strike. Keep Strike "
            "target at (3,0), Missile target at (3,1), and Nuclear target at Sparta (6,6) in place. Do not buy "
            "replacements or attack the Roman observer or carrier. France will launch one nuclear missile at "
            "Sparta on turn 1 as the guided milestone; discuss terms if useful. Inspect the nuclear reports, "
            "war state, city damage/population and fallout on turn 2. Keep both cities and ordinary production."
        ),
        "France": (
            "Turn 0: use PARADROP with Drop team from Paris (0,8) to the visible land at (-3,6). Keep Drop "
            "observer in place. Preview Nuclear trial (Nuclear Missile) at Sparta (6,6), but retain it through "
            "the planned restart. Turn 1: read a fresh mission_preview and launch Nuclear trial at (6,6) with "
            "NUCLEAR_STRIKE and confirmAction=true. This native action will declare war on Greece and consume "
            "the missile. Inspect receipt.mission.resolution, newWars and unit removal. Do not separately "
            "declare war first, launch elsewhere, or buy another missile. Turn 2: inspect outcomes and end turn."
        ),
    }
    return common + roles[civilization]


def main() -> int:
    parser = scheduler.parser()
    parser.add_argument("--port", type=int, required=True, help="Host port; the next three ports serve the players")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    root = args.state_dir.expanduser().resolve()
    initial = json.loads((root / "initial-game.json").read_text())
    assert initial["tileMap"]["mapParameters"]["name"] == "Air warfare acceptance"
    if args.model not in (None, MODEL) or args.reasoning_effort not in (None, EFFORT):
        parser.error(f"This exercise uses {MODEL} with {EFFORT} reasoning for every agent")
    args.model, args.reasoning_effort = MODEL, EFFORT
    args.action = "resume" if args.resume else "start"
    if not args.resume:
        assert not read_record(path_for(root), "state", "run") is not None, "Use --resume for an existing live run"
        ensure_players(root / "players.json", False, PLAYERS)
    os.umask(0o077)
    base_prompt = scheduler.player_prompt

    def guided_prompt(civilization, game_turn=None):
        return base_prompt(civilization, game_turn) + "\n\n" + objective(civilization, game_turn)

    scheduler.player_prompt = guided_prompt
    (root / "air-live-prompts.json").write_text(
        json.dumps({name: {str(turn): guided_prompt(name, turn) for turn in range(3)} for name in PLAYERS}, indent=2)
        + "\n"
    )
    processes = []
    logs = []
    executable = str(Path(sys.executable).with_name("civ-agent"))

    def start(arguments: list[str], label: str, ready: str):
        path = root / f"{label}.log"
        log = path.open("a")
        logs.append(log)
        offset = path.stat().st_size
        process = subprocess.Popen([executable, *arguments, "--state-dir", str(root)], stdout=log, stderr=log)
        processes.append(process)
        deadline = time.monotonic() + 180
        while not any(line.startswith(ready) for line in path.read_text()[offset:].splitlines()):
            if process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError(f"{label} failed to become ready; inspect {path}")
            time.sleep(0.25)
        print(f"Ready: {label}", flush=True)

    try:
        start(["host", "start", "--port", str(args.port)], "host", "MCP-READY:")
        for index, name in enumerate(PLAYERS, 1):
            start(
                [
                    "agent-server",
                    "start",
                    name,
                    "--port",
                    str(args.port + index),
                    "--model",
                    MODEL,
                    "--reasoning-effort",
                    EFFORT,
                ],
                name,
                "AGENT-READY:",
            )
        result = scheduler.run(args)
        checkpoint = json.loads((root / "exploration.json").read_text())
        run = read_record(path_for(root), "state", "run") or {}
        if result == 0 and run["limits"]["maxTurns"] == 1 and checkpoint["handoff"] == 3 and checkpoint["paused"]:
            if not (root / "run-before-restart.json").exists():
                write_private_json(root / "run-before-restart.json", run)
            for source, destination in (("exploration.json", "before-restart.json"),):
                if not (root / destination).exists():
                    (root / destination).write_bytes((root / source).read_bytes())
        return result
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        for log in logs:
            log.close()


if __name__ == "__main__":
    raise SystemExit(main())
