"""Prepare isolated saved-position comparisons using the ordinary launcher services.

For ordinary games and opening benchmarks, use civ-agent game start.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from civ_agent import scheduler
from civ_agent.agent_settings import resolve_run_settings
from civ_agent.commands.host import ensure_players
from civ_agent.tls import write_private_json
from tests.support import services

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
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--checkpoint", type=Path, help="Start a fresh run from a saved host/round checkpoint")
    mode.add_argument("--position", type=Path, help="Prepared strategic position directory, with fresh private memory")
    parser.add_argument("--memory-seed", type=Path, help="Operator memory seed for an isolated --position evaluation")
    parser.add_argument("--source-state", type=Path, help="Original state directory providing checkpoint credentials")
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
    if args.memory_seed and not args.position:
        parser.error("--memory-seed requires a fresh --position evaluation")
    args.action = "start"
    players = PLAYERS
    preflight = None
    root = args.state_dir.expanduser().resolve()
    try:
        args.model, args.reasoning_effort = resolve_run_settings(
            args.model, args.reasoning_effort, defaults=(DEFAULT_MODEL, DEFAULT_EFFORT)
        )
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
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
    if args.checkpoint:
        source = json.loads(args.checkpoint.read_text())
        checkpoint = source.get("host", source)
        game = json.loads(checkpoint["gameJson"])
        majors = {
            civ["civID"]: civ.get("playerType", "AI")
            for civ in game["civilizations"]
            if civ.get("civID") in (*PLAYERS, "Egypt")
        }
        expected = {**dict.fromkeys(PLAYERS, "Human"), "Egypt": "AI"}
        if majors != expected:
            parser.error("Checkpoint is not the Rome/Greece/France plus native Egypt match")
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
    with services(root, args.startup_timeout, lambda label, message: print(f"{label}: {message}", flush=True)) as start:
        host = ["host", "start", "--port", str(args.port)]
        start(host, "host", "MCP-READY:")
        if args.memory_seed:
            from tests.memory_evaluation_seed import seed_memory

            active_checkpoint = json.loads((root / "exploration.json").read_text())
            active_game = json.loads(active_checkpoint["gameJson"])
            seed_memory(root, args.memory_seed, players[0], active_checkpoint["epoch"], active_game["turns"])
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


if __name__ == "__main__":
    raise SystemExit(main())
