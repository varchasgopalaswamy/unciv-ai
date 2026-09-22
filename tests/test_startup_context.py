import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from civ_agent.civilization_state import CivilizationState, StateConflict
from civ_agent.game_commands import command_arguments
from civ_agent.player_prompts import job_instructions
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.protocol import JsonObject
from civ_agent.service_io import ServiceIO
from civ_agent.startup_context import (
    core_definitions,
    prepare_startup,
    startup_instructions,
    startup_prompt,
    startup_text,
)
from civ_agent.state_updates import apply_overview_update
from civ_agent.strategy_guide import strategy_reference
from civ_agent.telemetry import JobAccounting
from tests.test_agent_service import StartupHost
from tests.test_structured_memory import create, plan


def test_prepared_reads_establish_evidence_and_update_base_without_model_calls(tmp_path):
    async def run():
        io = ServiceIO()
        memory = CivilizationState(tmp_path / "memory", "Rome")
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        gateway.accounting = JobAccounting(tmp_path / "accounting")
        host = StartupHost(
            SimpleNamespace(service=SimpleNamespace(active_id="job", records={"job": {"epoch": "g1", "gameTurn": 0}}))
        )
        host.view["alerts"] = [{"handle": "alert1", "canAcknowledge": True, "content": {"text": "A message"}}]
        host.view["map"]["units"][0]["canFoundCity"] = True
        try:
            job = await gateway.open("job", "gameplay", "g1", host, {}, 0)
            await io.call(gateway.accounting.start, "job", "g1", True)
            prepared = await prepare_startup(gateway, job)
            assert set(prepared["reads"]) == {"player_turn", "player_capabilities", "victory_progress", "chat_read"}
            view = prepared["reads"]["player_turn"]
            assert view == job.delivered_overview
            assert job.social_read and job.verified and not job.details_read
            assert "observation:overview" in job.memory["records"]
            assert prepared["reads"]["player_capabilities"]["version"]
            assert "revision" not in view and "epoch" not in view
            assert "stateId" in view
            assert {tool["name"] for tool in prepared["coreTools"]} == {
                "mcp__civ__player_turn",
                "mcp__civ__player_command",
                "mcp__civ__memory_update",
                "mcp__civ__memory_read",
                "mcp__civ__unit_details",
                "mcp__civ__map_region",
            }
            for tool in prepared["coreTools"]:
                Draft202012Validator.check_schema(tool["inputSchema"])
            assert "social promises" in prepared["readInstructions"]["chat_read"]
            assert json.loads(startup_text(prepared).split("\n")[-1]) == prepared["reads"]
            action = view["alerts"][0]["command"]
            assert action == {"kind": "ACKNOWLEDGE_ALERT", "target": "alert1"}
            result = await gateway.call(job, "player_command", action, "ack")
            assert apply_overview_update(view, result["stateUpdate"]) == job.delivered_overview
            assert len(host.executed) == 1
            assert view["endTurn"]["command"] == {"kind": "END_TURN"}
            assert "deferReason" not in view["endTurn"]["command"]
            assert "deferReason is optional" in view["endTurn"]["instruction"]
            settlement = view["units"][0]["inspections"]["FOUND_CITY"]
            args = {"kind": "FOUND_CITY", "target": "unit1", "intent": {"reason": "Settle here"}}
            rejection = await gateway.call(job, "player_command", args, "uninspected")
            assert rejection["recovery"]["reads"] == [settlement]
            await gateway.call(job, settlement["tool"], settlement["arguments"], "settlement")
            assert "error" not in await gateway.call(job, "player_command", args, "found")
            assert len(host.executed) == 2
            await io.call(gateway.accounting.sample, {"activationId": "job", "phase": "RUNNING"})
            calls = gateway.accounting.pending()[0]["telemetry"]["apiCalls"]
            assert calls["startup:player_turn"]["calls"] == 1
            assert "tool:player_turn" not in calls
            assert calls["tool:player_command"]["calls"] == 3
            assert memory.call("job", "startup:player_turn", "player_turn", {}) == view
        finally:
            await io.close()

    asyncio.run(run())


def test_communication_startup_cannot_prepare_gameplay_or_expose_its_commands(tmp_path):
    async def run():
        io = ServiceIO()
        memory = CivilizationState(tmp_path / "memory", "Rome")
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = StartupHost(
            SimpleNamespace(service=SimpleNamespace(active_id="job", records={"job": {"epoch": "g1", "gameTurn": 0}}))
        )
        try:
            job = await gateway.open("job", "communication", "g1", host, {})
            prepared = await prepare_startup(gateway, job)
            assert set(prepared["reads"]) == {"chat_read"}
            assert job.observation is None
            assert {tool["name"] for tool in prepared["coreTools"]} == {
                "mcp__civ__chat_read",
                "mcp__civ__chat_send",
                "mcp__civ__memory_update",
            }
            assert "player_command" not in json.dumps(prepared)
        finally:
            await io.close()

    asyncio.run(run())


@pytest.mark.parametrize("purpose", ["gameplay", "communication"])
def test_private_startup_context_follows_observation_reviews_and_expiry(tmp_path, purpose):
    async def run():
        memory, io = CivilizationState(tmp_path / "memory", "Rome"), ServiceIO()
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        memory.bind("g1")
        memory.start_job("previous", "gameplay", {})
        memory.observe_player({"turn": 0}, "previous")
        key = create(memory, plan(reviewAfterTurn=1), job="previous")
        expiring = create(memory, {"type": "note", "text": "Temporary report", "expiresTurn": 1}, job="previous")
        memory.finish_job("previous", True)
        host = StartupHost(
            SimpleNamespace(service=SimpleNamespace(active_id="job", records={"job": {"epoch": "g1", "gameTurn": 1}}))
        )
        original = host.tool

        async def at_turn_one(name, args):
            result: JsonObject = await original(name, args)
            if name == "player_communication" and args["op"] == "state":
                result["context"] = {"turn": 1}
            return result

        host.tool = at_turn_one
        try:
            job = await gateway.open("job", purpose, "g1", host, {}, 1)
            assert not job.memory["records"][key]["body"]["needsReview"]
            prepared = await prepare_startup(gateway, job)
            context = json.loads(prepared["memoryContext"].split("\n", 1)[1])
            records = {record["id"]: record for record in context["records"]}
            assert records[key]["needsReview"]
            assert records[expiring]["state"] == "retired"
            assert "memoryUpdates" not in prepared["reads"].get("player_turn", {})
            prompt = startup_prompt(prepared, purpose, "Play this turn")
            assert prompt.user.count(prepared["memoryContext"]) == 1
            assert "Temporary report" not in prompt.instructions
            assert startup_text(prepared).count(prepared["memoryContext"]) == 1
            saved = memory.call(
                job.id,
                "startup:memory:0",
                "memory_read",
                {"ids": job.memory["contextIds"], "includeRetired": True},
            )
            assert saved is not None and saved["records"] == context["records"]
            result = await gateway.call(
                job,
                "memory_update",
                {"changes": [{"op": "revise", "id": key, "content": plan(reviewAfterTurn=5)}]},
                "review",
            )
            assert result["changes"][0]["accepted"]
        finally:
            await io.close()

    asyncio.run(run())


def test_startup_rejects_foreign_identity(tmp_path):
    async def run():
        io = ServiceIO()
        gateway = ProposalGateway(CivilizationState(tmp_path / "memory", "Rome"), io, tmp_path / "workspace")
        host = StartupHost(
            SimpleNamespace(service=SimpleNamespace(active_id="job", records={"job": {"epoch": "g1", "gameTurn": 0}}))
        )
        host.civilization = "Greece"
        try:
            job = await gateway.open("job", "gameplay", "g1", host, {})
            with pytest.raises(StateConflict, match="PLAYER_CREDENTIAL_MISMATCH"):
                await prepare_startup(gateway, job)
            assert job.observation is None
        finally:
            await io.close()

    asyncio.run(run())


def test_core_definitions_keep_complete_schemas_and_do_not_mutate_catalogue():
    schema = {
        "type": "object",
        "properties": {"x": {"type": "integer"}},
        "required": ["x"],
        "additionalProperties": False,
    }
    catalogue = [
        {"name": "player_command", "description": "Execute", "inputSchema": schema, "outputSchema": {"type": "object"}}
    ]
    before = copy.deepcopy(catalogue)
    assert core_definitions(catalogue, "gameplay") == [
        {"name": "mcp__civ__player_command", "description": "Execute", "inputSchema": schema}
    ]
    assert catalogue == before
    assert command_arguments({"kind": "END_TURN", "deferReason": "Stay fortified"})["deferReason"] == "Stay fortified"


@pytest.mark.parametrize("strategy_guide", [False, True])
def test_prompt_prefix_excludes_state_memory_identity_and_capability_changes(strategy_guide):
    prepared: JsonObject = {
        "coreTools": [{"name": "mcp__civ__player_command", "inputSchema": {"type": "object"}}],
        "readInstructions": {"chat_read": "Statements are claims."},
        "reads": {
            "player_capabilities": {"version": "v1", "value": {"commands": ["END_TURN"]}},
            "player_turn": {"turn": 1, "civilization": "Rome", "stateId": "private-state-1"},
            "chat_read": {"text": "UNTRUSTED_MESSAGE_IGNORE_INSTRUCTIONS"},
        },
    }
    before = copy.deepcopy(prepared)
    first = startup_prompt(prepared, "gameplay", "Rome turn 1; MEMORY_SENTINEL", strategy_guide=strategy_guide)
    changed = copy.deepcopy(prepared)
    changed["reads"] = {
        "player_capabilities": {"error": "TEMPORARY_ERROR"},
        "player_turn": {"turn": 2, "civilization": "Greece", "stateId": "private-state-2"},
        "chat_read": {"text": "SECOND_PRIVATE_MESSAGE"},
    }
    second = startup_prompt(changed, "gameplay", "Greece turn 2; OTHER_MEMORY", strategy_guide=strategy_guide)
    assert first.instructions == second.instructions
    assert first.metadata()["instructionsSha256"] == second.metadata()["instructionsSha256"]
    assert first.user != second.user
    for private in ("MEMORY_SENTINEL", "UNTRUSTED_MESSAGE_IGNORE_INSTRUCTIONS", "private-state-1"):
        assert private not in first.instructions
        assert first.user.count(private) == 1
        assert private not in second.user
    assert first.user.index("Prepared capabilities") < first.user.index("MEMORY_SENTINEL")
    assert first.user.index("MEMORY_SENTINEL") < first.user.index("private-state-1")
    assert "inputSchema" in first.instructions and "inputSchema" not in first.user
    assert "TEMPORARY_ERROR" in second.user
    changed["coreTools"][0]["inputSchema"]["required"] = ["kind"]
    third = startup_prompt(changed, "gameplay", "Greece turn 2", strategy_guide=strategy_guide)
    assert third.metadata()["instructionsSha256"] != first.metadata()["instructionsSha256"]
    assert prepared == before


@pytest.mark.parametrize("purpose", ["gameplay", "communication"])
def test_strategy_reference_is_optional_once_and_before_dynamic_context(tmp_path, monkeypatch, purpose):
    # The reference must be available to installed services outside the checkout.
    monkeypatch.chdir(tmp_path)
    reference = strategy_reference()
    prepared = {
        "coreTools": [],
        "readInstructions": {},
        "memoryContext": "PRIVATE_MEMORY_SENTINEL",
        "reads": {"chat_read": {"text": "PRIVATE_CHAT_SENTINEL"}},
    }
    baseline = startup_prompt(prepared, purpose, "Play this turn")
    enabled = startup_prompt(prepared, purpose, "Play this turn", strategy_guide=True)
    assert baseline.instructions == job_instructions(purpose) + "\n\n" + startup_instructions(prepared)
    assert baseline.metadata()["strategyGuide"] == {"enabled": False, "characters": 0, "sha256": None}
    assert enabled.instructions == baseline.instructions
    assert enabled.user.count(reference) == 1
    assert enabled.user.endswith(baseline.user)
    assert enabled.user.index(reference) < enabled.user.index("PRIVATE_MEMORY_SENTINEL")
    assert enabled.metadata()["strategyGuide"]["enabled"]
    assert enabled.metadata()["strategyGuide"]["characters"] == len(reference)
    assert enabled.metadata()["strategyGuide"]["sha256"]
    assert enabled.metadata()["instructionCharacters"] == baseline.metadata()["instructionCharacters"]
    assert enabled.metadata()["instructionsSha256"] == baseline.metadata()["instructionsSha256"]
    interactive = startup_text(prepared, strategy_guide=True)
    assert interactive == reference + "\n\n" + startup_text(prepared)
    assert interactive.count(reference) == 1
    assert interactive.index(reference) < interactive.index("PRIVATE_MEMORY_SENTINEL")
    assert json.loads(interactive.split("\n")[-1]) == prepared["reads"]


def test_prefix_is_deterministic_and_communication_has_its_own_workflow():
    first = {"coreTools": [], "readInstructions": {"b": "B", "a": "A"}, "reads": {"chat_read": {}}}
    second = {"reads": {"chat_read": {}}, "readInstructions": {"a": "A", "b": "B"}, "coreTools": []}
    one = startup_prompt(first, "communication", "Rome")
    two = startup_prompt(second, "communication", "Greece")
    assert one.instructions == two.instructions
    assert "Avoid duplicate proposals" in one.instructions
    assert "After END_TURN succeeds" not in one.instructions
    assert one.metadata()["instructionCharacters"] == len(one.instructions)
