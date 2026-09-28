"""Opt-in saved-position evaluation with normal player objectives and isolated services.

Prepare checkpoints with Gradle's prepareStrategyPositions first. Criteria stay
in the operator manifest; the agents receive only ordinary authorized reads.
"""

import argparse
import concurrent.futures
import json
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from civ_agent.monitoring import path_for, read_collection, read_record
from civ_agent.tls import write_private_json


def evaluation_status(root, expected_rounds, exit_code):
    state = read_record(path_for(root), "state", "run") or {}
    turns = {row["turnId"]: row for row in read_collection(path_for(root), "turns")}
    completed = sum(row["turn"]["status"] == "completed" and row["accountingComplete"] for row in turns.values())
    return {
        "runnerStatus": state.get("status"),
        "reason": state.get("reason"),
        "completedRounds": completed,
        "expectedRounds": expected_rounds,
        "completedEvaluation": exit_code == 0 and (completed >= expected_rounds or state.get("status") == "complete"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--port", required=True, type=int, help="First host port; reserve four ports per position")
    parser.add_argument("--case", action="append", help="Run selected position IDs; default: all")
    parser.add_argument("--parallel", type=int, choices=(1, 2), default=2)
    parser.add_argument("--model", default="gpt-6-luna")
    parser.add_argument("--reasoning-effort", default="high")
    parser.add_argument("--fast", action="store_true")
    parser.add_argument("--strategy-guide", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()
    manifest = json.loads((Path(__file__).parent / "fixtures/strategy_positions/manifest.json").read_text())
    selected = set(args.case or [case["id"] for case in manifest["positions"]])
    cases = [case for case in manifest["positions"] if case["id"] in selected]
    if selected != {case["id"] for case in cases}:
        parser.error("Unknown position ID")
    root = args.state_dir.resolve()
    if root.exists() and any(root.iterdir()):
        parser.error("Use a fresh suite directory; failed attempts are retained")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    processes = []
    lock = threading.Lock()
    stopping = threading.Event()
    results = []
    metadata = {
        "model": args.model,
        "reasoningEffort": args.reasoning_effort,
        "strategyGuide": args.strategy_guide,
        "fast": args.fast,
        "freshPrivateMemory": True,
        "positions": cases,
    }
    write_private_json(root / "suite.json", metadata)

    def run_case(index, case):
        command = [
            sys.executable,
            "-m",
            "tests.live_saved_position",
            "--position",
            str(args.prepared.resolve() / case["id"]),
            "--state-dir",
            str(root / case["id"]),
            "--port",
            str(args.port + 4 * index),
            "--model",
            args.model,
            "--reasoning-effort",
            args.reasoning_effort,
            "--max-turns",
            str(case["rounds"]),
            "--max-invocations",
            "12",
            "--max-seconds",
            "3600",
            "--turn-timeout",
            "1200",
            "--strategy-guide" if args.strategy_guide else "--no-strategy-guide",
            *(["--fast"] if args.fast else []),
        ]
        started = time.monotonic()
        print(f"Starting {case['id']} ({case['rounds']} rounds)", flush=True)
        with (root / f"{case['id']}.log").open("w") as log:
            with lock:
                if stopping.is_set():
                    return {"id": case["id"], "completedEvaluation": False, "reason": "Suite interrupted before launch"}
                process = subprocess.Popen(command, stdout=log, stderr=log)
                processes.append(process)
            code = process.wait()
        result = {
            "id": case["id"],
            "exitCode": code,
            "elapsedSeconds": time.monotonic() - started,
            **evaluation_status(root / case["id"], case["rounds"], code),
        }
        print(f"Finished {case['id']}: exit {code}", flush=True)
        return result

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=args.parallel)
    futures = []
    try:
        futures = [pool.submit(run_case, index, case) for index, case in enumerate(cases)]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
            write_private_json(root / "results.json", results)
    finally:
        # SIGINT lets each driver's finally block close its agents and host.
        stopping.set()
        for future in futures:
            future.cancel()
        with lock:
            for process in processes:
                if process.poll() is None:
                    process.send_signal(signal.SIGINT)
        pool.shutdown(wait=True, cancel_futures=True)
    return 0 if all(result["completedEvaluation"] for result in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
