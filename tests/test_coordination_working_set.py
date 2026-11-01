"""History survives bounded, role-scoped delivery and explicit lifecycle changes."""

import copy
import json
from types import SimpleNamespace
from typing import cast

import pytest

from civ_agent.agent_service import AgentService
from civ_agent.civilization_state import CivilizationState
from civ_agent.coordination_context import MAX_COORDINATION_CHARACTERS, encoded, working_context
from civ_agent.decision_contracts import DecisionContract
from civ_agent.decisions_policy import DecisionsSettings
from civ_agent.war_state import WarStore


@pytest.fixture
def wars(tmp_path):
    memory = CivilizationState(tmp_path, "Rome")
    memory.bind("epoch")
    yield WarStore(memory)
    memory.engine.dispose()


def context():
    return {
        "rolesFinished": {"gameplay": False, "military": False, "economy": False},
        "requests": [
            {
                "id": "supply",
                "status": "pending",
                "author": "military",
                "priority": "urgent",
                "text": "Keep funding the defense",
            }
        ],
        "cityOverrides": {"city1": "Emergency recruitment"},
        "warState": {
            "episodes": {
                "war1": {
                    "id": "war1",
                    "active": True,
                    "version": 2,
                    "goals": [{"outcome": "Defend the frontier; costs remain open to discussion"}],
                    "history": [{"reason": "OLD_GOAL " * 1000}],
                    "proposals": {},
                }
            },
            "drafts": {},
            "escorts": {},
            "operations": {
                f"turn{i}": {"id": f"turn{i}", "plan": "OLD_PLAN " * 150, "progress": "Unassessed", "status": "legacy"}
                for i in range(60)
            },
            "messages": [
                {
                    "id": f"msg{i}",
                    "author": "military",
                    "to": "gameplay",
                    "text": f"Historical report {i} " + "FACT " * 100,
                }
                for i in range(100)
            ],
        },
    }


@pytest.mark.parametrize("role", ["gameplay", "economy", "military"])
def test_bounded_startup_preserves_current_state_and_exposes_omissions(role):
    full = context()
    original = copy.deepcopy(full)
    value = working_context(full, role)
    assert len(encoded(value)) < MAX_COORDINATION_CHARACTERS
    assert full == original
    assert value["requests"][0]["id"] == "supply"
    assert value["history"]["legacyOperations"] == 60
    assert "OLD_GOAL" not in encoded(value) and "OLD_PLAN" not in encoded(value)
    if role == "gameplay":
        assert value["history"]["omitted"]["messages"] > 90
        assert value["warState"]["messages"]
    else:
        assert not value["warState"]["messages"]
    if role in {"gameplay", "military"}:
        assert value["warState"]["episodes"]["war1"]["goals"] == full["warState"]["episodes"]["war1"]["goals"]
    else:
        assert not value["warState"]["episodes"]


def test_oversized_current_goals_and_pending_requests_are_retrievable_not_truncated(wars):
    full = context()
    full["warState"]["episodes"]["war1"]["goals"] = [{"outcome": "Exact ambiguous wording " * 1000}]
    full["requests"] *= 100
    with wars.memory.transaction() as db:
        wars.memory._set(db, "war:state", full["warState"])
    projected = working_context(full, "gameplay")
    assert len(encoded(projected)) < MAX_COORDINATION_CHARACTERS
    assert projected["history"]["omitted"]["episodes"] == 1
    assert projected["history"]["omitted"]["requests"] > 0
    page = wars.read("gameplay", {"section": "episodes", "ids": ["war1"]})
    assert page["oversizedRecord"]
    assert page["items"][0]["goals"] == full["warState"]["episodes"]["war1"]["goals"]
    page = wars.read("gameplay", {"section": "requests", "cursor": 90, "limit": 10}, full["requests"])
    assert len(page["items"]) == 10 and page["nextCursor"] is None


def test_legacy_migration_requires_explicit_reconciliation_and_retains_every_note(wars):
    legacy = context()["warState"]
    for operation in legacy["operations"].values():
        del operation["status"]
    with wars.memory.transaction() as db:
        wars.memory._set(db, "war:state", legacy)
    state = wars.state()
    assert all(op["status"] == "legacy" for op in state["operations"].values())
    found, cursor = [], 0
    while True:
        page = wars.read("military", {"section": "operations", "status": "legacy", "cursor": cursor})
        found.extend(page["items"])
        if page["nextCursor"] is None:
            break
        cursor = page["nextCursor"]
    assert len(found) == 60
    args = {"action": "set_status", "ids": ["turn0"], "status": "active", "reason": "This plan still applies"}
    assert wars.apply("epoch", "military", "military_operation", args, "reconcile")["status"] == "active"
    active = wars.read("military", {})["operations"]
    assert active["turn0"]["plan"] == legacy["operations"]["turn0"]["plan"]
    previous = wars.read("military", {"section": "operation_history", "ids": ["turn0"]})
    assert previous["items"][0]["status"] == "legacy"


def test_stable_operation_updates_supersession_and_retries_preserve_revision_history(wars):
    args = {"id": "frontier", "plan": "Hold the river", "progress": "Staging", "reconsiderWhen": "Enemy withdraws"}
    first = wars.apply("epoch", "military", "military_operation", args, "start", 1)
    assert wars.apply("epoch", "military", "military_operation", args, "start", 1) == first
    wars.apply("epoch", "military", "military_operation", {**args, "progress": "In position"}, "update", 2)
    assert len(wars.state()["operations"]) == 1
    assert wars.state()["operations"]["frontier"]["revision"] == 2
    replacement = {**args, "id": "fallback", "supersedes": ["frontier"]}
    wars.apply("epoch", "military", "military_operation", replacement, "replace", 3)
    assert set(wars.read("military", {})["operations"]) == {"fallback"}
    assert wars.state()["operations"]["frontier"]["status"] == "superseded"
    assert len(wars.read("military", {"section": "operation_history", "ids": ["frontier"]})["items"]) == 2


def test_acknowledgement_survives_reopening_but_never_resolves_an_open_request(wars):
    args = {"to": "economy", "kind": "request", "text": "Can we fund reinforcements?"}
    message = wars.apply("epoch", "military", "role_message", args, "send")["message"]
    ack = {"action": "acknowledge", "ids": [message["id"]]}
    assert wars.apply("epoch", "gameplay", "role_message", ack, "wrong-role")["error"] == "MESSAGE_RECIPIENT_REQUIRED"
    wars.apply("epoch", "economy", "role_message", ack, "ack")
    reopened = WarStore(wars.memory)
    assert reopened.read("economy", {})["messages"][0]["id"] == message["id"]
    assert reopened.read("military", {})["messages"][0]["id"] == message["id"]
    result = reopened.apply(
        "epoch",
        "economy",
        "role_message",
        {"action": "resolve", "ids": [message["id"]], "reason": "A Spearman is queued"},
        "resolve",
    )
    assert result["action"] == "resolve"
    assert not reopened.read("economy", {})["messages"]
    history = reopened.read("economy", {"section": "messages", "status": "resolved"})
    assert history["items"][0]["text"] == args["text"]


def test_acknowledged_notices_do_not_repeat_on_next_activation(wars):
    sent = wars.apply("epoch", "military", "role_message", {"to": "gameplay", "text": "Patrol finished"}, "notice")
    wars.apply("epoch", "gameplay", "role_message", {"action": "acknowledge", "ids": [sent["message"]["id"]]}, "ack")
    assert not WarStore(wars.memory).read("gameplay", {})["messages"]
    assert wars.read("gameplay", {"section": "messages"})["items"][0]["text"] == "Patrol finished"


@pytest.mark.parametrize("action", ["acknowledge", "resolve"])
def test_request_id_confusion_returns_valid_recovery_without_changing_sibling_messages(wars, action):
    message = wars.apply(
        "epoch", "gameplay", "role_message", {"to": "economy", "kind": "request", "text": "Review production"}, "send"
    )["message"]
    before = wars.state()
    arguments = {"action": action, "ids": [message["id"], "request451109b9b2"]}
    if action == "resolve":
        arguments["reason"] = "Production assessed"
    result = wars.apply("epoch", "economy", "role_message", arguments, "confused")
    assert result["error"] == "UNKNOWN_ROLE_MESSAGE"
    assert result["unknownIds"] == ["request451109b9b2"]
    assert "arguments.requests" in result["instruction"]
    assert result["applied"] is False and wars.state() == before
    contract = DecisionContract("economy", [{"name": "war_goals"}])
    recovery = contract.parse(contract.encode({"kind": "inspect", "operations": result["recovery"]["reads"]}))
    page = wars.read("economy", recovery["operations"][0]["arguments"])
    assert page["items"] == [message]
    corrected = {**arguments, "ids": [message["id"]]}
    receipt = wars.apply("epoch", "economy", "role_message", corrected, "corrected")
    assert receipt["messageIds"] == [message["id"]]
    assert wars.apply("epoch", "economy", "role_message", corrected, "corrected") == receipt


@pytest.mark.parametrize("action", ["acknowledge", "resolve"])
def test_wrong_recipient_reports_ownership_and_does_not_partially_change_messages(wars, action):
    incoming = wars.apply(
        "epoch", "military", "role_message", {"to": "gameplay", "kind": "request", "text": "Arrange an escort"}, "in"
    )["message"]
    outgoing = wars.apply(
        "epoch", "gameplay", "role_message", {"to": "military", "kind": "request", "text": "Assess readiness"}, "out"
    )["message"]
    before = wars.state()
    arguments = {"action": action, "ids": [incoming["id"], outgoing["id"]]}
    if action == "resolve":
        arguments["reason"] = "Reviewed"
    result = wars.apply("epoch", "gameplay", "role_message", arguments, "wrong-role")
    assert result["error"] == "MESSAGE_RECIPIENT_REQUIRED"
    assert result["recipients"] == {incoming["id"]: "gameplay", outgoing["id"]: "military"}
    assert "addressed recipient" in result["instruction"]
    assert result["applied"] is False and wars.state() == before


def test_control_status_does_not_copy_authored_history_or_mutate_durable_record():
    record = {
        "activationId": "a",
        "phase": "INTERRUPTED",
        "usage": {},
        "roles": {"ready": {}, "warState": context()["warState"]},
    }
    original = copy.deepcopy(record)
    service = SimpleNamespace(
        active_id="a",
        records={"a": record},
        finalization_tasks={},
        finalization_errors={},
        gateway=SimpleNamespace(jobs={}),
        decisions_settings=DecisionsSettings(),
    )
    status = AgentService.status(cast(AgentService, service), "a")
    assert len(json.dumps({"status": status}).encode()) < 65536
    assert "Historical report" not in json.dumps(status) and "OLD_PLAN" not in json.dumps(status)
    assert status["roles"]["counts"]["messages"] == 100
    assert record == original


def test_service_startup_migrates_an_idle_players_store_before_any_model_activation(tmp_path):
    import asyncio

    async def run():
        root = tmp_path / "service"
        root.mkdir(mode=0o700)
        memory = CivilizationState(root / "civilization", "Rome")
        with memory.transaction() as db:
            memory._set(
                db, "war:state", {"operations": {"old": {"id": "old", "plan": "Keep this exact note"}}, "messages": []}
            )
        memory.engine.dispose()
        service = AgentService("Rome", tmp_path / "workspace", root, "S" * 43, "https://localhost:8765/mcp", None)
        try:
            assert service.active_id is None
            with service.memory.transaction() as db:
                state = service.memory._meta(db, "war:state")
            assert state["schemaVersion"] == 2
            assert state["operations"]["old"]["status"] == "legacy"
            assert state["operations"]["old"]["plan"] == "Keep this exact note"
        finally:
            await service.close()

    asyncio.run(run())
