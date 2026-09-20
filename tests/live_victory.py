"""Opt-in guided live games, using the ordinary scheduler and three real model services.

First generate fresh checkpoints with generateVictoryLiveFixtures. This wrapper adds
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


def objective(kind: str, civilization: str, turn: int | None) -> str:
    common = (
        "This is a guided late-game acceptance exercise, overriding the generic aim to beat the other players. "
        "Stay at peace, preserve the prepared cities and infrastructure, and resolve ordinary turn decisions. "
        "Use only observed player information. Finish each turn once through the ordinary reviewed END_TURN. "
        "Keep responses concise; do not repeatedly query an unchanged state or wait for production within one turn. "
    )
    if kind == "spaceship":
        if civilization == "Rome":
            role = (
                "Win Scientific victory by building all six spaceship parts from zero production. "
                "Use both Rome and Antium for parallel production, replacing their perpetual Gold queues. "
                "Choose your own build allocation and routes. Read victory_progress for the required parts/counts. "
                "Move every part produced outside Rome to the capital using its advertised legal moves, "
                "then assemble with ADD_SPACESHIP_PART after reading unit_details. "
                "Read city_details for construction choices and progress. Parts use Aluminum while unassembled, "
                "so prioritize moving and assembling completed parts before queuing more. "
                "Keep unfinished spaceship production in progress between turns; do not replace it. "
                "If no action will advance the project this turn, review and end the turn so production can accrue."
            )
        else:
            role = (
                "Support Rome's peaceful space program by maintaining your city and ending your ordinary turns. "
                "Keep the existing perpetual Gold production and Future Tech research when legal. "
                "Do not build your own spaceship, declare war, or open negotiations; this exercise measures Rome's "
                "production, transport and assembly, plus final result delivery to all players."
            )
    else:
        if civilization == "Greece":
            role = "Explicitly ABSTAIN_DIPLOMATIC_VOTE in each election when your ballot is available."
        elif civilization == "Rome":
            role = "CAST_DIPLOMATIC_VOTE for Greece whenever your ballot is available."
        elif turn == 0:
            role = "In the initial election CAST_DIPLOMATIC_VOTE for Rome, to exercise an election without a winner."
        else:
            role = "In the next election CAST_DIPLOMATIC_VOTE for Greece, to exercise a later diplomatic victory."
        role += (
            " Read the available victory.ballot and victory.votingResults. "
            "When results require acknowledgement, read them then ACKNOWLEDGE_DIPLOMATIC_RESULTS. "
            "Keep Gold production and Future Tech research, handle other required choices, and end ordinary turns "
            "until the native election countdown brings the next ballot. Do not build spaceship parts. "
            "Do not assume the election outcome from these objectives; report only the observed result."
        )
    return common + role


def main() -> int:
    parser = scheduler.parser()
    parser.add_argument("--scenario", choices=("spaceship", "election"), required=True)
    parser.add_argument("--port", type=int, required=True, help="Host port; the next three ports serve the players")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    root = args.state_dir.expanduser().resolve()
    initial = json.loads((root / "initial-game.json").read_text())
    assert initial["tileMap"]["mapParameters"]["name"] == f"Victory live acceptance: {args.scenario}"
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
        return base_prompt(civilization, game_turn) + "\n\n" + objective(args.scenario, civilization, game_turn)

    scheduler.player_prompt = guided_prompt
    (root / "victory-live-prompts.json").write_text(
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
