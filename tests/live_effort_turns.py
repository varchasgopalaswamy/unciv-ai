"""Opt-in paired effort selection through complete isolated role activations.

Uses real native checkpoints, the production gateway/decision loop and a local
classifier. Other roles remain quiescent. A wait needing another role is an
unfinished trial, never a fabricated completion. No production match is edited.
"""

import argparse
import asyncio
import copy
import hashlib
import os
import random
import shutil
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from civ_agent.agent_service import CodexProcess
from civ_agent.civilization_state import CivilizationState
from civ_agent.commands.host import ensure_players
from civ_agent.decision_contracts import DecisionContract
from civ_agent.decisions_policy import DecisionsSettings
from civ_agent.decisions_tasks import acceptance_threshold, parse_answers, request_body, request_metadata
from civ_agent.diplomacy_decisions import DecisionsClient
from civ_agent.economy_roles import TurnRoles
from civ_agent.economy_schedule import EconomySchedule
from civ_agent.effort_decisions import EFFORT_LEVELS, select_effort, supported_levels
from civ_agent.host_connection import HostConnection
from civ_agent.memory_records import public_record
from civ_agent.model_status import error_fields
from civ_agent.proposal_gateway import ProposalGateway, public_result
from civ_agent.protocol import JsonObject
from civ_agent.provider_gate import RequestBudget
from civ_agent.service_io import ServiceIO
from civ_agent.startup_context import prepare_startup, startup_prompt
from civ_agent.structured_decisions import DecisionLoop, capture_answer, generation_event
from civ_agent.tls import ensure_host_tls, private_directory, write_private_json
from tests.effort_calibration import MODELS, digest, encoded, read
from tests.evaluation_sources import tree_hash
from tests.live_saved_position import copy_position_checkpoint

REQUEST_LIMIT = 120
TRIAL_REQUEST_LIMIT = 20


class EvaluationGateway(ProposalGateway):
    """Freeze only opaque delivery IDs, inside separate trial databases."""

    @staticmethod
    def reset_coordination(job):
        ProposalGateway.reset_coordination(job)
        job.coordination_scope = digest([job.id, job.epoch, job.purpose])[:24]


class PairedBudget(RequestBudget):
    """Every upstream attempt spends both a trial and suite reservation."""

    def __init__(self, suite, trial):
        self.suite, self.trial = suite, trial

    def reserve(self, identity, kind, *, retry_scope=None):
        if self.suite.used() >= self.suite.limit:
            return False
        if not self.trial.reserve(identity, kind, retry_scope=retry_scope):
            return False
        return self.suite.reserve(identity, kind, retry_scope=retry_scope)


def schedule(cases, repetitions=1):
    rows = []
    rng = random.Random(20261101)
    for repetition in range(1, repetitions + 1):
        for case in cases:
            block = [
                {"case": case, "variant": variant, "model": model, "repetition": repetition}
                for model in MODELS
                for variant in ("baseline", "candidate")
            ]
            rng.shuffle(block)
            rows.extend(block)
    for index, row in enumerate(rows):
        row["id"] = f"{row['case']}-{row['model']}-{row['variant']}-r{row['repetition']}"
        row["port"] = 25400 + index
    return rows


def terminal_state(job):
    if job.role_finished.is_set():
        return "completed"
    if job.role_wait_pending.is_set() or job.negotiation_pending.is_set():
        return "unfinished_peer_wait"
    return None


@asynccontextmanager
async def native_position(root, position, civilization, port):
    copy_position_checkpoint(position / "exploration.json", root)
    ensure_players(root / "players.json", False, [civilization])
    tls, ca = ensure_host_tls(root)
    repo = Path(__file__).resolve().parents[1]
    command = [
        str(repo / "build/install/unciv-ai/bin/unciv-ai"),
        "--resume-checkpoint",
        str(root / "exploration.json"),
        "--mcp-config",
        str(root / "players.json"),
        "--tls-config",
        str(tls),
        "--data-dir",
        str(root / "data"),
        "--port",
        str(port),
    ]
    with (root / "host.log").open("w") as log:
        process = await asyncio.create_subprocess_exec(
            *command, cwd=repo / "vendor/Unciv/android/assets", stdout=log, stderr=log
        )
        try:
            async with asyncio.timeout(90):
                while "MCP-READY:" not in (root / "host.log").read_text():
                    if process.returncode is not None:
                        raise RuntimeError("Isolated native host exited; see host.log")
                    await asyncio.sleep(0.2)
            token = read(root / "players.json")["players"][0]["token"]
            async with HostConnection(f"https://localhost:{port}/mcp", token, ca) as connection:
                yield connection
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 15)
                except TimeoutError:
                    process.kill()
                    await process.wait()


class LocalEffort:
    """Same versioned task/parser/selection policy, with a separately counted local call."""

    def __init__(self, root, settings):
        self.root, self.settings = root, settings

    async def evaluate(self, evidence):
        body = request_body(evidence, self.settings)
        write_private_json(self.root / "classifier-request.json", body)
        client = DecisionsClient(backend="jev")
        raw = await client.evaluate(body, timeout=30)
        result = {
            **request_metadata(body, evidence),
            **parse_answers(body, raw, acceptance_threshold(evidence, self.settings)),
            "usage": raw.get("usage"),
        }
        write_private_json(self.root / "classifier-result.json", result)
        return result


async def role_context(root, connection, case, model, io, memory):
    before = await connection.tool("player_turn", {})
    if before.get("phase") != "ACTIVE" or before["civilizationProfile"]["name"] != case["civilization"]:
        raise ValueError("Wrong native position")
    write_private_json(root / "before.json", before)
    settings = {"provider": "openai", "model": model, "reasoningEffort": "medium"}
    record: JsonObject = {
        "activationId": "evaluation",
        "epoch": before["epoch"],
        "gameTurn": before["turn"],
        "purpose": case["role"],
        "settings": settings,
        "modelPhase": case["role"],
    }

    async def save(value):
        write_private_json(root / "activation.json", value)

    policy = DecisionsSettings(decisions_enabled=True, decisions_backend="jev", decisions_effort_enabled=True)
    service = SimpleNamespace(
        memory=memory,
        io=io,
        records={"evaluation": record},
        active_id="evaluation",
        economy_interval=5,
        _save=save,
        state_dir=root,
        failure=None,
        stopping=False,
        decisions_settings=policy,
        civilization=case["civilization"],
    )
    gateway = service.gateway = EvaluationGateway(memory, io, private_directory(root / "workspace"))
    job = await gateway.open("evaluation", case["role"], before["epoch"], connection, settings, before["turn"])
    roles = TurnRoles(service, record)
    gateway.role_coordinator = roles
    roles.register(job)
    roles.requests = copy.deepcopy(case["requests"])
    # These controls explicitly seed an unchanged last review, not historical memory.
    EconomySchedule(memory, 5).complete(before, before["turn"] - case["reviewAge"])
    await roles.initialize_economy(before)
    roles.ready.update({role: role != case["role"] for role in roles.ready})
    prepared = await prepare_startup(gateway, job)
    prompt = startup_prompt(
        prepared,
        case["role"],
        "Play the current turn using your ordinary objectives and current permitted information.",
        strategy_guide=True,
        civilization_guide=True,
        civilization=case["civilization"],
    )
    contract = DecisionContract(case["role"], await gateway.tools(job))
    plans = [
        public_result(public_record(key, job.memory["records"][key]))
        for key in job.memory.get("contextIds", [])
        if job.memory["records"][key]["category"] in {"strategy", "city_plan", "handoff"}
    ]
    context = {"prepared": prepared, "developer": prompt.instructions, "user": prompt.user, "plans": plans}
    write_private_json(root / "context.json", context)
    write_private_json(root / "schema.json", contract.schema)
    return service, gateway, job, record, settings, prepared, prompt, contract


async def worker(suite, row, probe=False):
    root = suite / ("probes" if probe else "trials") / row["id"]
    if root.exists():
        raise ValueError("Never rerun a reserved role activation")
    private_directory(root)
    write_private_json(root / "reservation.json", row)
    manifest = read(suite / "manifest.json")
    case = next(c for c in manifest["cases"] if c["id"] == row["case"])
    io, memory = ServiceIO(), CivilizationState(root / "memory", case["civilization"])
    result: JsonObject = {**row, "status": "unfinished", "requests": [], "notices": [], "decisions": []}
    started, process = time.monotonic(), None
    done, answers = asyncio.Event(), {}
    record = {}
    try:
        async with native_position(
            root, suite / "positions" / case["id"], case["civilization"], row["port"]
        ) as connection:
            service, gateway, job, record, selected, prepared, prompt, contract = await role_context(
                root, connection, case, row["model"], io, memory
            )
            result["contextSha256"] = digest(read(root / "context.json"))
            result["schemaFingerprint"] = contract.fingerprint
            checked = {}
            if not probe:
                checked = read(suite / "probes" / row["id"] / "result.json")
                if any(result[key] != checked[key] for key in ("contextSha256", "schemaFingerprint")):
                    raise ValueError("Live context differs from verified preflight")
            loop = DecisionLoop(gateway, job, contract, record)

            async def notification(method, params):
                await generation_event(gateway, record, method, params)
                if method == "item/completed":
                    capture_answer(answers, params)
                elif method == "turn/completed":
                    result["lastTurn"] = {k: params["turn"].get(k) for k in ("id", "status")}
                    if isinstance(params["turn"].get("error"), dict):
                        result["modelFailure"] = error_fields(params["turn"]["error"])
                    done.set()
                elif method == "thread/tokenUsage/updated":
                    result["usage"] = params.get("tokenUsage", {})
                elif method == "service/providerRequest":
                    result["requests"].append(params)
                elif method in {
                    "service/modelStatus",
                    "service/requestFailed",
                    "service/transportFailure",
                    "service/generationFailed",
                }:
                    result["notices"].append({"method": method, **params})
                    if method != "service/modelStatus":
                        done.set()

            home = private_directory(root / "codex")
            (home / "config.toml").write_text('cli_auth_credentials_store = "ephemeral"\n')
            os.environ["CODEX_HOME"] = str(home)
            executable = shutil.which("codex")
            if not executable:
                raise ValueError("Codex unavailable")
            process = CodexProcess(executable, root / "workspace", "", None, notification)
            await process.start(job.token, selected)
            for model in MODELS:
                if set(EFFORT_LEVELS) - await supported_levels(process, model):
                    raise ValueError("Five effort levels must be supported")
            gateway.advisories = LocalEffort(root, service.decisions_settings)
            selected = await select_effort(service, record, job, prepared, selected, process)
            gateway.advisories = None
            result["selection"] = record["effortSelection"]
            if not probe and result["selection"] != checked["selection"]:
                raise ValueError("Local selection differs from verified preflight")
            if record["effortSelection"]["reason"] not in {"selected", "not_standout"}:
                raise ValueError("Local selector did not produce a usable result")
            if probe:
                result["status"] = "probe_completed"
            else:
                gate = process.provider_gate
                assert gate is not None and gate.session is not None
                gate.budget = PairedBudget(
                    RequestBudget(suite / "requests.sqlite", REQUEST_LIMIT),
                    RequestBudget(root / "requests.sqlite", TRIAL_REQUEST_LIMIT),
                )
                request = gate.session.request

                def capture(method, url, **kwargs):
                    body = kwargs.get("json")
                    if isinstance(body, dict):
                        if (body.get("model"), body.get("reasoning", {}).get("effort")) != (
                            row["model"],
                            selected["reasoningEffort"],
                        ):
                            raise ValueError("Model/effort changed on provider wire")
                        if body.get("tools") != [] or body.get("tool_choice") != "none":
                            raise ValueError("Executable tools exposed")
                        if body.get("text", {}).get("format", {}).get("schema") != contract.schema:
                            raise ValueError("Strict schema changed")
                        result.setdefault("wire", []).append(
                            {
                                "model": body["model"],
                                "effort": selected["reasoningEffort"],
                                "inputSha256": digest(body["input"]),
                                "schemaFingerprint": contract.fingerprint,
                                "schemaCharactersEstimate": len(encoded(contract.schema)),
                                "executableTools": 0,
                            }
                        )
                    return request(method, url, **kwargs)

                gate.session.request = capture
                thread = await process.request(
                    "thread/start",
                    {
                        "model": row["model"],
                        "cwd": str(root / "workspace"),
                        "approvalPolicy": "never",
                        "sandbox": "workspace-write",
                        "developerInstructions": prompt.instructions,
                        "config": {"model_reasoning_effort": selected["reasoningEffort"]},
                        "dynamicTools": [],
                    },
                )
                record["threadId"] = result["threadId"] = thread["thread"]["id"]
                record["effectiveSettings"] = selected
                gateway.commit_startup_coordination(job)
                loop.delivered(prepared["reads"]["player_turn"])
                message = prompt.user
                async with asyncio.timeout(1200):
                    for index in range(TRIAL_REQUEST_LIMIT):
                        done.clear()
                        params = await loop.begin(message)
                        turn = await process.request(
                            "turn/start",
                            {
                                "threadId": record["threadId"],
                                "model": row["model"],
                                "effort": selected["reasoningEffort"],
                                **params,
                            },
                        )
                        await loop.bind(turn["turn"]["id"])
                        await done.wait()
                        completed = result.get("lastTurn", {})
                        if completed.get("id") != turn["turn"]["id"] or completed.get("status") != "completed":
                            raise ValueError("Model generation did not complete")
                        answer = answers.get(completed["id"], "")
                        receipts = await loop.complete(answer, completed["status"])
                        assert loop.current is not None
                        decision = {"answer": answer, "receipts": receipts, "journal": loop.current}
                        write_private_json(root / f"decision-{index:02d}.json", decision)
                        result["decisions"].append(
                            {
                                "index": index,
                                "status": loop.current["status"],
                                "kind": loop.current.get("decision", {}).get("kind"),
                            }
                        )
                        if terminal := terminal_state(job):
                            result["status"] = terminal
                            break
                        loop.delivered(receipts)
                        message = receipts
                    else:
                        result["status"] = "unfinished_request_limit"
                write_private_json(root / "after.json", await connection.tool("player_turn", {}))
                await gateway.close(job, result["status"] == "completed")
    except Exception as error:
        result.update(status="failed", exceptionType=type(error).__name__, exception=str(error)[:300])
    finally:
        if process:
            await process.close()
        await io.close()
        memory.engine.dispose()
        result["elapsedSeconds"] = time.monotonic() - started
        write_private_json(root / "activation.json", record)
        write_private_json(root / "result.json", result)
    print(encoded({k: result.get(k) for k in ("id", "status", "selection", "elapsedSeconds", "exception")}), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["probe", "run", "worker"])
    parser.add_argument("root", type=Path)
    parser.add_argument("--trial")
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    manifest = read(root / "manifest.json")
    if args.mode == "worker":
        row = next(r for r in manifest["schedule"] if r["id"] == args.trial)
        source_file = __import__("civ_agent").__file__
        assert source_file is not None
        source = Path(source_file).parent
        if tree_hash(source) != manifest["sources"][row["variant"]]:
            raise ValueError("Frozen source changed")
        asyncio.run(worker(root, row, args.probe))
        return
    for relative, expected in manifest["frozen"].items():
        if hashlib.sha256((root / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("Frozen evaluation input changed: " + relative)
    game = Path("benchmarks/two-continents-v5-20261031").resolve()
    container = read(game / "container.json")
    environment = {
        **os.environ,
        "CIV_AUTH_SOCKET": str(Path(container["auth_root"]) / "ipc/broker.sock"),
        "CIV_AUTH_TOKEN_FILE": str(game / "agent-services/Arabia/auth-client.json"),
    }
    if args.mode == "run" and not (root / "preflight-verified.json").exists():
        raise ValueError("Verify paired contexts and offline checks before inference")
    for row in manifest["schedule"]:
        destination = root / ("probes" if args.mode == "probe" else "trials") / row["id"]
        if destination.exists():
            continue  # Reserved uncertain trials are not retried.
        command = [sys.executable, "-u", "-m", "tests.live_effort_turns", "worker", str(root), "--trial", row["id"]]
        if args.mode == "probe":
            command.append("--probe")
        env = {**environment, "PYTHONPATH": str(root / row["variant"]) + os.pathsep + str(Path.cwd())}
        completed = subprocess.run(command, env=env, check=False)
        result = read(destination / "result.json") if (destination / "result.json").exists() else {}
        if completed.returncode or result.get("status") == "failed":
            raise ValueError("Trial failed; inspect artifacts before spending further requests")


if __name__ == "__main__":
    main()
