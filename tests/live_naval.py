"""Opt-in guided naval game, using the ordinary scheduler and three real model services.

First generate a fresh checkpoint with generateNavalLiveFixture. This wrapper adds
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

MODEL = "gpt-5.6-luna"
EFFORT = "medium"
PLAYERS = ("Rome", "Greece", "France")


def objective(civilization: str, turn: int | None) -> str:
    common = (
        "This is a guided Vanilla naval acceptance exercise with Rome, Greece and France. "
        "Choose destinations, never tile-by-tile paths: use movement_preview(target,destination), then "
        "SET_UNIT_DESTINATION with the same target/destination and intent. Inspect the returned turn paths first. "
        "Orders continue automatically at the start of your turns, survive restart and clear on arrival. "
        "Inspect movementOrder and actual positions before acting again; CANCEL_UNIT_MOVEMENT stops an order. "
        "CREATE_WATER_IMPROVEMENT uses an available unit_details.waterImprovement name and consumes its Work Boat. "
        "Read full unit details before naval attacks, upgrades, healing or promotions and inspect actual receipts. "
        "Resolve ordinary choices, keep production and research running, and use reviewed END_TURN. "
        "This is a limited Rome-Greece naval conflict. Negotiate your own social terms through structured chat "
        "if useful; trade or native peace treaties use the trade negotiation flow. France remains neutral. "
        "Milestones have priority over open-ended conquest. Do not repeat completed purchases or improvements. "
        "Keep messages brief and end each turn once its achievable milestones are done. "
        "If a turn resumes after interruption, inspect current state and archived receipts, skip completed "
        "milestones and finish the incomplete ones. An already issued replacement order only needs cancellation. "
    )
    roles = {
        "Rome": (
            "On turn 0 create Fishing Boats with Resource crew, upgrade Upgrade ship from Trireme to Caravel, "
            "and send Voyager toward (1,-2) using a multi-turn destination order. You have sufficient Gold and Iron. "
            "Declare war on Greece with the native DECLARE_WAR action, then use Coastal battery (Frigate) to "
            "attack the Greek Defender ship if a legal offer exists. You may discuss limits with Greece. "
            "On turn 1 verify Voyager arrived automatically. Preview and issue a new distant destination (8,-3), "
            "then cancel that new order using CANCEL_UNIT_MOVEMENT and verify it stopped. Have Coastal battery "
            "bombard the Greek capital using an offered ATTACK; do not attempt to capture it with this ranged ship. "
            "On later turns inspect outcomes and use native healing/promotions if useful. Do not move Voyager "
            "again after the cancellation milestone."
        ),
        "Greece": (
            "On turn 0 improve Fish with Greek resource crew through CREATE_WATER_IMPROVEMENT. "
            "Your Defender ship may retaliate against Rome's Frigate if an attack is offered; read the damage "
            "preview and decide whether to attack or seek terms. On later turns use sleep-until-healed or an "
            "earned promotion if available, otherwise inspect the war and end your turn. Discuss your own social "
            "limits with Rome; do not prevent its one coastal-bombardment milestone before turn 2."
        ),
        "France": (
            "Stay neutral. On turn 0 inspect the overseas island at (-4,7) and send Overseas settlers there "
            "using one SET_UNIT_DESTINATION order. Let native embarkation, sailing and landing continue on "
            "later turns. Inspect its position each turn; found a city there as soon as canFoundCity is true. "
            "Landing may consume all movement, in which case founding waits until the following turn. "
            "Keep Survey ship available as an escort; you choose its destination if needed. Do not cancel the "
            "settler order while it is making progress. On later turns verify the new city and select production."
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
    assert initial["tileMap"]["mapParameters"]["name"] == "Naval gameplay acceptance"
    if args.model not in (None, MODEL) or args.reasoning_effort not in (None, EFFORT):
        parser.error(f"This exercise uses {MODEL} with {EFFORT} reasoning for every agent")
    args.model, args.reasoning_effort = MODEL, EFFORT
    args.action = "resume" if args.resume else "start"
    if not args.resume:
        assert not (root / "runner" / "run.json").exists(), "Use --resume for an existing live run"
        ensure_players(root / "players.json", False, PLAYERS)
    os.umask(0o077)
    base_prompt = scheduler.player_prompt

    def guided_prompt(civilization, game_turn=None):
        return base_prompt(civilization, game_turn) + "\n\n" + objective(civilization, game_turn)

    scheduler.player_prompt = guided_prompt
    (root / "naval-live-prompts.json").write_text(
        json.dumps({name: {str(turn): guided_prompt(name, turn) for turn in range(4)} for name in PLAYERS}, indent=2)
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
        run = json.loads((root / "runner" / "run.json").read_text())
        if result == 0 and run["limits"]["maxTurns"] == 1 and checkpoint["handoff"] == 3 and checkpoint["paused"]:
            for source, destination in (
                ("exploration.json", "before-restart.json"),
                ("runner/run.json", "run-before-restart.json"),
            ):
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
