"""The isolated routing exercise requires native text, budgets and crash evidence."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from civ_agent.orchestration.contracts import WorkflowLink, digest
from civ_agent.orchestration.game_verification import verify_routing_artifacts
from civ_agent.tls import write_private_json


def routing_artifacts(root):
    original = "Preserve this private authored claim exactly."
    response = "Acknowledged m1: " + original
    message = {"message": "m1", "text": original, "participants": ["France", "Rome"]}
    write_private_json(
        root / "communication-fixture.json", {"request": {"text": original}, "receipt": {"message": message}}
    )
    write_private_json(root / "advisory-broker/provider-count.json", {"calls": 2})
    crashes = [
        {
            "point": "routing-reply",
            "exitCode": 137,
            "brokerReplyPersisted": True,
            "workerReplyPersisted": True,
            "incomingRow": 1,
            "incomingDeadline": 130,
            "requestId": digest("incoming"),
        },
        {
            "point": "routing-delivery",
            "exitCode": 137,
            "nativeReceiptPresent": True,
            "outcomeUnrecorded": True,
            "outgoingText": response,
            "nativeRequestId": "send1",
        },
    ]
    write_private_json(root / "routing-crashes.json", crashes)
    checkpoint = {
        "communication": {
            "requests": {
                "Rome:send1": {
                    "response": {
                        "message": {
                            "author": "Rome",
                            "text": response,
                            "participants": ["France", "Rome"],
                            "public": False,
                        }
                    },
                    "audit": {"handoff": 1},
                }
            }
        }
    }
    result = SimpleNamespace(
        workflow_links=[
            WorkflowLink.model_validate({"id": digest([kind, index]), "kind": kind})
            for index, kind in enumerate(["routing", "routing", "outgoing-draft", "outgoing-delivery"])
        ]
    )
    with sqlite3.connect(root / "advisory-broker/decisions.sqlite3") as db:
        db.executescript(
            "CREATE TABLE routing_requests(id TEXT,result TEXT,input_tokens INTEGER); CREATE TABLE requests(id TEXT)"
        )
        for direction in ("incoming", "outgoing"):
            identifier = digest(direction)
            receipt = {
                "requestId": identifier,
                "direction": direction,
                "choice": "batch",
                "fallback": None,
                "replayEvidence": {"pending": [{"text": original}], "proposedMessage": {"text": response}},
            }
            db.execute("INSERT INTO routing_requests VALUES(?,?,?)", (identifier, json.dumps(receipt), 10))
    notebook = root / "coordinator/notebook/memory.sqlite3"
    notebook.parent.mkdir(parents=True)
    with sqlite3.connect(notebook) as db:
        db.executescript(
            "CREATE TABLE meta(key TEXT,value TEXT); CREATE TABLE records(key TEXT,version INTEGER,category TEXT,body TEXT);"
            "CREATE TABLE heads(key TEXT,version INTEGER)"
        )
        db.execute(
            "INSERT INTO meta VALUES('routing:events',?)", (json.dumps({"1": {"deadline": 130, "completed": True}}),)
        )
        for link in result.workflow_links:
            db.execute("INSERT INTO meta VALUES(?,?)", ("nativeRoutingLink:" + link.id, link.model_dump_json()))
        db.execute(
            "INSERT INTO records VALUES('delivery1',1,'delivery',?)",
            (json.dumps({"status": "sent", "payload": {"text": response}, "requestId": "send1"}),),
        )
        db.execute("INSERT INTO heads VALUES('delivery1',1)")
    return result, checkpoint


def test_routing_acceptance_matches_broker_native_receipts_deadlines_and_workflow_histories(tmp_path):
    result, checkpoint = routing_artifacts(tmp_path)
    report = verify_routing_artifacts(tmp_path, result, checkpoint)
    assert report == {
        "verified": True,
        "routingRequests": 2,
        "routingProviderCalls": 2,
        "routingInputTokens": 20,
        "routingWorkflows": 4,
    }


@pytest.mark.parametrize(
    "damage",
    [
        "duplicate_send",
        "renewed_deadline",
        "unknown",
        "missing_usage",
        "extra_provider",
        "missing_workflow",
        "changed_text",
        "missing_crash",
    ],
)
def test_routing_acceptance_rejects_incomplete_or_changed_recovery_evidence(tmp_path, damage):
    result, checkpoint = routing_artifacts(tmp_path)
    if damage == "duplicate_send":
        checkpoint["communication"]["requests"]["Rome:duplicate"] = checkpoint["communication"]["requests"][
            "Rome:send1"
        ]
    elif damage == "missing_workflow":
        result.workflow_links.pop()
    elif damage == "extra_provider":
        write_private_json(tmp_path / "advisory-broker/provider-count.json", {"calls": 3})
    elif damage == "missing_crash":
        path = tmp_path / "routing-crashes.json"
        crashes = json.loads(path.read_text())
        crashes[1]["outcomeUnrecorded"] = False
        write_private_json(path, crashes)
    elif damage in {"unknown", "missing_usage"}:
        with sqlite3.connect(tmp_path / "advisory-broker/decisions.sqlite3") as db:
            if damage == "missing_usage":
                db.execute("UPDATE routing_requests SET input_tokens=NULL")
            else:
                identifier, encoded = db.execute("SELECT id,result FROM routing_requests LIMIT 1").fetchone()
                db.execute(
                    "UPDATE routing_requests SET result=? WHERE id=?",
                    (json.dumps({**json.loads(encoded), "pending": True}), identifier),
                )
    else:
        with sqlite3.connect(tmp_path / "coordinator/notebook/memory.sqlite3") as db:
            if damage == "renewed_deadline":
                db.execute(
                    "UPDATE meta SET value=? WHERE key='routing:events'",
                    (json.dumps({"1": {"deadline": 140, "completed": True}}),),
                )
            else:
                db.execute(
                    "UPDATE records SET body=?",
                    (
                        json.dumps(
                            {"status": "sent", "payload": {"text": "Changed authored message"}, "requestId": "send1"}
                        ),
                    ),
                )
    assert not verify_routing_artifacts(tmp_path, result, checkpoint)["verified"]
