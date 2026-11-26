import asyncio
import gzip
import json
import sqlite3

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai_harness.step_persistence import ContinuableSnapshot, RunRecord, StepEvent, ToolEffectRecord

from civ_agent import memory_schema as tables
from civ_agent.host_effects import host_request_id
from civ_agent.orchestration.contracts import (
    GameRunRequest,
    GameRunResult,
    ModelSelection,
    RoleResult,
    Scope,
    canonical,
    digest,
)
from civ_agent.orchestration.game_verification import verify_game_artifacts
from civ_agent.orchestration.gateway_execution import operation_identity
from civ_agent.orchestration.store import WorkflowStore
from civ_agent.orchestration.transcripts import backend, pointer, seal
from civ_agent.sqlite_database import sqlite_engine
from civ_agent.tls import write_private, write_private_json
from tests.step_history import save_history

NATIVE_TOOL_OPERATION = operation_identity("job1", "e1", 9, "call2") + ":0"
NATIVE_REQUEST = host_request_id("job1", NATIVE_TOOL_OPERATION)
NATIVE_RECORD = "native:" + NATIVE_REQUEST


def artifacts(tmp_path, *, native_command=False):
    request = GameRunRequest(game="game", player="p0", turns=1, selection=ModelSelection(model="scripted-v1"))
    scope = Scope(game="game", player="p0", epoch="e1", activation="a1")

    async def persist():
        store = WorkflowStore(tmp_path / "player-store", player="p0")
        store.bind_epoch("game", "e1")
        try:
            messages: list[ModelMessage] = [ModelResponse(parts=[TextPart("done")])]
            if native_command:
                messages = [
                    ModelResponse(
                        parts=[
                            ToolCallPart(
                                "execute_commands", {"commands": [{"kind": "FOUND_CITY", "target": "unit1"}]}, "call2"
                            )
                        ]
                    ),
                    ModelRequest(
                        parts=[
                            ToolReturnPart(
                                "execute_commands",
                                {
                                    "receipts": [
                                        {
                                            "operationId": NATIVE_TOOL_OPERATION,
                                            "status": "confirmed",
                                            "result": {"requestId": NATIVE_REQUEST, "actionSummary": "Rome founded."},
                                        }
                                    ]
                                },
                                "call2",
                            )
                        ]
                    ),
                    ModelResponse(parts=[ToolCallPart("finish_role", {"arguments": {}}, "call1")]),
                    ModelRequest(parts=[ToolReturnPart("finish_role", {"roleFinished": True}, "call1")]),
                    ModelResponse(parts=[TextPart("done")]),
                ]
                steps = backend(store, create=True)
                transcript = pointer(scope.identity("fixture-history", "player_role_v1"), messages)
                await steps.register_run(
                    RunRecord(
                        run_id=transcript.run_id,
                        agent_name="player_role_v1",
                        conversation_id=scope.identity("conversation", "role"),
                        metadata={"scope": canonical(scope.model_dump(mode="json")), "modelStepOffset": "7"},
                    )
                )
                await steps.save_snapshot(
                    ContinuableSnapshot(
                        run_id=transcript.run_id, step_index=0, messages=messages, agent_name="player_role_v1"
                    )
                )
            else:
                transcript = await save_history(store, scope, messages, agent="player_role_v1", conversation="role")
                steps = backend(store)
            await steps.append_event(
                StepEvent(
                    run_id=transcript.run_id,
                    step_index=3 if native_command else 1,
                    kind="tool_call_started",
                    tool_call_id="call1",
                )
            )
            await steps.record_tool_effect(
                ToolEffectRecord(
                    run_id=transcript.run_id,
                    tool_call_id="call1",
                    tool_name="finish_role",
                    status="completed",
                    idempotency_key=operation_identity("job1", "e1", 10, "call1") if native_command else "batch1",
                )
            )
            if native_command:
                await steps.append_event(
                    StepEvent(run_id=transcript.run_id, step_index=2, kind="tool_call_started", tool_call_id="call2")
                )
                await steps.record_tool_effect(
                    ToolEffectRecord(
                        run_id=transcript.run_id,
                        tool_call_id="call2",
                        tool_name="execute_commands",
                        status="completed",
                        idempotency_key=operation_identity("job1", "e1", 9, "call2"),
                    )
                )
            reference = await seal(store, scope, transcript, agent="player_role_v1")
            with store.engine.begin() as db:
                db.exec_driver_sql("INSERT INTO generation_attempts VALUES('model1', ?)", ("scope",))
            return reference
        finally:
            store.close()

    history = asyncio.run(persist())
    result = GameRunResult(
        game="game",
        player="p0",
        handoffs=1,
        turn=1,
        roles=[
            RoleResult(
                scope=scope,
                job="job1",
                purpose="gameplay",
                status="COMPLETED",
                history=history,
                usage={"requests": 1},
            )
        ],
    )
    write_private_json(
        tmp_path / "manifest.json", {"project": "isolated", "hostImage": "host", "workerImage": "worker"}
    )
    write_private_json(tmp_path / "control/run.json", {"request": request.model_dump(mode="json")})
    write_private_json(tmp_path / "results/game-result.json", result.model_dump(mode="json"))
    write_private_json(
        tmp_path / "host/checkpoint.json",
        {
            "epoch": "e1",
            "handoff": 1,
            "gameJson": json.dumps({"turns": 1}),
            "players": {
                "Rome": {
                    "receipts": [
                        {
                            "request": {"requestId": "r1", "kind": "END_TURN"},
                            "receipt": {"requestId": "r1", "kind": "END_TURN"},
                        }
                    ]
                }
            },
        },
    )
    write_private_json(tmp_path / "control-wait.log", {"historyReplay": "passed"})
    for index in range(2):
        write_private_json(tmp_path / f"results/history-{index}.json", {})
    notebook = tmp_path / "coordinator/notebook/memory.sqlite3"
    notebook.parent.mkdir(parents=True)
    engine = sqlite_engine(notebook)
    try:
        with engine.begin() as db:
            tables.metadata.create_all(db)
    finally:
        engine.dispose()
    with sqlite3.connect(notebook) as db:
        if not native_command:
            db.execute("INSERT INTO decision_journal VALUES('batch1', 'job1', 'e1', 'gameplay', '{}')")
        db.execute(
            "INSERT INTO outbox(id, job, transport, payload, result, decision) VALUES(?, 'job1', ?, ?, ?, 'decision1')",
            (
                "r1",
                "player_command",
                json.dumps({"requestId": "r1", "kind": "END_TURN"}),
                json.dumps({"requestId": "r1", "kind": "END_TURN"}),
            ),
        )
        db.execute("INSERT INTO jobs VALUES('job1', 'e1', 'gameplay', '{}', '{}', 'COMPLETED', 0)")
        archive = {"retired": True}
        write_private(
            tmp_path / "coordinator/notebook/native-archives" / (digest(archive) + ".json.gz"),
            gzip.compress(json.dumps(archive).encode()),
        )
        db.executemany(
            "INSERT INTO meta VALUES (?, ?)",
            [
                (
                    "nativeGatewayJob:job1",
                    json.dumps(
                        {
                            "archive": digest(archive),
                            "identity": {"scope": scope.model_dump(mode="json"), "job": "job1", "purpose": "gameplay"},
                        }
                    ),
                ),
                ("nativeTurn:turn1", json.dumps({"lifecycleFinalized": True, "bootstraps": {}})),
            ],
        )
        if native_command:
            payload = {
                "requestId": NATIVE_REQUEST,
                "epoch": "e1",
                "revision": "revision1",
                "kind": "FOUND_CITY",
                "target": "unit1",
            }
            receipt = {
                "requestId": NATIVE_REQUEST,
                "epoch": "e1",
                "kind": "FOUND_CITY",
                "actionSummary": "Rome founded.",
            }
            body = {
                "native": {
                    "operationId": NATIVE_REQUEST,
                    "transport": "player_command",
                    "requestSha256": digest(["player_command", payload]),
                    "receiptSha256": digest(receipt),
                    "toolOperationId": NATIVE_TOOL_OPERATION,
                },
                "turn": 0,
                "intent": None,
                "_pinned": {},
                "command": payload,
                "outcome": "confirmed",
                "facts": {"actionSummary": "Rome founded."},
                "subjects": ["unit1"],
            }
            db.execute(
                "INSERT INTO records VALUES(?, 1, 'decision', ?, NULL, 'job1')", (NATIVE_RECORD, json.dumps(body))
            )
            db.execute("INSERT INTO heads VALUES(?, 1)", (NATIVE_RECORD,))
            path = tmp_path / "host/checkpoint.json"
            checkpoint = json.loads(path.read_text())
            checkpoint["players"]["Rome"]["receipts"].append({"request": payload, "receipt": receipt})
            write_private_json(path, checkpoint)
    return tmp_path


def test_audit_matches_native_state_receipts_and_durable_budgets(tmp_path):
    root = artifacts(tmp_path)
    report = verify_game_artifacts(root)
    assert report["verified"] and report["nativeCommands"] == {"END_TURN": 1}
    assert report["simulatedGenerations"] == 1 and report["realProviderCalls"] == 0


def test_audit_matches_new_native_commands_and_legacy_handoff_without_a_python_command_journal(tmp_path):
    root = artifacts(tmp_path, native_command=True)
    with sqlite3.connect(root / "coordinator/notebook/memory.sqlite3") as db:
        assert db.execute("SELECT id FROM outbox").fetchall() == [("r1",)]
        assert db.execute("SELECT count(*) FROM calls").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM decision_journal").fetchone()[0] == 0
    report = verify_game_artifacts(root)
    assert report["verified"] and report["nativeCommands"] == {"END_TURN": 1, "FOUND_CITY": 1}
    assert report["nativeCommandsWithoutPythonJournal"] == 1
    assert report["linkedNativeToolEffects"] == 2 and report["upstreamAgentRuns"] == 1


@pytest.mark.parametrize(
    "damage",
    [
        "request_hash",
        "receipt_hash",
        "request",
        "host_receipt",
        "tool",
        "effect",
        "metadata_scope",
        "offset",
        "step",
        "remove_offset",
        "coherent_scope",
        "record_job",
        "outcome",
        "coherent_request_id",
        "coherent_choice",
        "extra_wire_field",
    ],
)
def test_native_attribution_audit_rejects_changed_host_binding_and_upstream_effect_context(tmp_path, damage):
    root = artifacts(tmp_path, native_command=True)
    notebook = root / "coordinator/notebook/memory.sqlite3"
    checkpoint_path = root / "host/checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text())
    with sqlite3.connect(notebook) as db:
        body = json.loads(db.execute("SELECT body FROM records WHERE key=?", (NATIVE_RECORD,)).fetchone()[0])
        if damage == "request_hash":
            body["native"]["requestSha256"] = "0" * 64
        elif damage == "receipt_hash":
            body["native"]["receiptSha256"] = "0" * 64
        elif damage == "request":
            body["command"]["target"] = "unit2"
            body["native"]["requestSha256"] = digest(["player_command", body["command"]])
        elif damage == "host_receipt":
            checkpoint["players"]["Rome"]["receipts"][1]["receipt"]["actionSummary"] = "Another result"
        elif damage == "tool":
            body["native"]["toolOperationId"] = operation_identity("job1", "e1", 9, "unobserved-call") + ":0"
        elif damage == "coherent_scope":
            body["command"]["epoch"] = "another-epoch"
            checkpoint["players"]["Rome"]["receipts"][1]["request"]["epoch"] = "another-epoch"
            checkpoint["players"]["Rome"]["receipts"][1]["receipt"]["epoch"] = "another-epoch"
            body["native"]["requestSha256"] = digest(["player_command", body["command"]])
            body["native"]["receiptSha256"] = digest(checkpoint["players"]["Rome"]["receipts"][1]["receipt"])
        elif damage == "record_job":
            db.execute("UPDATE records SET job='another-job' WHERE key=?", (NATIVE_RECORD,))
        elif damage == "outcome":
            body["outcome"] = "rejected"
        elif damage in {"coherent_choice", "extra_wire_field"}:
            field, value = ("target", "unit2") if damage == "coherent_choice" else ("confirmAction", True)
            body["command"][field] = value
            checkpoint["players"]["Rome"]["receipts"][1]["request"][field] = value
            body["native"]["requestSha256"] = digest(["player_command", body["command"]])
        elif damage == "coherent_request_id":
            body["command"]["requestId"] = body["native"]["operationId"] = "invented-id"
            checkpoint["players"]["Rome"]["receipts"][1]["request"]["requestId"] = "invented-id"
            checkpoint["players"]["Rome"]["receipts"][1]["receipt"]["requestId"] = "invented-id"
            body["native"]["requestSha256"] = digest(["player_command", body["command"]])
            body["native"]["receiptSha256"] = digest(checkpoint["players"]["Rome"]["receipts"][1]["receipt"])
        db.execute("UPDATE records SET body=? WHERE key=?", (json.dumps(body), NATIVE_RECORD))
    write_private_json(checkpoint_path, checkpoint)
    with sqlite3.connect(root / "player-store/agent-steps.sqlite3") as db:
        if damage == "effect":
            db.execute("UPDATE tool_effects SET idempotency_key='another-effect' WHERE tool_call_id='call2'")
        elif damage in {"metadata_scope", "offset", "remove_offset"}:
            metadata = json.loads(db.execute("SELECT metadata FROM runs").fetchone()[0])
            if damage == "metadata_scope":
                metadata["scope"] = canonical({**json.loads(metadata["scope"]), "player": "other-player"})
            elif damage == "offset":
                metadata["modelStepOffset"] = "8"
            else:
                metadata.pop("modelStepOffset")
            db.execute("UPDATE runs SET metadata=?", (json.dumps(metadata),))
        elif damage == "step":
            db.execute("UPDATE events SET step_index=3 WHERE tool_call_id='call2'")
    with pytest.raises(ValueError, match="VERIFICATION_FAILED"):
        verify_game_artifacts(root)
    report = json.loads((root / "results/verification.json").read_text())
    assert not report["verified"]


@pytest.mark.parametrize(
    "damage",
    [
        "receipt",
        "receipt_content",
        "duplicate",
        "budget",
        "position",
        "archive",
        "context",
        "pending",
        "crash",
        "host",
        "effect",
    ],
)
def test_audit_rejects_missing_receipts_reset_budgets_and_changed_native_position(tmp_path, damage):
    root = artifacts(tmp_path)
    if damage == "effect":
        with sqlite3.connect(root / "player-store/agent-steps.sqlite3") as db:
            db.execute("UPDATE tool_effects SET idempotency_key='unconfirmed'")
    elif damage == "archive":
        next((root / "coordinator/notebook/native-archives").glob("*.gz")).write_bytes(b"corrupt")
    elif damage in {"context", "pending"}:
        with sqlite3.connect(root / "coordinator/notebook/memory.sqlite3") as db:
            if damage == "context":
                db.execute(
                    "INSERT INTO decision_journal VALUES('extra', 'job1', 'e1', 'gameplay', ?)",
                    (json.dumps({"gatewayState": {}}),),
                )
            else:
                db.execute("INSERT INTO calls VALUES('job1', 'pending', 'act', '{}', NULL)")
    elif damage == "crash":
        path = root / "manifest.json"
        manifest = json.loads(path.read_text())
        write_private_json(path, {**manifest, "crashTest": True})
        write_private_json(root / "crashes.json", [])
        for suffix, marker in (("coordinator", "COORDINATOR-READY"), ("player", "PLAYER-WORKER-READY")):
            write_private(root / ("isolated-" + suffix + ".log"), ((marker + "\n") * 2).encode())
    elif damage == "host":
        path = root / "manifest.json"
        write_private_json(path, {**json.loads(path.read_text()), "hostRestart": True})
        write_private_json(
            root / "host-restart.json",
            {
                "hostExitCode": 137,
                "pythonContainersRetained": False,
                "receiptsRetained": True,
                "epoch": "e1",
                "afterEpoch": "e1",
                "handoff": 1,
                "afterHandoff": 1,
            },
        )
        write_private(root / "isolated-coordinator.log", b"HOST-RECONNECTING\nHOST-RECONNECTED\n")
    elif damage == "budget":
        with sqlite3.connect(root / "player-store/workflow.sqlite3") as db:
            db.execute("DELETE FROM generation_attempts")
    else:
        path = root / "host/checkpoint.json"
        checkpoint = json.loads(path.read_text())
        if damage == "receipt":
            checkpoint["players"]["Rome"]["receipts"].clear()
        elif damage == "receipt_content":
            checkpoint["players"]["Rome"]["receipts"][0]["receipt"]["kind"] = "FOUND_CITY"
        elif damage == "duplicate":
            checkpoint["players"]["Rome"]["receipts"] *= 2
        else:
            checkpoint["handoff"] = 2
        write_private_json(path, checkpoint)
    with pytest.raises(ValueError, match="VERIFICATION_FAILED"):
        verify_game_artifacts(root)
    assert not json.loads((root / "results/verification.json").read_text())["verified"]
