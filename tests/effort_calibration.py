"""Opt-in, bounded decision replays from immutable gameplay histories.

These are single decision points, not simulated turns. Only recorded visible
messages preceding the selected answer are replayed; no generated action runs.
"""

import argparse
import asyncio
import copy
import hashlib
import json
import os
import random
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

from civ_agent.agent_service import CodexProcess
from civ_agent.decision_contracts import DecisionContract
from civ_agent.effort_decisions import EFFORT_LEVELS, supported_levels
from civ_agent.model_status import error_fields
from civ_agent.protocol import JsonObject
from civ_agent.provider_gate import RequestBudget
from civ_agent.runner_history import Chunks
from civ_agent.strict_schema import fingerprint
from civ_agent.structured_decisions import capture_answer
from civ_agent.tls import write_private_json

MODELS = ("gpt-6-luna", "gpt-6.1-terra")
REPETITIONS = 2
REQUEST_LIMIT = 132
SELECTIONS = [
    ("worker-and-queue", "pangaea-v5-20261031", "Inca", 64, "economy", 0),
    ("queue-reprioritization", "pangaea-v5-20261031", "Inca", 72, "economy", 0),
    ("settlement-budget", "pangaea-v5-20261031", "Inca", 73, "gameplay", 0),
    ("persistent-travel", "two-continents-v5-20261031", "China", 40, "gameplay", 5),
    ("routine-economy", "pangaea-v5-20261031", "China", 64, "economy", 0),
    ("routine-main", "two-continents-v5-20261031", "Persia", 55, "gameplay", 0),
]


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def read(path):
    return json.loads(path.read_text())


def visible_prefix(path, target_item):
    """Stop before the target output; discard reasoning and internal metadata."""
    items = []
    instructions = None
    found = False
    for line in path.open():
        # Do not decode retained reasoning, even though it would be filtered later.
        if '"type":"reasoning"' in line or '"type": "reasoning"' in line:
            continue
        event = json.loads(line)
        payload = event.get("payload", {})
        if event["type"] == "session_meta":
            instructions = payload.get("base_instructions", {}).get("text")
        elif event["type"] == "compacted":
            raise ValueError("Compacted source requires its actual compacted prefix")
        elif event["type"] == "response_item":
            if payload.get("id") == target_item:
                found = True
                break
            kind = payload.get("type")
            if kind == "message":
                if payload["role"] not in {"developer", "user", "assistant", "system"}:
                    raise ValueError("Unsupported message role")
                if any(c.get("type") not in {"input_text", "output_text"} for c in payload["content"]):
                    raise ValueError("Only text-only contexts are supported")
                items.append({k: payload[k] for k in ("type", "role", "content", "phase") if k in payload})
            elif kind == "function_call_output":
                if not isinstance(payload.get("output"), str) or not payload.get("name"):
                    raise ValueError("Expected standalone recorded tool output")
                items.append({k: payload[k] for k in ("type", "name", "namespace", "output") if k in payload})
            elif kind != "additional_tools":
                raise ValueError("Unsupported visible source item: " + str(kind))
    if not found or not instructions or not items:
        raise ValueError("Complete source prefix unavailable")
    return instructions, items


def schedule(cases):
    """Balanced, interleaved cells; seed fixed before any result is available."""
    rows = []
    rng = random.Random(20261101)
    for repetition in range(1, REPETITIONS + 1):
        block = [
            {"case": case, "model": model, "effort": effort, "repetition": repetition}
            for case in cases
            for model in MODELS
            for effort in EFFORT_LEVELS
        ]
        rng.shuffle(block)
        for row in block:
            row["id"] = f"{row['case']}-{row['model']}-{row['effort']}-r{repetition}"
        rows.extend(block)
    return rows


def prepare(root):
    if root.exists():
        raise ValueError("Use a fresh artifact directory")
    root.mkdir(parents=True)
    audit = Path("build/gameplay-context-audit-20261101")
    catalogues = {
        "gameplay": read(Path("build/model-service-warning-20261101/rejected-catalogue.json")),
        "economy": read(audit / "economy-catalogue.json"),
    }
    contracts = {role: DecisionContract(role, catalogue) for role, catalogue in catalogues.items()}
    for role, contract in contracts.items():
        write_private_json(root / (role + "-schema.json"), contract.schema)
        write_private_json(root / (role + "-catalogue.json"), catalogues[role])
    cases = []
    for name, game, civilization, turn, role, index in SELECTIONS:
        service = Path("benchmarks") / game / "agent-services" / civilization
        db = sqlite3.connect(f"file:{service.resolve()}/civilization/memory.sqlite3?mode=ro", uri=True)
        db.execute("PRAGMA query_only=ON")
        jobs = db.execute("select id from jobs where game_turn=? and purpose=?", (turn, role)).fetchall()
        if len(jobs) != 1:
            raise ValueError("Ambiguous source job")
        job = jobs[0][0]
        records = [
            json.loads(row[0])
            for row in db.execute("select body from decision_journal where job=? order by rowid", (job,))
        ]
        db.close()
        record = records[index]
        if contracts[role].fingerprint != record["schema"]["fingerprint"]:
            raise ValueError("Historical schema differs: " + name)
        event_path = service / "history" / hashlib.sha256(job.encode()).hexdigest() / "events.jsonl"
        chunks = Chunks()
        visible = []
        for line in event_path.open():
            event = chunks.expand(json.loads(line))
            if event and event["kind"] in {"developer_message", "user_message", "assistant_message", "tool_result"}:
                visible.append(event)
        target = next(e for e in visible if e["kind"] == "assistant_message" and e["data"]["turnId"] == record["turn"])
        if hashlib.sha256(target["data"]["text"].encode()).hexdigest() != record["outputSha256"]:
            raise ValueError("Historical output hash mismatch")
        rollouts = list(
            (Path("benchmarks") / game / "codex" / civilization / "sessions").rglob("*" + record["thread"] + ".jsonl")
        )
        if len(rollouts) != 1:
            raise ValueError("Ambiguous source rollout")
        instructions, items = visible_prefix(rollouts[0], target["data"]["itemId"])
        developer = next(e["data"]["text"] for e in visible if e["kind"] == "developer_message")
        startup = next(e["data"]["text"] for e in visible if e["kind"] == "user_message")
        for text in (developer, startup):
            if not any(text == part.get("text") for item in items for part in item.get("content", [])):
                raise ValueError("Startup mismatch between rollout and service history")
        case = {
            "id": name,
            "game": game,
            "civilization": civilization,
            "turn": turn,
            "role": role,
            "job": job,
            "sourceDecisionId": record["id"],
            "sourceDecisionIndex": index,
            "sourceModel": record["model"],
            "schema": record["schema"],
            "instructions": instructions,
            "input": items,
            "inputSha256": digest(items),
            "developer": developer,
            "startup": startup,
            "sourceHistory": str(event_path),
            "sourceHistorySha256": hashlib.sha256(event_path.read_bytes()).hexdigest(),
            "sourceRollout": str(rollouts[0]),
            "sourceRolloutSha256": hashlib.sha256(rollouts[0].read_bytes()).hexdigest(),
            "targetOutputExcluded": True,
            "historicalAssistantMessages": sum(i.get("role") == "assistant" for i in items),
        }
        destination = root / "cases" / name
        destination.mkdir(parents=True)
        write_private_json(destination / "context.json", case)
        write_private_json(
            destination / "historical-answer.json", {"raw": target["data"]["text"], "decision": record["decision"]}
        )
        activation = read(service / "activations" / (hashlib.sha256(job.encode()).hexdigest() + ".json"))
        write_private_json(destination / "historical-effort.json", activation.get("effortSelection"))
        cases.append(
            {
                k: case[k]
                for k in (
                    "id",
                    "game",
                    "civilization",
                    "turn",
                    "role",
                    "sourceDecisionId",
                    "inputSha256",
                    "historicalAssistantMessages",
                )
            }
        )
    manifest = {
        "createdAtUnix": time.time(),
        "scope": "single-decision visible-context replay; no dispatch",
        "models": MODELS,
        "efforts": EFFORT_LEVELS,
        "repetitions": REPETITIONS,
        "plannedGenerations": len(cases) * len(MODELS) * len(EFFORT_LEVELS) * REPETITIONS,
        "requestCeilingIncludingRetries": REQUEST_LIMIT,
        "cases": cases,
        "schedule": schedule([c["id"] for c in cases]),
        "sourceRevision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "limitations": [
            "One decision per cell; inspect is unresolved, never a quality success.",
            "No native execution, follow-up decisions, total-turn savings, or win-rate claim.",
            "Historical public assistant messages retained for continuation case; private reasoning excluded for every cell.",
            "Purposive six-context sample and two repetitions are screening, not population calibration.",
            "Strategic choices are recorded separately; no hardcoded preferred strategy establishes correctness.",
        ],
    }
    write_private_json(root / "manifest.json", manifest)
    print(encoded({"cases": cases, "plannedGenerations": manifest["plannedGenerations"]}))


def configure(root):
    game = Path("benchmarks/two-continents-v5-20261031").resolve()
    container = read(game / "container.json")
    home = root / "codex"
    home.mkdir(exist_ok=True, mode=0o700)
    (home / "config.toml").write_text('cli_auth_credentials_store = "ephemeral"\n')
    os.environ.update(
        CODEX_HOME=str(home),
        CIV_AUTH_SOCKET=str(Path(container["auth_root"]) / "ipc/broker.sock"),
        CIV_AUTH_TOKEN_FILE=str(game / "agent-services/Arabia/auth-client.json"),
        CIV_MODEL_REQUEST_BUDGET=str(root / "requests.sqlite"),
        CIV_MODEL_REQUEST_LIMIT=str(REQUEST_LIMIT),
    )


def wire_context(body, case, model, effort, schema):
    """Replace only historical conversation input, asserting all experiment controls."""
    if body.get("model") != model or body.get("reasoning", {}).get("effort") != effort:
        raise ValueError("Requested model/effort not present on provider wire")
    if body.get("tools") != [] or body.get("tool_choice") != "none":
        raise ValueError("Executable tools exposed")
    if body.get("text", {}).get("format", {}).get("schema") != schema:
        raise ValueError("Output schema mismatch")
    if digest(case["input"]) != case["inputSha256"]:
        raise ValueError("Frozen input was changed")
    body["input"] = copy.deepcopy(case["input"])
    body["instructions"] = case["instructions"]
    if body.get("service_tier") not in {None, "default"}:
        raise ValueError("Unexpected service tier")
    return {
        "model": body["model"],
        "reasoning": body["reasoning"],
        "serviceTier": body.get("service_tier"),
        "inputSha256": digest(body["input"]),
        "instructionsSha256": digest(body["instructions"]),
        "schemaFingerprint": fingerprint(schema),
        "executableTools": 0,
        "schemaCharactersEstimate": len(encoded(schema)),
        "conversationCharactersEstimate": len(encoded(body["input"])),
    }


async def trial(root, row):
    destination = root / "trials" / row["id"]
    if destination.exists():
        raise ValueError("Never rerun a reserved trial")
    destination.mkdir(parents=True)
    write_private_json(destination / "reservation.json", {**row, "reservedAtUnix": time.time()})
    case = read(root / "cases" / row["case"] / "context.json")
    contract = DecisionContract(case["role"], read(root / (case["role"] + "-catalogue.json")))
    if contract.fingerprint != case["schema"]["fingerprint"]:
        raise ValueError("Frozen contract mismatch")
    done = asyncio.Event()
    answers = {}
    result: JsonObject = {
        **row,
        "startedAtUnix": time.time(),
        "requests": [],
        "notices": [],
        "valid": False,
        "dispatchedOperations": 0,
    }

    async def notification(method, params):
        if method == "item/completed":
            capture_answer(answers, params)
        elif method == "turn/completed":
            turn = params["turn"]
            result.update(status=turn.get("status"), turnId=turn["id"])
            if isinstance(turn.get("error"), dict):
                result["modelFailure"] = error_fields(turn["error"])
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
            if method in {"service/requestFailed", "service/transportFailure"}:
                done.set()

    executable = shutil.which("codex")
    if executable is None:
        raise ValueError("Codex unavailable")
    process = CodexProcess(executable, destination, "", None, notification)
    try:
        async with asyncio.timeout(600):
            await process.start(
                "isolated-no-dispatch", {"provider": "openai", "model": row["model"], "reasoningEffort": row["effort"]}
            )
            gate = process.provider_gate
            assert gate is not None and gate.session is not None
            request = gate.session.request

            def capture(method, url, **kwargs):
                body = kwargs.get("json")
                if isinstance(body, dict):
                    captured = wire_context(body, case, row["model"], row["effort"], contract.schema)
                    result.setdefault("wire", []).append(captured)
                    write_private_json(destination / "wire.json", result["wire"])
                return request(method, url, **kwargs)

            gate.session.request = capture
            thread = await process.request(
                "thread/start",
                {
                    "model": row["model"],
                    "cwd": str(destination),
                    "approvalPolicy": "never",
                    "sandbox": "workspace-write",
                    "developerInstructions": case["developer"],
                    "config": {"model_reasoning_effort": row["effort"]},
                },
            )
            result["threadId"] = thread["thread"]["id"]
            await process.request(
                "turn/start",
                {
                    "threadId": result["threadId"],
                    "input": [{"type": "text", "text": case["startup"]}],
                    "model": row["model"],
                    "effort": row["effort"],
                    "outputSchema": contract.schema,
                },
            )
            await done.wait()
            answer = answers.get(result.get("turnId"))
            if answer:
                (destination / "answer.txt").write_text(answer)
            if result.get("status") == "completed" and answer:
                decision = contract.parse(answer)
                write_private_json(destination / "decision.json", decision)
                result.update(valid=True, kind=decision["kind"], operations=len(decision.get("operations", [])))
    except Exception as error:
        result.update(exceptionType=type(error).__name__, exception=str(error)[:500])
    finally:
        await process.close()
        result["elapsedSeconds"] = time.time() - result["startedAtUnix"]
        write_private_json(destination / "result.json", result)
    print(
        encoded({k: result.get(k) for k in ("id", "valid", "status", "kind", "elapsedSeconds", "exceptionType")}),
        flush=True,
    )
    return result


async def run(root, preflight=False):
    configure(root)
    manifest = read(root / "manifest.json")
    if read(root / "rubric.json").get("frozenBeforeInference") is not True:
        raise ValueError("Freeze a rubric before inference")

    async def ignore(method, params):
        pass

    executable = shutil.which("codex")
    if executable is None:
        raise ValueError("Codex unavailable")
    probe = root / "preflight"
    probe.mkdir(exist_ok=True)
    process = CodexProcess(executable, probe, "", None, ignore)
    try:
        await process.start(
            "isolated-no-dispatch", {"provider": "openai", "model": MODELS[0], "reasoningEffort": "medium"}
        )
        supported = {model: sorted(await supported_levels(process, model)) for model in MODELS}
        if any(set(EFFORT_LEVELS) - set(levels) for levels in supported.values()):
            raise ValueError("A requested effort is unsupported; no substitution")
        write_private_json(root / "supported-models.json", supported)
    finally:
        await process.close()
    if preflight:
        print(
            encoded(
                {
                    "supported": supported,
                    "inferenceRequests": RequestBudget(root / "requests.sqlite", REQUEST_LIMIT).used(),
                }
            )
        )
        return
    if not (root / "frozen.json").exists():
        paths = [root / "manifest.json", root / "rubric.json", *sorted((root / "cases").glob("*/context.json"))]
        write_private_json(
            root / "frozen.json",
            {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths},
        )
    for relative, expected in read(root / "frozen.json").items():
        if hashlib.sha256((root / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("Frozen evaluation input changed")
    write_private_json(
        root / "runner.json",
        {
            "sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "codexVersion": subprocess.check_output([executable, "--version"], text=True).strip(),
        },
    )
    semaphore = asyncio.Semaphore(4)

    async def one(row):
        async with semaphore:
            path = root / "trials" / row["id"]
            if path.exists():
                # An interrupted reservation consumes its cell; never silently retry.
                return read(path / "result.json") if (path / "result.json").exists() else {**row, "status": "uncertain"}
            return await trial(root, row)

    first = await one(manifest["schedule"][0])
    if not first.get("valid"):
        write_private_json(root / "results.json", [first])
        raise ValueError("First replay failed; remaining cells were not dispatched")
    results = [first, *await asyncio.gather(*(one(row) for row in manifest["schedule"][1:]))]
    write_private_json(root / "results.json", results)
    print(
        encoded(
            {
                "trials": len(results),
                "valid": sum(bool(r.get("valid", False)) for r in results),
                "providerRequests": RequestBudget(root / "requests.sqlite", REQUEST_LIMIT).used(),
            }
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "preflight", "run"))
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = args.output.resolve()
    if args.action == "prepare":
        prepare(root)
    else:
        asyncio.run(run(root, preflight=args.action == "preflight"))


if __name__ == "__main__":
    main()
