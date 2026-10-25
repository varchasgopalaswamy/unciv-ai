"""Run bounded economy/military acceptance exercises with a restart between stages."""

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from civ_agent.checkout import repository_root
from civ_agent.commands.verify_manager_scenario import verify
from civ_agent.tls import write_private_json


def parser(*, add_help=True):
    result = argparse.ArgumentParser(add_help=add_help)
    result.add_argument("--state-dir", type=Path, required=True, help="Fresh parent directory for the controlled games")
    result.add_argument("--case", choices=("both", "economy", "war"), default="both")
    result.add_argument("--model", help="Override the models for both agent players and their managers")
    result.add_argument("--image", help="Use an explicit freshly built runtime image")
    result.add_argument("--repo-dir", type=Path)
    result.add_argument(
        "--dry-run", action="store_true", help="Print the exact launch sequence without starting models"
    )
    return result


def commands(root, variant, repo, model=None, image=None):
    directory = root / variant
    # Each stage deliberately uses the same pinned image and durable game state.
    start = [
        "game",
        "start",
        "--config",
        str(repo / "examples" / (variant + "-test.toml")),
        "--state-dir",
        str(directory),
    ]
    if model:
        start += ["--model", model]
    if image:
        start += ["--image", image]
    stop = ["game", "stop", "--state-dir", str(directory)]
    return [
        start,
        stop,
        ["match", "verify-manager-scenario", "--state-dir", str(directory), "--save-baseline"],
        ["game", "resume", "--state-dir", str(directory), "--max-turns", "6"],
        stop,
        ["match", "verify-manager-scenario", "--state-dir", str(directory)],
    ]


def wait_boundary(directory, rounds, timeout=8100, *, not_before=0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        path = directory / "container-status.json"
        status = json.loads(path.read_text()) if path.exists() else {}
        if status.get("updated_at", 0) >= not_before and status.get("status") in {
            "failed",
            "paused",
            "completed",
            "budget_exhausted",
        }:
            saved = directory / "exploration.json"
            turn = json.loads(json.loads(saved.read_text())["gameJson"]).get("turns", 0) if saved.exists() else -1
            if status["status"] != "budget_exhausted" or turn != rounds:
                reason = status.get("reason") or status.get("failure") or "No failure reason was recorded."
                raise RuntimeError(
                    f"Run stopped at round {turn}, status={status['status']}: {reason}; inspect {directory / 'launches'}"
                )
            return
        time.sleep(2)
    raise TimeoutError("Controlled exercise did not reach its requested round boundary")


def run(args):
    root = args.state_dir.expanduser().resolve()
    variants = (
        ["economy-manager", "military-manager"]
        if args.case == "both"
        else ["economy-manager" if args.case == "economy" else "military-manager"]
    )
    repo = repository_root(args.repo_dir)
    if any((root / variant).exists() for variant in variants):
        print("Choose a fresh test directory; existing exercise state is never replaced.")
        return 1
    sequences = {variant: commands(root, variant, repo, args.model, args.image) for variant in variants}
    if args.dry_run:
        print(json.dumps({"commands": sequences, "roundBoundaries": [2, 6]}, indent=2))
        return 0
    summary = root / "manager-tests.json"
    reports = json.loads(summary.read_text()) if summary.exists() else {}
    for variant, sequence in sequences.items():
        directory = root / variant
        launched = False
        try:
            for index, command in enumerate(sequence):
                print(f"[{variant}] {' '.join(command)}", flush=True)
                launch_time = time.time()
                if index in {0, 3}:
                    launched = True
                subprocess.run([sys.executable, "-m", "civ_agent", *command], check=True)
                if index in {0, 3}:
                    wait_boundary(directory, 2 if index == 0 else 6, not_before=launch_time)
                elif index in {1, 4}:
                    launched = False
            reports[variant] = verify(directory)
        except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
            report_path = directory / "verification" / "manager-scenario.json"
            previous = json.loads(report_path.read_text()) if report_path.exists() else {}
            try:
                # A first-stage report predates actions in the resumed game.
                # Keep the failure summary grounded in the current checkpoint.
                previous = verify(directory)
            except (OSError, ValueError, KeyError, sqlite3.Error):
                pass
            reports[variant] = {
                **previous,
                "verified": False,
                "status": "FAILED",
                "error": str(error),
                "stateDirectory": str(directory),
            }
            write_private_json(report_path, reports[variant])
        finally:
            if launched:
                subprocess.run(
                    [sys.executable, "-m", "civ_agent", "game", "stop", "--state-dir", str(directory)], check=False
                )
            write_private_json(summary, reports)
    print(json.dumps(reports, indent=2))
    return 0 if all(reports[variant]["verified"] for variant in variants) else 2
