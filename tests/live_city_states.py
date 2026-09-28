"""Opt-in guided city-state game, using the ordinary scheduler and three real model services.

First generate a fresh checkpoint with generateCityStateLiveFixture. This wrapper adds
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

MODEL = "gpt-5.6-luna"
EFFORT = "medium"
PLAYERS = ("Rome", "Greece", "France")


def objective(civilization: str, turn: int | None) -> str:
    common = (
        "This is a guided city-state acceptance exercise. Stay at peace with all civilizations. "
        "Use player_details(section=diplomacy) for cityStates and native actions. CITY_STATE_ACTION uses "
        "target=civilizationId, selection=an available actions.name and confirmAction=true when confirmation is present. "
        "Use unit_details before unit gifts or Great Merchant abilities. Read receipt.cityState for actual effects. "
        "Complete the milestones during turn 0. Later turns verify persistence, influence decay, quest rewards and "
        "notifications; discuss consequences briefly if useful, then end the turn. Do not repeatedly buy influence. "
        "Resolve ordinary choices, keep production and research running, and use reviewed END_TURN. "
        "Your diplomacy with the other major players may use structured chat; negotiate your own social terms, "
        "which do not override native city-state mechanics. Keep messages brief. "
    )
    roles = {
        "Rome": (
            "On your first turn, give Geneva enough Gold to complete its Give Gold quest and become its ally. "
            "Then gift its offered Gems improvement, gift your prepared Warrior in Geneva territory using GIFT_UNIT, "
            "and conduct your prepared Great Merchant's native trade mission using ACTIVATE_GREAT_PERSON. "
            "Finally pledge protection to Geneva. You may inform Greece about your investment, but Greece is allowed "
            "to compete for the alliance. On later turns inspect any lost-alliance notification and protection cooldown; "
            "do not undo or repeat completed milestones."
        ),
        "Greece": (
            "On your first turn, compete with Rome for Geneva: inspect its current ally/influence and give enough "
            "Gold to become Geneva's ally. Select the native gift amounts yourself; re-read after each gift. "
            "Also pledge protection to Sidon to pursue its Pledge to Protect quest. On later turns inspect the quest "
            "reward and any French bullying of Sidon. Respond to native diplomatic alerts through chat_send.nativeAction "
            "when offered; you choose your response and any social terms. Stay at peace."
        ),
        "France": (
            "On your first turn, demand Gold tribute from Sidon using its offered native goldTribute action. "
            "Your prepared force should make this possible; do not attack or reposition it. Read the influence loss "
            "and report your position to Greece if asked. On later turns inspect the tribute cooldown and diplomacy; "
            "respond to any messages without starting war. Do not repeat tribute during its cooldown."
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
    assert initial["tileMap"]["mapParameters"]["name"] == "City-state live acceptance"
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
    (root / "city-state-live-prompts.json").write_text(
        json.dumps({name: {str(turn): guided_prompt(name, turn) for turn in (0, 1)} for name in PLAYERS}, indent=2)
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
        return scheduler.run(args)
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
