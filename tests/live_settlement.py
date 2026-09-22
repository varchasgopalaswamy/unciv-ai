"""Opt-in settlement exercise: one real model player, two native AI rivals.

Generate a fresh checkpoint with generateSettlementLiveFixture first. The driver
sets the objective and starts ordinary services; it issues no gameplay commands.
"""

import json
import os
import subprocess
import sys
import time

from civ_agent import scheduler
from civ_agent.commands.host import ensure_players
from civ_agent.tls import write_private_json

MODEL = "gpt-5.6-luna"
EFFORT = "medium"
OBJECTIVE = (
    "This is a focused settlement exercise. Found one additional Roman city with the existing Settler. "
    "It starts with a destination order toward an uncertain city site. On the opening turn, survey that destination's "
    "vicinity with the Scout, then decide whether to keep the order or choose another site. "
    "You choose the scouting destination, city site and any replacement orders. "
    "On later turns, complete the remaining settlement work; skip milestones already completed. "
    "Use native destination orders and path previews for travel. Keep research and construction running. "
    "After founding the city, select production and end subsequent turns without pursuing more expansion. "
    "Greece and France use the built-in AI. This exercise does not require diplomacy or war."
)


def main() -> int:
    parser = scheduler.parser()
    parser.add_argument("--port", type=int, required=True, help="Host port; the next port serves Rome")
    parser.add_argument("--resume", action="store_true")
    parser.set_defaults(max_turns=3, max_invocations=12, max_seconds=1800, turn_timeout=600)
    args = parser.parse_args()
    root = args.state_dir.expanduser().resolve()
    initial = json.loads((root / "initial-game.json").read_text())
    if initial["tileMap"]["mapParameters"]["name"] != "Settlement destination acceptance":
        parser.error("Generate a settlement checkpoint first")
    if args.model not in (None, MODEL) or args.reasoning_effort not in (None, EFFORT):
        parser.error(f"This exercise uses {MODEL} with {EFFORT} reasoning")
    args.model, args.reasoning_effort = MODEL, EFFORT
    args.action = "resume" if args.resume else "start"
    if not args.resume:
        if (root / "runner" / "run.json").exists():
            parser.error("Use --resume for an existing run")
        ensure_players(root / "players.json", False, ("Rome",))
    os.umask(0o077)
    base_prompt = scheduler.player_prompt

    def guided_prompt(civilization, game_turn=None):
        return base_prompt(civilization, game_turn) + "\n\n" + OBJECTIVE

    scheduler.player_prompt = guided_prompt
    write_private_json(
        root / "settlement-live.json", {"model": MODEL, "reasoningEffort": EFFORT, "objective": OBJECTIVE}
    )
    processes: list[subprocess.Popen] = []
    logs = []

    def start(arguments: list[str], label: str, ready: str):
        path = root / f"{label}.log"
        log = path.open("a")
        logs.append(log)
        offset = path.stat().st_size
        process = subprocess.Popen(
            [sys.executable, "-m", "civ_agent", *arguments, "--state-dir", str(root)], stdout=log, stderr=log
        )
        processes.append(process)
        deadline = time.monotonic() + 300
        while not any(line.startswith(ready) for line in path.read_text()[offset:].splitlines()):
            if process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError(f"{label} failed to become ready; inspect {path}")
            time.sleep(0.25)
        print(f"Ready: {label}", flush=True)

    try:
        start(["host", "start", "--port", str(args.port)], "host", "MCP-READY:")
        start(
            [
                "agent-server",
                "start",
                "Rome",
                "--port",
                str(args.port + 1),
                "--model",
                MODEL,
                "--reasoning-effort",
                EFFORT,
            ],
            "Rome",
            "AGENT-READY:",
        )
        return scheduler.run(args)
    finally:
        scheduler.player_prompt = base_prompt
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        for log in logs:
            log.close()


if __name__ == "__main__":
    raise SystemExit(main())
