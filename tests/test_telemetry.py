import asyncio
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from civ_agent.civilization_state import CivilizationState
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.service_io import ServiceIO
from civ_agent.telemetry import JobAccounting, merge_model_usage


def record(id="job", **changes):
    return {
        "activationId": id,
        "phase": "RUNNING",
        "usage": {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 30, "reasoning_output_tokens": 10},
        "usageReported": True,
        "usageComplete": False,
        "modelRequests": 2,
        "effectiveSettings": {"model": "gpt-5.6-luna", "reasoningEffort": "medium"},
        **changes,
    }


@pytest.mark.parametrize("role_state", ["finished", "waiting_negotiation", "waiting_economy"])
def test_suspended_role_reports_match_the_native_request_contract(tmp_path, role_state):
    store = JobAccounting(tmp_path)
    store.start("role-job", "epoch1", True)
    store.sample(record("role-job", purpose="economy", roleState=role_state))
    report = store.pending()[0]
    schema = Path(__file__).resolve().parents[1] / "src/main/resources/uncivai/schemas/communication.json"
    validator = Draft202012Validator(json.loads(schema.read_text()))
    validator.validate(
        {
            "op": "job_report",
            "epoch": "epoch1",
            "job": report["job"],
            "telemetry": report["telemetry"],
            "usage": report["usage"],
        }
    )
    assert report["telemetry"]["endedAtMs"] is None


def test_role_wait_time_is_distinct_from_negotiation_and_monotonic(tmp_path):
    store = JobAccounting(tmp_path)
    store.start("role-job", "epoch1", True)
    store.sample(record("role-job", roleState="waiting_economy", roleWaitSeconds=2))
    report = store.pending()[0]["telemetry"]
    assert report["phase"] == "WAITING_ROLE"
    assert report["roleWaitMs"] == 2000 and report["negotiationWaitMs"] == 0
    store.sample(record("role-job", roleWaitSeconds=1))
    report = store.pending()[0]["telemetry"]
    assert report["phase"] == "RUNNING" and report["roleWaitMs"] == 2000


def test_reports_survive_lost_acknowledgement_and_do_not_drop_newer_counts(tmp_path):
    store = JobAccounting(tmp_path)
    store.start("job", "epoch1", True)
    store.sample(record())
    first = store.pending()[0]
    store.sample(record(phase="COMPLETED", usageComplete=True))
    store.acknowledge("job", first["telemetry"]["sequence"])
    final = store.pending()[0]
    assert final["telemetry"]["sequence"] > first["telemetry"]["sequence"]
    restored = JobAccounting(tmp_path)
    assert restored.pending() == [final]
    restored.acknowledge("job", final["telemetry"]["sequence"])
    assert restored.pending() == []
    assert final["usage"]["input_tokens"] + final["usage"]["output_tokens"] == 130
    assert final["telemetry"]["usageComplete"]
    assert final["telemetry"]["modelRequests"] == 2


def test_payload_character_counts_survive_restart_without_becoming_provider_tokens(tmp_path):
    from civ_agent.coordination_updates import payload_characters

    store = JobAccounting(tmp_path)
    store.start("job", "epoch1", True)
    reply = {"economyCoordination": {"warState": {"messages": [{"id": "m1", "text": "Private history"}]}}}
    for _ in range(2):
        call = store.begin_call("job", "city_details", {})
        store.end_call(call, "ok", False, payload_characters(reply))
    store.sample(record(phase="COMPLETED", usageComplete=True))
    report = JobAccounting(tmp_path).pending()[0]
    assert report["payloadCharacters"]["responses"] == 2
    assert report["payloadCharacters"]["characters"] == 2 * payload_characters(reply)["characters"]
    assert "payloadCharacters" not in report["telemetry"]
    assert report["usage"]["input_tokens"] == 100
    assert "Private history" not in json.dumps(report)


def test_cumulative_usage_counts_new_responses_but_not_duplicate_notifications():
    fields = {
        "input_tokens": "inputTokens",
        "cached_input_tokens": "cachedInputTokens",
        "output_tokens": "outputTokens",
        "reasoning_output_tokens": "reasoningOutputTokens",
    }
    job = {"usage": dict.fromkeys(fields, 0), "modelRequests": 0}
    first = {"inputTokens": 100, "cachedInputTokens": 80, "outputTokens": 20, "reasoningOutputTokens": 5}
    assert merge_model_usage(job, first, fields)
    assert merge_model_usage(job, first, fields)
    second = {**first, "inputTokens": 250, "outputTokens": 35}
    assert merge_model_usage(job, second, fields)
    assert job["modelRequests"] == 2
    assert job["usage"]["input_tokens"] == 250


def test_model_telemetry_uses_effective_settings_and_survives_restart(tmp_path):
    store = JobAccounting(tmp_path)
    store.start("job", "epoch1", True)
    effective = {"provider": "openrouter", "model": "test/resolved-model", "reasoningEffort": "high"}
    store.sample(
        record(
            phase="COMPLETED",
            settings={"model": "requested-alias", "reasoningEffort": "high"},
            effectiveSettings=effective,
            usageComplete=True,
        )
    )
    restored = JobAccounting(tmp_path)
    telemetry = restored.pending()[0]["telemetry"]
    assert {key: telemetry[key] for key in effective} == effective


def test_service_crash_preserves_spending_and_uncertain_call(tmp_path):
    store = JobAccounting(tmp_path)
    store.start("job", "epoch1", True)
    store.sample(record())
    store.begin_call("job", "player_command", {"kind": "END_TURN", "secret": "not in telemetry"})
    restored = JobAccounting(tmp_path)
    report = restored.pending()[0]
    assert report["telemetry"]["phase"] == "FAILED"
    assert report["usage"]["input_tokens"] == 100
    assert not report["telemetry"]["usageComplete"]
    assert report["telemetry"]["apiCalls"]["command:END_TURN"]["incomplete"] == 1
    assert "secret" not in json.dumps(report)


def test_missing_usage_and_chat_are_separate_from_completed_gameplay(tmp_path):
    store = JobAccounting(tmp_path)
    store.start("gameplay", "old_epoch", True)
    store.sample(record("gameplay", phase="COMPLETED", usageReported=False, usage={}))
    store.start("chat", "new_epoch", True)
    store.sample(record("chat", phase="COMPLETED", usageComplete=True))
    gameplay, chat = store.pending()
    assert not gameplay["telemetry"]["usageComplete"]
    assert chat["telemetry"]["usageComplete"]
    assert gameplay["epoch"] != chat["epoch"]


def test_gateway_counts_cached_tools_replays_and_rejections(tmp_path):
    class Connection:
        async def close(self):
            pass

        async def list_tools(self):
            return []

        async def tool(self, name, arguments):
            if name == "player_communication" and arguments["op"] == "state":
                return {"civilization": "Rome", "epoch": "epoch1"}
            if name == "player_communication" and arguments["op"] == "negotiation":
                return {"negotiation": {"active": False}}
            raise AssertionError("These tools are entirely local")

    async def run():
        memory = CivilizationState(tmp_path / "memory", "Rome")
        io = ServiceIO()
        try:
            gateway = ProposalGateway(memory, io, tmp_path / "workspace")
            store = JobAccounting(tmp_path / "accounting", memory.memory_edit_counts)
            gateway.accounting = store
            store.start("job", "epoch1", True)
            job = await gateway.open("job", "gameplay", "epoch1", Connection(), {})
            first = await gateway.call(job, "memory_read", {"ids": ["missing"]}, "a")
            assert await gateway.call(job, "memory_read", {"ids": ["missing"]}, "a") == first
            batch = {
                "changes": [
                    {"op": "create", "content": {"type": "note", "text": "PRIVATE_NOTE"}},
                    {"op": "amend", "id": "missing", "fields": {"text": "PRIVATE_CLAIM"}},
                ]
            }
            edited = await gateway.call(job, "memory_update", batch, "batch")
            assert await gateway.call(job, "memory_update", batch, "batch") == edited
            rejected = await gateway.call(job, "unknown_tool", {}, "bad")
            assert rejected == {"error": "TOOL_NOT_PERMITTED", "applied": False}
            store.sample(record(phase="COMPLETED", usageComplete=True))
            calls = store.pending()[0]["telemetry"]["apiCalls"]
            assert calls["tool:memory_read"]["calls"] == 2
            assert calls["tool:memory_read"]["replayed"] == 1
            assert calls["tool:unknown_tool"]["errors"] == 1
            assert calls["tool:memory_update"]["succeeded"] == 2
            assert calls["tool:memory_update"]["errors"] == 0
            assert calls["tool:memory_update"]["replayed"] == 1
            report = store.pending()[0]
            assert report["telemetry"]["memoryEdits"] == {
                "batches": 1,
                "accepted": 1,
                "rejected": 1,
                "rejectedBatches": 1,
                "rejectionReasons": {"UNKNOWN_MEMORY": 1},
                "complete": True,
            }
            assert "PRIVATE" not in json.dumps(report)
            await gateway.close(job, True)
        finally:
            await io.close()

    asyncio.run(run())


def test_memory_counts_recover_lost_responses_and_survive_rollback(tmp_path):
    from tests.domain_fixtures import setup

    memory = setup(tmp_path / "memory")
    store = JobAccounting(tmp_path / "accounting", memory.memory_edit_counts)
    store.start("game", "g1", True)
    saved = tmp_path / "checkpoint.json"
    checksum = memory.backup(saved)
    batch = [{"op": "create", "content": {"type": "note", "text": "PRIVATE"}}]
    store.begin_call("game", "memory_update", {"changes": batch})
    memory.patch("game", batch, "committed")
    # Crash after the memory transaction, before end_call or a telemetry sample.
    restored = JobAccounting(tmp_path / "accounting", memory.memory_edit_counts)
    report = restored.pending()[0]["telemetry"]
    assert report["apiCalls"]["tool:memory_update"]["incomplete"] == 1
    assert report["memoryEdits"]["accepted"] == 1
    assert report["memoryEdits"]["batches"] == 1
    memory.restore(saved, checksum, "g2")
    assert memory.memory_edit_counts("game") == report["memoryEdits"]
    assert memory.memory_edit_counts("other")["batches"] == 0


def test_late_finalization_updates_timing_without_changing_gameplay_usage(tmp_path):
    store = JobAccounting(tmp_path)
    store.start("job", "epoch", True)
    store.sample(record(phase="COMPLETED", usageComplete=True, gameplayElapsedMs=300, finalizationElapsedMs=20))
    before = store.pending()[0]
    store.acknowledge("job", before["telemetry"]["sequence"])
    store.sample(record(phase="COMPLETED", usageComplete=True, gameplayElapsedMs=300, finalizationElapsedMs=75))
    after = store.pending()[0]
    assert after["usage"] == before["usage"]
    assert after["telemetry"]["elapsedMs"] == before["telemetry"]["elapsedMs"]
    assert after["telemetry"]["gameplayElapsedMs"] == 300
    assert after["telemetry"]["finalizationElapsedMs"] == 75


def test_restored_activation_cannot_downgrade_completed_usage(tmp_path):
    store = JobAccounting(tmp_path)
    store.start("job", "original-epoch", True)
    store.sample(record(phase="COMPLETED", usageComplete=True, finalizationElapsedMs=50))
    accepted = store.pending()[0]
    store.acknowledge("job", accepted["telemetry"]["sequence"])
    # Restore retains a pre-completion record and marks its conversation failed.
    store.sample(record(phase="FAILED", usageReported=False, usageComplete=False, usage={}, finalizationElapsedMs=10))
    restored = JobAccounting(tmp_path).pending()[0]
    assert restored["usage"] == accepted["usage"]
    assert restored["epoch"] == accepted["epoch"]
    assert restored["telemetry"]["phase"] == "COMPLETED"
    assert restored["telemetry"]["usageReported"] and restored["telemetry"]["usageComplete"]
    assert restored["telemetry"]["finalizationElapsedMs"] == 50
    assert restored["telemetry"]["sequence"] > accepted["telemetry"]["sequence"]
