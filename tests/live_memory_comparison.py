"""Opt-in paired memory evaluation. Every trial starts from the same saved board.

The baseline is an operator-supplied source snapshot, not a production
compatibility mode. Knowledge-only starts without a strategy or handoff; agents
may write new assessments normally. Failed trials stop the suite for inspection.
"""

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from civ_agent.tls import write_private_json
from tests.live_strategy_positions import evaluation_status

VARIANTS = ("baseline", "knowledge", "revised")


def tree_hash(root):
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--prepared", required=True, type=Path)
    parser.add_argument("--baseline-source", type=Path, help="Directory containing the old civ_agent package")
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--case", action="append")
    parser.add_argument("--variant", choices=VARIANTS, action="append")
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--model", default="gpt-6-luna")
    parser.add_argument("--reasoning-effort", default="max")
    parser.add_argument("--fast", action="store_true")
    parser.add_argument("--strategy-guide", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()
    manifest_path = args.manifest.resolve()
    manifest = json.loads(manifest_path.read_text())
    cases = [case for case in manifest["positions"] if not args.case or case["id"] in args.case]
    if not cases or args.case and set(args.case) != {case["id"] for case in cases}:
        parser.error("Select existing position IDs")
    variants = args.variant or list(VARIANTS)
    if args.repetitions < 1:
        parser.error("--repetitions must be positive")
    if "baseline" in variants and (not args.baseline_source or not (args.baseline_source / "civ_agent").is_dir()):
        parser.error("The baseline needs its preserved source snapshot")
    root = args.state_dir.resolve()
    if root.exists() and any(root.iterdir()):
        parser.error("Use an empty suite directory; previous trials are retained")
    for case in cases:
        if not (args.prepared / case["id"] / "exploration.json").is_file():
            parser.error(f"Missing prepared position: {case['id']}")
        for variant in variants:
            seed = manifest_path.parent / case["seeds"][variant]
            if not seed.is_file():
                parser.error(f"Missing memory seed: {seed}")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    sources = {"current": Path(__file__).resolve().parents[1] / "src"}
    if args.baseline_source:
        sources["baseline"] = args.baseline_source.resolve()
    write_private_json(
        root / "suite.json",
        {
            "manifest": manifest,
            "manifestSha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "sources": {key: {"path": str(path), "sha256": tree_hash(path)} for key, path in sources.items()},
            "model": args.model,
            "reasoningEffort": args.reasoning_effort,
            "fast": args.fast,
            "strategyGuide": args.strategy_guide,
            "repetitions": args.repetitions,
            "variants": variants,
            "knowledgeControl": "Initial knowledge only; subsequent memory writes use the revised API normally",
        },
    )
    results = []
    for repetition in range(args.repetitions):
        # Rotate order between repetitions to reduce systematic cache/order effects.
        order = variants[repetition % len(variants) :] + variants[: repetition % len(variants)]
        for case in cases:
            for variant in order:
                label = f"{case['id']}-{variant}-{repetition + 1}"
                directory = root / label
                command = [
                    sys.executable,
                    "-m",
                    "tests.live_saved_position",
                    "--position",
                    str(args.prepared.resolve() / case["id"]),
                    "--memory-seed",
                    str(manifest_path.parent / case["seeds"][variant]),
                    "--state-dir",
                    str(directory),
                    "--port",
                    str(args.port),
                    "--model",
                    args.model,
                    "--reasoning-effort",
                    args.reasoning_effort,
                    "--max-turns",
                    str(case["rounds"]),
                    "--max-invocations",
                    "30",
                    "--startup-timeout",
                    "900",
                    "--turn-timeout",
                    "1200",
                    "--max-seconds",
                    "7200",
                    "--strategy-guide" if args.strategy_guide else "--no-strategy-guide",
                    *(["--fast"] if args.fast else []),
                ]
                source = sources["baseline" if variant == "baseline" else "current"]
                environment = {**os.environ, "PYTHONPATH": str(source)}
                print(f"Starting {label}", flush=True)
                started = time.monotonic()
                with (root / f"{label}.log").open("w") as log:
                    process = subprocess.Popen(command, env=environment, stdout=log, stderr=log)
                    try:
                        code = process.wait()
                    finally:
                        if process.poll() is None:
                            process.send_signal(signal.SIGINT)
                            process.wait(timeout=90)
                result = {
                    "id": label,
                    "case": case["id"],
                    "variant": variant,
                    "repetition": repetition + 1,
                    "exitCode": code,
                    "elapsedSeconds": time.monotonic() - started,
                    **evaluation_status(directory, case["rounds"], code),
                }
                results.append(result)
                write_private_json(root / "results.json", results)
                print(f"Finished {label}: {result['completedRounds']}/{case['rounds']} rounds", flush=True)
                if not result["completedEvaluation"]:
                    print(f"Stopped for inspection: {directory}", flush=True)
                    return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
