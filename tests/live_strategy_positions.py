"""Opt-in saved-position evaluation with normal player objectives and isolated services.

Prepare checkpoints with Gradle's prepareStrategyPositions first. Criteria stay
in the operator manifest; the agents receive only ordinary authorized reads.
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from civ_agent.monitoring import path_for, read_collection, read_record
from civ_agent.scheduler import positive
from civ_agent.tls import write_private_json
from tests.evaluation_sources import tree_hash


def trial_schedule(cases, variants, repetitions):
    """Rotate implementation order, retaining every attempt under a unique label."""
    return [
        {
            "case": case,
            "variant": variant,
            "repetition": repetition + 1,
            "id": case["id"] if len(variants) == repetitions == 1 else f"{case['id']}-{variant}-{repetition + 1}",
        }
        for repetition in range(repetitions)
        for case in cases
        for variant in variants[repetition % len(variants) :] + variants[: repetition % len(variants)]
    ]


def validate_case(case, position):
    """Check diagnostic selection assumptions without changing the player's objective."""
    if case.get("requiresScienceConversion") and not any(
        city.get("constructionQueue", [])[:1] == ["Science"] for city in position.get("cities", [])
    ):
        raise ValueError(f"{case['id']}: expected an active Science conversion")
    minimum = case.get("minimumCulturePerTurn")
    if minimum is not None and float((position.get("culture") or {}).get("culturePerTurn", 0)) < minimum:
        raise ValueError(f"{case['id']}: cultural specialization is missing")


def freeze_memory_seeds(cases, manifest_path, root):
    """Keep one operator seed snapshot per case, shared by every source variant."""
    seeds = {}
    for case in cases:
        if not case.get("memorySeed"):
            continue
        source = (manifest_path.resolve().parent / case["memorySeed"]).resolve()
        raw = source.read_bytes()
        payload = json.loads(raw)
        if payload.get("civilization") != case["civilization"]:
            raise ValueError(f"{case['id']}: memory seed belongs to another civilization")
        digest = hashlib.sha256(raw).hexdigest()
        destination = root / "memory-seeds" / f"{digest}.json"
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.write_bytes(raw)
        destination.chmod(0o600)
        seeds[case["id"]] = {"source": str(source), "path": str(destination), "sha256": digest}
    return seeds


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


def provider_usage_exhausted(root):
    """Use structured provider status, never infer an account limit from prose."""
    state = read_record(path_for(root), "state", "run") or {}
    statuses = [(state.get("inFlight") or {}).get("modelStatus") or {}]
    for path in root.glob("agent-services/*/activations/*.json"):
        record = json.loads(path.read_text())
        if record.get("phase") == "FAILED":
            statuses.append(record.get("modelStatus") or {})
            statuses.append((record.get("reviewProgress") or {}).get("modelStatus") or {})
    return any(status.get("state") == "failed" and status.get("reason") == "usageLimitExceeded" for status in statuses)


def reported_model_tokens(root):
    """Count each job once, including reviews, chat, failures and cached input."""
    return sum(
        (row["job"].get("usage") or {}).get(key, 0)
        for row in read_collection(path_for(root), "jobs")
        for key in ("input_tokens", "output_tokens")
    )


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
    parser.add_argument("--civilization-guide", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--review-interval", type=positive, default=5)
    parser.add_argument("--rounds", type=positive, help="Override case length, e.g. 5 to screen or 25 for outcomes")
    parser.add_argument("--repetitions", type=positive, default=1)
    parser.add_argument(
        "--skip-trial", action="append", default=[], help="Previously completed trial ID to retain elsewhere"
    )
    parser.add_argument(
        "--variant", action="append", help="NAME=SOURCE_ROOT containing civ_agent; default: current checkout"
    )
    parser.add_argument("--max-invocations", type=positive, default=60)
    parser.add_argument(
        "--max-model-requests",
        type=positive,
        help="Hard shared limit of Codex provider attempts across this suite, including failed requests and compaction; requires the provider gate in every variant",
    )
    parser.add_argument("--max-seconds", type=positive, default=14400)
    parser.add_argument("--turn-timeout", type=positive, default=1200)
    parser.add_argument(
        "--max-tokens",
        type=positive,
        help="Per-trial input plus output tokens, including cached input; checked every two seconds",
    )
    parser.add_argument(
        "--manifest", type=Path, default=Path(__file__).parent / "fixtures/strategy_positions/manifest.json"
    )
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    selected = set(args.case or [case["id"] for case in manifest["positions"]])
    cases = [case for case in manifest["positions"] if case["id"] in selected]
    if selected != {case["id"] for case in cases}:
        parser.error("Unknown position ID")
    if args.rounds:
        cases = [{**case, "rounds": args.rounds} for case in cases]
    for case in cases:
        validate_case(case, json.loads((args.prepared / case["id"] / "initial-turn.json").read_text()))
    sources = {}
    for value in args.variant or [f"current={Path(__file__).resolve().parents[1] / 'src'}"]:
        name, separator, path = value.partition("=")
        if (
            not separator
            or not re.fullmatch(r"[a-zA-Z0-9_-]+", name)
            or name in sources
            or not (Path(path) / "civ_agent").is_dir()
        ):
            parser.error("Each variant needs a unique simple name and a source directory containing civ_agent")
        sources[name] = Path(path).resolve()
    trials = trial_schedule(cases, list(sources), args.repetitions)
    if set(args.skip_trial) - {trial["id"] for trial in trials}:
        parser.error("Unknown skipped trial ID")
    trials = [trial for trial in trials if trial["id"] not in args.skip_trial]
    root = args.state_dir.resolve()
    if root.exists() and any(root.iterdir()):
        parser.error("Use a fresh suite directory; failed attempts are retained")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    request_budget = None
    if args.max_model_requests:
        if any(not (source / "civ_agent" / "provider_gate.py").exists() for source in sources.values()):
            parser.error("Every variant must implement the provider gate to enforce --max-model-requests")
        from civ_agent.provider_gate import RequestBudget

        request_budget = RequestBudget(root / "provider-requests.sqlite", args.max_model_requests)
    memory_seeds = freeze_memory_seeds(cases, args.manifest, root)
    processes = []
    lock = threading.Lock()
    stopping = threading.Event()
    stop_reason = "Suite interrupted before launch"
    results = []
    metadata = {
        "model": args.model,
        "reasoningEffort": args.reasoning_effort,
        "strategyGuide": args.strategy_guide,
        "civilizationGuide": args.civilization_guide,
        "reviewInterval": args.review_interval,
        "fast": args.fast,
        "freshPrivateMemory": True,
        "memorySeeds": memory_seeds,
        "positions": cases,
        "manifest": manifest,
        "sources": {
            name: {"path": str(path), "sha256": tree_hash(path / "civ_agent")} for name, path in sources.items()
        },
        "repetitions": args.repetitions,
        "previouslyCompletedTrials": args.skip_trial,
        "limits": {
            "invocations": args.max_invocations,
            "providerRequests": args.max_model_requests,
            "seconds": args.max_seconds,
            "turnSeconds": args.turn_timeout,
            "tokens": args.max_tokens,
            "tokenAccounting": "Reported input plus output across all model jobs; cached input included once",
        },
    }
    write_private_json(root / "suite.json", metadata)

    def run_case(index, trial):
        nonlocal stop_reason
        case, label = trial["case"], trial["id"]
        command = [
            sys.executable,
            "-m",
            "tests.live_saved_position",
            "--position",
            str(args.prepared.resolve() / case["id"]),
            "--state-dir",
            str(root / label),
            "--port",
            str(args.port + 4 * index),
            "--model",
            args.model,
            "--reasoning-effort",
            args.reasoning_effort,
            "--max-turns",
            str(case["rounds"]),
            "--max-invocations",
            str(args.max_invocations),
            "--max-seconds",
            str(args.max_seconds),
            "--turn-timeout",
            str(args.turn_timeout),
            "--strategy-guide" if args.strategy_guide else "--no-strategy-guide",
            "--civilization-guide" if args.civilization_guide else "--no-civilization-guide",
            "--review-interval",
            str(args.review_interval),
            *(["--fast"] if args.fast else []),
        ]
        if case["id"] in memory_seeds:
            command += ["--memory-seed", memory_seeds[case["id"]]["path"]]
        started = time.monotonic()
        environment = {
            **os.environ,
            "PYTHONPATH": str(sources[trial["variant"]]) + os.pathsep + os.environ.get("PYTHONPATH", ""),
        }
        if args.max_model_requests:
            environment.update(
                CIV_MODEL_REQUEST_BUDGET=str(root / "provider-requests.sqlite"),
                CIV_MODEL_REQUEST_LIMIT=str(args.max_model_requests),
            )
        with (root / f"{label}.log").open("w") as log:
            with lock:
                if request_budget is not None and request_budget.used() >= request_budget.limit:
                    stop_reason = "Suite provider request budget exhausted; no further trials launched"
                    stopping.set()
                if stopping.is_set():
                    return {
                        "id": label,
                        "case": case["id"],
                        "variant": trial["variant"],
                        "repetition": trial["repetition"],
                        "completedEvaluation": False,
                        "notStarted": True,
                        "reason": stop_reason,
                    }
                print(f"Starting {label} ({case['rounds']} rounds)", flush=True)
                process = subprocess.Popen(command, stdout=log, stderr=log, env=environment)
                processes.append(process)
            token_stop = None
            while process.poll() is None:
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    if args.max_tokens and token_stop is None:
                        try:
                            total = reported_model_tokens(root / label)
                        except (OSError, ValueError, SQLAlchemyError):
                            continue
                        if total >= args.max_tokens:
                            token_stop = total
                            process.send_signal(signal.SIGINT)
            code = process.returncode
        provider_limit = provider_usage_exhausted(root / label)
        if provider_limit:
            with lock:
                stop_reason = "Suite stopped after provider usageLimitExceeded; no further trials launched"
                stopping.set()
        result = {
            "id": label,
            "case": case["id"],
            "variant": trial["variant"],
            "repetition": trial["repetition"],
            "exitCode": code,
            "elapsedSeconds": time.monotonic() - started,
            **evaluation_status(root / label, case["rounds"], code),
            "tokenBudgetStop": token_stop,
            "providerFailure": "usageLimitExceeded" if provider_limit else None,
            "suiteProviderAttempts": request_budget.used() if request_budget is not None else None,
        }
        print(
            f"Finished {label}: {result['completedRounds']}/{result['expectedRounds']} rounds with complete accounting, "
            f"{result['runnerStatus']}, exit {code}",
            flush=True,
        )
        return result

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=args.parallel)
    futures = []
    try:
        futures = [pool.submit(run_case, index, trial) for index, trial in enumerate(trials)]
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
