"""Opt-in ordinary Ancient-start match. Own services and cleanup; issue no gameplay commands."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from civ_agent import scheduler

MODEL = "gpt-5.6-luna"
EFFORT = "medium"
PLAYERS = ("Rome", "Greece", "France")


def main() -> int:
    parser = scheduler.parser()
    parser.add_argument("--port", type=int, required=True, help="Host port; next three ports serve agents")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--seed", type=int, default=20260920)
    args = parser.parse_args()
    if args.model not in (None, MODEL) or args.reasoning_effort not in (None, EFFORT):
        parser.error(f"All model jobs must use {MODEL} with {EFFORT} reasoning")
    args.model, args.reasoning_effort = MODEL, EFFORT
    args.action = "resume" if args.resume else "start"
    root = args.state_dir.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not args.resume and (root / "exploration.json").exists():
        parser.error("Existing match: use --resume")
    if args.resume:
        checkpoint = json.loads((root / "exploration.json").read_text())
        game = json.loads(checkpoint["gameJson"])
        majors = {
            civ["civID"]: civ.get("playerType", "AI")
            for civ in game["civilizations"]
            if civ.get("civID") in (*PLAYERS, "Egypt")
        }
        if majors != {**dict.fromkeys(PLAYERS, "Human"), "Egypt": "AI"}:
            parser.error("Checkpoint is not the Rome/Greece/France plus native Egypt match")
    executable = str(Path(sys.executable).with_name("civ-agent"))
    processes: list[subprocess.Popen] = []
    logs = []
    os.umask(0o077)

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
        host = ["host", "start", "--port", str(args.port)]
        if not args.resume:
            host += ["--new", "--built-in-ai", "Egypt", "--city-states", "2", "--seed", str(args.seed)]
        start(host, "host", "MCP-READY:")
        for offset, civilization in enumerate(PLAYERS, 1):
            start(
                [
                    "agent-server",
                    "start",
                    civilization,
                    "--port",
                    str(args.port + offset),
                    "--model",
                    MODEL,
                    "--reasoning-effort",
                    EFFORT,
                ],
                civilization,
                "AGENT-READY:",
            )
        return scheduler.run(args)
    finally:
        # Agents finish flushing before the host closes its durable checkpoint.
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
