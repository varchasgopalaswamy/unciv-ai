"""Opt-in ordinary Ancient-start match. Own services and cleanup; issue no gameplay commands."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from civ_agent import scheduler
from civ_agent.agent_settings import resolve_run_settings
from civ_agent.commands.host import ensure_players
from civ_agent.tls import write_private_json

DEFAULT_MODEL = "gpt-6-luna"
DEFAULT_EFFORT = "medium"
PLAYERS = ("Rome", "Greece", "France")


def source_fingerprint(repository: Path) -> str:
    """Include new source files, which a plain git diff omits before staging."""
    names = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "src"], cwd=repository
    ).split(b"\0")
    digest = hashlib.sha256()
    for name in sorted(set(names) - {b""}):
        path = repository / os.fsdecode(name)
        if path.is_file():
            digest.update(name + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def main() -> int:
    parser = scheduler.parser()
    parser.add_argument("--port", type=int, required=True, help="Host port; next three ports serve agents")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--resume", action="store_true")
    mode.add_argument("--checkpoint", type=Path, help="Start a fresh run from a saved host/round checkpoint")
    mode.add_argument("--position", type=Path, help="Prepared strategic position directory, with fresh private memory")
    parser.add_argument("--source-state", type=Path, help="Original state directory providing checkpoint credentials")
    parser.add_argument("--seed", type=int, default=20260920)
    parser.add_argument("--fast", action="store_true", help="Request Codex fast priority for all agent work")
    parser.add_argument(
        "--strategy-guide",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Give all agents optional Civ V strategy suggestions (default: off)",
    )
    parser.add_argument(
        "--startup-timeout", type=scheduler.positive, default=180, help="Seconds allowed per service startup"
    )
    args = parser.parse_args()
    if bool(args.checkpoint) != bool(args.source_state):
        parser.error("--checkpoint and --source-state must be supplied together")
    args.action = "resume" if args.resume else "start"
    players = PLAYERS
    preflight = None
    root = args.state_dir.expanduser().resolve()
    try:
        saved = json.loads((root / "runner/run.json").read_text()) if args.resume else None
        args.model, args.reasoning_effort = resolve_run_settings(
            args.model, args.reasoning_effort, defaults=(DEFAULT_MODEL, DEFAULT_EFFORT), saved=saved
        )
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not args.resume and (root / "exploration.json").exists():
        parser.error("Existing match: use --resume")
    if (args.checkpoint or args.position) and any(root.iterdir()):
        parser.error("A checkpoint comparison requires an empty state directory")
    if args.position:
        checkpoint = json.loads((args.position / "exploration.json").read_text())
        preflight = json.loads((args.position / "preflight.json").read_text())
        game = json.loads(checkpoint["gameJson"])
        humans = [c["civID"] for c in game["civilizations"] if c.get("playerType") == "Human"]
        if humans != [preflight["civilization"]] or game["currentPlayer"] != humans[0]:
            parser.error("Position must start with its single evaluated human player")
        if checkpoint["communication"].get("jobs") or checkpoint.get("handoff", 0):
            parser.error("Position must have a fresh host ledger")
        players = tuple(humans)
        write_private_json(root / "exploration.json", checkpoint)
        write_private_json(root / "initial-game.json", game)
        write_private_json(root / "position.json", preflight)
        ensure_players(root / "players.json", False, players)
    position_resume = args.resume and (root / "position.json").exists()
    if position_resume:
        players = (json.loads((root / "position.json").read_text())["civilization"],)
    if args.resume or args.checkpoint:
        source = json.loads((args.checkpoint or root / "exploration.json").read_text())
        checkpoint = source.get("host", source)
        game = json.loads(checkpoint["gameJson"])
        majors = {
            civ["civID"]: civ.get("playerType", "AI")
            for civ in game["civilizations"]
            if civ.get("civID") in (*PLAYERS, "Egypt")
        }
        expected = {**dict.fromkeys(PLAYERS, "Human"), "Egypt": "AI"}
        if position_resume:
            expected = {name: "Human" if name in players else "AI" for name in (*PLAYERS, "Egypt")}
        if majors != expected:
            parser.error("Checkpoint is not the Rome/Greece/France plus native Egypt match")
        if args.checkpoint:
            if checkpoint["communication"].get("jobs") or checkpoint.get("handoff", 0):
                parser.error("A fresh comparison requires an opening checkpoint without previous jobs or handoffs")
            write_private_json(root / "exploration.json", checkpoint)
            for name in ("players.json", "operator.json"):
                shutil.copyfile(args.source_state / name, root / name)
                (root / name).chmod(0o600)
            repository = Path(__file__).resolve().parents[1]
            write_private_json(
                root / "live-run.json",
                {
                    "checkpointSha256": hashlib.sha256(json.dumps(checkpoint, sort_keys=True).encode()).hexdigest(),
                    "sourceState": str(args.source_state.resolve()),
                    "sourceCheckpoint": str(args.checkpoint.resolve()),
                    "model": args.model,
                    "reasoningEffort": args.reasoning_effort,
                    "strategyGuide": args.strategy_guide,
                    "requestedServiceTier": "fast" if args.fast else None,
                    "serviceRevision": subprocess.check_output(
                        ["git", "rev-parse", "HEAD"], cwd=repository, text=True
                    ).strip(),
                    "serviceSourceModified": bool(
                        subprocess.check_output(
                            ["git", "status", "--porcelain", "--", "src/civ_agent"], cwd=repository, text=True
                        ).strip()
                    ),
                    "engineRevision": subprocess.check_output(
                        ["git", "rev-parse", "HEAD"], cwd=repository / "vendor/Unciv", text=True
                    ).strip(),
                },
            )
    if args.position:
        repository = Path(__file__).resolve().parents[1]
        assert preflight is not None
        write_private_json(
            root / "live-run.json",
            {
                "sourcePosition": str(args.position.resolve()),
                "position": preflight,
                "checkpointSha256": hashlib.sha256((args.position / "exploration.json").read_bytes()).hexdigest(),
                "model": args.model,
                "reasoningEffort": args.reasoning_effort,
                "strategyGuide": args.strategy_guide,
                "requestedServiceTier": "fast" if args.fast else None,
                "serviceRevision": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=repository, text=True
                ).strip(),
                "sourceTreeSha256": source_fingerprint(repository),
                "engineRevision": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=repository / "vendor/Unciv", text=True
                ).strip(),
            },
        )
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
        deadline = time.monotonic() + args.startup_timeout
        while not any(line.startswith(ready) for line in path.read_text()[offset:].splitlines()):
            if process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError(f"{label} failed to become ready; inspect {path}")
            time.sleep(0.25)
        print(f"Ready: {label}", flush=True)

    try:
        host = ["host", "start", "--port", str(args.port)]
        if not args.resume and not args.checkpoint and not args.position:
            host += ["--new", "--built-in-ai", "Egypt", "--city-states", "2", "--seed", str(args.seed)]
        start(host, "host", "MCP-READY:")
        for offset, civilization in enumerate(players, 1):
            start(
                [
                    "agent-server",
                    "start",
                    civilization,
                    "--port",
                    str(args.port + offset),
                    "--model",
                    args.model,
                    "--reasoning-effort",
                    args.reasoning_effort,
                    *(["--strategy-guide"] if args.strategy_guide else []),
                    *(["--fast"] if args.fast else []),
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
