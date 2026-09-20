"""Opt-in discovery comparison: one real gameplay or communication job on a saved position.

Each invocation owns an isolated host and Rome service. A baseline can select an
archived Python source directory without switching or modifying the working tree.
"""

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from civ_agent.commands.agent import player_credentials
from civ_agent.host_connection import HostConnection
from civ_agent.scheduler import Agent, Operator, agent_services, operator_token, player_prompt
from civ_agent.tls import endpoint_configuration, write_private_json

MODEL = "gpt-5.6-luna"
EFFORT = "medium"
MESSAGE = (
    "Would you be open to a nonbinding understanding to discuss border concerns before a future conflict? "
    "Please tell me any conditions you would want and remember your position for our next conversation. "
    "I am not proposing a trade or a native diplomatic action."
)


def run(args):
    root = args.state_dir.resolve()
    root.mkdir(mode=0o700)  # Never overwrite an earlier run or the source match.
    source = json.loads(args.checkpoint.read_text())
    checkpoint = source.get("host", source)
    assert json.loads(checkpoint["gameJson"])["currentPlayer"] == "Rome"
    write_private_json(root / "exploration.json", checkpoint)
    for name in ("players.json", "operator.json"):
        shutil.copyfile(args.source_state / name, root / name)
        (root / name).chmod(0o600)
    record = {
        "purpose": args.purpose,
        "checkpointSha256": hashlib.sha256(json.dumps(checkpoint, sort_keys=True).encode()).hexdigest(),
        "model": MODEL,
        "reasoningEffort": EFFORT,
        "agentSource": str(args.agent_source.resolve()) if args.agent_source else "working tree",
    }
    write_private_json(root / "discovery-run.json", record)
    processes = []
    logs = []
    operator = service = None
    activation = None
    lease = str(uuid.uuid4())

    def start(label, command, ready, environment=None):
        path = root / f"{label}.log"
        log = path.open("w")
        logs.append(log)
        process = subprocess.Popen(command, stdout=log, stderr=log, env=environment)
        processes.append(process)
        deadline = time.monotonic() + 180
        while not any(line.startswith(ready) for line in path.read_text().splitlines()):
            if process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError(f"{label} failed to start; inspect {path}")
            time.sleep(0.25)
        print(f"Ready: {label} ({args.purpose})", flush=True)

    def op(operation, **fields):
        assert operator is not None
        return operator.call(operation, **({"leaseId": lease} if operation != "status" else {}), **fields)

    try:
        command = [sys.executable, "-m", "civ_agent"]
        start(
            "host",
            command + ["host", "start", "--state-dir", str(root), "--port", str(args.port)],
            "MCP-READY:",
        )
        environment = os.environ.copy()
        if args.agent_source:
            environment["PYTHONPATH"] = str(args.agent_source.resolve())
        start(
            "Rome",
            command
            + [
                "agent-server",
                "start",
                "Rome",
                "--state-dir",
                str(root),
                "--port",
                str(args.port + 1),
                "--model",
                MODEL,
                "--reasoning-effort",
                EFFORT,
            ],
            "AGENT-READY:",
            environment,
        )
        origin, ca = endpoint_configuration(root, None, None)
        operator = Operator(origin, operator_token(root / "operator.json"), ca)
        service = agent_services(root)["Rome"]
        before = op("acquire")["status"]
        epoch = before["epoch"]
        op("job_limits", maxInvocations=32, maxConcurrent=2, maxPerCivilization=2)
        connected = op("connect", epoch=epoch, civilization="Rome")
        service.call(
            "connect",
            epoch=epoch,
            playerToken=connected["playerToken"],
            model=MODEL,
            reasoningEffort=EFFORT,
            timeoutSeconds=600,
        )
        op("unbarrier")
        if args.purpose == "gameplay":
            activation = str(uuid.uuid4())
            active = op(
                "activate",
                epoch=epoch,
                serverInstance=before["serverInstance"],
                handoff=before["handoff"],
                civilization="Rome",
                activationId=activation,
            )
            service.call(
                "start",
                activationId=activation,
                epoch=epoch,
                civilization="Rome",
                gameTurn=before["turn"],
                handoff=before["handoff"],
                threadId=None,
                mcpUrl=origin + "/mcp",
                playerToken=active["playerToken"],
                prompt=player_prompt("Rome", before["turn"]),
                model=MODEL,
                reasoningEffort=EFFORT,
                timeoutSeconds=600,
                leaseSeconds=30,
            )
        else:

            async def message():
                _, token = player_credentials(root / "players.json", "France")
                async with HostConnection(origin + "/mcp", token, ca) as host:
                    result = await host.tool(
                        "player_communication",
                        {"op": "send", "epoch": epoch, "requestId": str(uuid.uuid4()), "to": "Rome", "text": MESSAGE},
                    )
                    assert "error" not in result, result

            asyncio.run(message())
        deadline = time.monotonic() + 660
        heartbeat = 0
        while time.monotonic() < deadline:
            if time.monotonic() - heartbeat >= 5:
                op("heartbeat")
                if activation:
                    service.call("heartbeat", activationId=activation)
                heartbeat = time.monotonic()
            if activation:
                status = service.call("status", activationId=activation)
                if status["phase"] in Agent.TERMINAL:
                    assert status["phase"] == "COMPLETED", status["phase"]
                    after = op("status")["status"]
                    assert after["handoff"] == before["handoff"] + 1, "Model failed to finish its game turn"
                    service.call(
                        "finalize",
                        activationId=activation,
                        epoch=epoch,
                        gameTurn=before["turn"],
                        committedHandoff=after["handoff"],
                    )
                    break
            else:
                completed = list((root / "agent-services/Rome/chat-jobs").glob("*.json"))
                if completed:
                    assert len(completed) == 1
                    status = json.loads(completed[0].read_text())
                    assert status["phase"] == "COMPLETED", status["phase"]
                    break
            time.sleep(0.5)
        else:
            raise TimeoutError("Live discovery job did not finish")
        record.update(
            activationId=status["activationId"],
            threadId=status["threadId"],
            phase=status["phase"],
            usage=status["usage"],
        )
        print(json.dumps(record), flush=True)
    finally:
        if service is not None:
            if activation:
                with contextlib.suppress(Exception):
                    service.call("interrupt", activationId=activation)
            with contextlib.suppress(Exception):
                service.call("disconnect")
        if operator is not None:
            with contextlib.suppress(Exception):
                op("release")
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
        write_private_json(root / "discovery-run.json", record)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--source-state", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True, help="Checkpoint or round-checkpoint manifest")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--purpose", choices=("gameplay", "communication"), required=True)
    parser.add_argument("--agent-source", type=Path, help="Archived src directory for a baseline service")
    os.umask(0o077)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
