"""Opt-in paired role-context screening through the saved-position harness.

Both variants use identical prepared boards and empty initial private memory.
Native AI plays opponents. This runs billed/live model jobs for both models.
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from civ_agent.container_supervisor import authorize_agent
from civ_agent.container_support import locked
from civ_agent.tls import private_directory, write_private_json
from tests.evaluation_sources import tree_hash


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("prepared", "manifest", "baseline-source", "candidate-source", "state-dir", "auth-root"):
        parser.add_argument("--" + field, required=True, type=Path)
    parser.add_argument("--port", type=int, default=21041)
    parser.add_argument(
        "--completed-suite",
        type=Path,
        action="append",
        default=[],
        help="Reuse completed trials from this prior paired suite",
    )
    args = parser.parse_args()
    root = private_directory(args.state_dir.resolve())
    if any(root.iterdir()):
        parser.error("Use a fresh state directory; previous attempts must remain available")
    sources = {"baseline": args.baseline_source.resolve(), "candidate": args.candidate_source.resolve()}
    source_hashes = {name: tree_hash(path / "civ_agent") for name, path in sources.items()}
    prior_suites = [path.resolve() for path in args.completed_suite]
    for previous in prior_suites:
        metadata = json.loads((previous / "commands.json").read_text())
        for inherited in metadata.get("completedSuites", []):
            path = Path(inherited).resolve()
            if path not in prior_suites:
                prior_suites.append(path)
        if {name: source["sha256"] for name, source in metadata["sources"].items()} != source_hashes:
            parser.error("Completed trials must use the same frozen source variants")
    commands = {}
    for index, model in enumerate(("gpt-6-luna", "gpt-6.1-terra")):
        completed = set()
        for previous in prior_suites:
            suite = json.loads((previous / model / "suite.json").read_text())
            expected = {
                "model": model,
                "reasoningEffort": "medium",
                "strategyGuide": True,
                "civilizationGuide": True,
                "reviewInterval": 10,
                "fast": False,
            }
            if any(suite.get(key) != value for key, value in expected.items()):
                parser.error("Completed trials must use the same model, effort, guides and review schedule")
            if suite["manifest"] != json.loads(args.manifest.read_text()) or suite.get("memorySeeds"):
                parser.error("Completed trials must use the same positions and empty initial private memory")
            for row in json.loads((previous / model / "results.json").read_text()):
                if row["completedEvaluation"]:
                    if row["expectedRounds"] != 10:
                        parser.error("Completed trials must use the same ten-round horizon")
                    run = json.loads((previous / model / row["id"] / "live-run.json").read_text())
                    prepared_hash = hashlib.sha256(
                        (args.prepared / row["case"] / "exploration.json").read_bytes()
                    ).hexdigest()
                    if run["checkpointSha256"] != prepared_hash:
                        parser.error("Completed trial checkpoint differs from the prepared position")
                    completed.add(row["id"])
        commands[model] = [
            sys.executable,
            "-u",
            "-m",
            "tests.live_strategy_positions",
            "--prepared",
            str(args.prepared.resolve()),
            "--manifest",
            str(args.manifest.resolve()),
            "--state-dir",
            str(root / model),
            "--port",
            str(args.port + index * 100),
            "--parallel",
            "2",
            "--rounds",
            "10",
            "--repetitions",
            "2",
            "--model",
            model,
            "--reasoning-effort",
            "medium",
            "--strategy-guide",
            "--civilization-guide",
            "--review-interval",
            "10",
            "--max-invocations",
            "60",
            "--max-seconds",
            "14400",
            "--turn-timeout",
            "1200",
            *[argument for name, path in sources.items() for argument in ("--variant", f"{name}={path}")],
            *[argument for name in sorted(completed) for argument in ("--skip-trial", name)],
        ]
    write_private_json(
        root / "commands.json",
        {
            "commands": commands,
            "sources": {
                name: {"path": str(path), "sha256": tree_hash(path / "civ_agent")} for name, path in sources.items()
            },
            "memory": "Identical empty private stores; each paired trial loads the same prepared native checkpoint",
            "nativeAdvice": False,
            "adaptiveEffort": False,
            "roleSchedules": {"economy": 5, "military": 5, "review": 10},
            "deployment": False,
            "completedSuites": [str(path) for path in prior_suites],
        },
    )
    auth = args.auth_root.resolve()
    login = private_directory(root / "login")
    identity = "context-screen-" + hashlib.sha256(str(root).encode()).hexdigest()[:16]
    authorize_agent(auth, identity, "evaluation", login)
    environment = {
        **os.environ,
        "CIV_AUTH_SOCKET": str(auth / "ipc/broker.sock"),
        "CIV_AUTH_TOKEN_FILE": str(login / "auth-client.json"),
        "CODEX_HOME": str(private_directory(root / "codex")),
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
    }

    def run(model):
        with (root / (model + ".log")).open("w") as log:
            print("Starting paired screening:", model, flush=True)
            code = subprocess.run(commands[model], env=environment, stdout=log, stderr=log).returncode
            print("Screening exited:", model, code, flush=True)
            return model, code

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = dict(pool.map(run, commands))
        write_private_json(root / "exit-codes.json", results)
        return int(any(results.values()))
    finally:
        # Remove only this evaluator's client; never alter another live runtime.
        with locked(auth / ".clients.lock", wait=True):
            registry = auth / "clients.json"
            clients = json.loads(registry.read_text())
            token = json.loads((login / "auth-client.json").read_text())["token"]
            key = identity + "-evaluation"
            if clients.get(key) == hashlib.sha256(token.encode()).hexdigest():
                clients.pop(key)
                write_private_json(registry, clients)


if __name__ == "__main__":
    raise SystemExit(main())
