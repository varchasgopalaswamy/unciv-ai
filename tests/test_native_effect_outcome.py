"""Native receipts reduce into domain facts, without another dispatch journal."""

import asyncio
import copy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from civ_agent import memory_schema as tables
from civ_agent.civilization_state import CivilizationState, StateConflict
from civ_agent.economy_roles import TurnRoles
from civ_agent.memory_store import RecordStore, encode
from civ_agent.orchestration.role_state import capture_roles, restore_roles
from civ_agent.protocol import JsonObject


def setup(tmp_path):
    memory = CivilizationState(tmp_path / "private", "Rome")
    memory.bind("epoch1")
    plan = {
        "type": "city_plan",
        "city": "city1",
        "state": "active",
        "createdTurn": 4,
        "updatedTurn": 4,
        "reasons": {},
        "reviewTurn": 9,
        "purchaseTargets": [],
    }
    with memory.transaction() as db:
        RecordStore(db).write("plan1", "city_plan", plan)
    memory.start_job("job1", "gameplay", {}, game_turn=4)
    memory.refresh_memory("job1", ["plan1"])
    prepared: JsonObject = {
        "operationId": "request1",
        "transport": "player_command",
        "payload": {
            "requestId": "request1",
            "epoch": "epoch1",
            "revision": "original-revision",
            "kind": "BUY_CONSTRUCTION",
            "target": "city1",
            "selection": "Granary",
        },
        "turn": 4,
        "intent": {"cityPlan": "plan1", "reason": "Improve food surplus"},
        "initiativeDecisions": [],
    }
    receipt: JsonObject = {
        "requestId": "request1",
        "epoch": "epoch1",
        "kind": "BUY_CONSTRUCTION",
        "turn": 4,
        "revision": "after-revision",
        "actionSummary": "Granary purchased.",
        "economy": {"goldChange": -180, "units": [], "rawDetails": "NOT_A_DOMAIN_FACT"},
        "rawExtra": "NOT_A_CACHED_RESPONSE",
    }
    return memory, prepared, receipt


def domain(memory, key="native:request1"):
    with memory.transaction() as db:
        return RecordStore(db).all(keys=[key], project=False).get(key)


def record(memory, key="native:request1"):
    value = domain(memory, key)
    assert value is not None
    return value


def counts(memory):
    with memory.transaction() as db:
        return {
            table.name: db.scalar(select(func.count()).select_from(table))
            for table in (tables.records, tables.outbox, tables.calls, tables.decision_journal)
        }


def test_native_outcome_reduces_once_without_caching_the_host_reply_or_revising_authored_plan(tmp_path):
    memory, prepared, receipt = setup(tmp_path)
    before = record(memory, "plan1")
    assert memory.record_native_outcome("job1", prepared, receipt) == {"replayed": False}
    first_counts = counts(memory)
    assert memory.record_native_outcome("job1", prepared, receipt) == {"replayed": True}
    assert counts(memory) == first_counts
    assert all(first_counts[name] == 0 for name in ("outbox", "calls", "decision_journal"))
    saved = record(memory)
    assert saved["version"] == 1 and saved["body"]["outcome"] == "confirmed"
    assert saved["body"]["native"]["receiptSha256"] == hashlib.sha256(encode(receipt).encode()).hexdigest()
    assert saved["body"]["facts"] == {"actionSummary": "Granary purchased.", "economy": {"goldChange": -180}}
    assert "NOT_A_" not in json.dumps(saved)
    assert "receipt" not in saved["body"] and "result" not in saved["body"] and "status" not in saved["body"]
    assert domain(memory, "plan1") == before
    assert memory.confirmed_city_actions() == {
        "plan1": {
            "BUY_CONSTRUCTION": {
                "receipt": "native:request1",
                "turn": 4,
                "planVersion": before["version"],
                "reason": "Improve food surplus",
                "selection": "Granary",
                "actionSummary": "Granary purchased.",
            }
        }
    }
    assert not memory.confirmed_city_actions(keys=["other-plan"])
    with memory.transaction() as db:
        facts = memory._action_facts(db, 4, 4)
    assert len(facts) == 1 and facts[0]["action"] == "Granary purchased."
    assert facts[0]["economy"] == {"goldChange": -180}
    assert facts[0]["_pinned"]["cityPlan"]["key"] == "plan1"


@pytest.mark.parametrize("changed", ["payload", "receipt", "intent", "toolOperationId", "job"])
def test_native_outcome_rejects_changed_identity_arguments_receipt_or_attribution(tmp_path, changed):
    memory, prepared, receipt = setup(tmp_path)
    prepared["toolOperationId"] = "parent:0"
    memory.record_native_outcome("job1", prepared, receipt)
    prior = counts(memory)
    job = "job1"
    if changed == "payload":
        prepared["payload"]["selection"] = "Library"
    elif changed == "receipt":
        receipt["actionSummary"] = "Different receipt"
    elif changed == "intent":
        prepared["intent"]["reason"] = "Different attribution"
    elif changed == "toolOperationId":
        prepared["toolOperationId"] = "other-parent:0"
    else:
        memory.start_job("job2", "economy", {}, game_turn=4)
        job = "job2"
    with pytest.raises(StateConflict, match="NATIVE_OUTCOME_CONFLICT"):
        memory.record_native_outcome(job, prepared, receipt)
    assert counts(memory) == prior


def test_native_outcome_replays_after_job_completion_but_never_applies_a_new_stale_outcome(tmp_path):
    memory, prepared, receipt = setup(tmp_path)
    memory.record_native_outcome("job1", prepared, receipt)
    memory.finish_job("job1", True)
    assert memory.record_native_outcome("job1", prepared, receipt) == {"replayed": True}
    fresh = copy.deepcopy(prepared)
    fresh["operationId"] = fresh["payload"]["requestId"] = "request2"
    with pytest.raises(StateConflict, match="STALE_JOB"):
        memory.record_native_outcome("job1", fresh, {**receipt, "requestId": "request2"})
    with memory.transaction() as db:
        memory._set(db, "epoch", "new-epoch")
    with pytest.raises(StateConflict, match="STALE_JOB"):
        memory.record_native_outcome("job1", prepared, receipt)
    assert domain(memory, "native:request2") is None


@pytest.mark.parametrize("error", ["NOT_ENACTED", "COMMUNICATION_UNAVAILABLE", "INTERNAL_ERROR"])
def test_uncertain_host_outcomes_never_create_domain_decisions(tmp_path, error):
    memory, prepared, _ = setup(tmp_path)
    prior = counts(memory)
    with pytest.raises(StateConflict, match="RECEIPT_UNCERTAIN"):
        memory.record_native_outcome("job1", prepared, {"error": error})
    assert counts(memory) == prior


def test_rejected_native_outcome_records_no_confirmed_city_action_or_investment(tmp_path):
    memory, prepared, _ = setup(tmp_path)
    result = memory.record_native_outcome("job1", prepared, {"error": {"code": "OPTION_UNAVAILABLE", "raw": "omitted"}})
    assert result == {"replayed": False}
    assert record(memory)["body"]["facts"] == {"error": "OPTION_UNAVAILABLE"}
    assert not memory.confirmed_city_actions()
    with memory.transaction() as db:
        assert memory._action_facts(db, 4, 4)[0]["commandStatus"] == "rejected"


def test_native_attribution_remains_nonblocking_and_pins_only_delivered_sources(tmp_path):
    memory, prepared, receipt = setup(tmp_path)
    prepared["intent"] = {
        "reason": "Consider an unverified plan",
        "cityPlan": "invented-plan",
        "strategy": "invented-strategy",
    }
    result = memory.record_native_outcome("job1", prepared, receipt)
    assert result["replayed"] is False
    assert result["intentFeedback"]["linked"] is False
    assert result["intentFeedback"]["unverifiedCityPlan"] == "invented-plan"
    assert record(memory)["body"]["outcome"] == "confirmed"
    assert not memory.confirmed_city_actions()


def test_native_trade_notice_and_attribution_roll_back_together_then_retry_once(tmp_path, monkeypatch):
    memory, prepared, _ = setup(tmp_path)
    with memory.transaction() as db:
        RecordStore(db).write("strategy1", "strategy", {"state": "active", "subjects": ["Greece"]})
    prepared["transport"] = "player_communication"
    prepared["payload"] = {"requestId": "request1", "epoch": "epoch1", "op": "trade_decide", "proposal": "trade1"}
    prepared["intent"] = None
    receipt = {
        "trade": {"proposal": "trade1", "status": "ENACTED", "author": "Rome", "partner": "Greece"},
        "outcome": {"gold": -20},
    }
    prior = counts(memory)
    original = RecordStore.flag

    def fail(*args, **kwargs):
        raise RuntimeError("projection failed")

    monkeypatch.setattr(RecordStore, "flag", fail)
    with pytest.raises(RuntimeError, match="projection failed"):
        memory.record_native_outcome("job1", prepared, receipt)
    assert counts(memory) == prior and domain(memory) is None
    monkeypatch.setattr(RecordStore, "flag", original)
    memory.record_native_outcome("job1", prepared, receipt)
    first = counts(memory)
    assert record(memory, "notice:strategy1")["version"] == 1
    assert memory.record_native_outcome("job1", prepared, receipt) == {"replayed": True}
    assert counts(memory) == first


def test_post_native_observation_delivery_can_skip_the_calls_journal(tmp_path):
    from tests.test_construction_estimates import snapshot

    memory, _, _ = setup(tmp_path)
    view = snapshot().view
    view["epoch"], view["turn"] = "epoch1", 4
    observed, result = memory.deliver_player(
        view,
        "job1",
        {"turn": 4},
        "native-post-action",
        "player_command",
        {},
        lambda records, changed: {"postAction": True},
        record_call=False,
    )
    assert observed and result == {"postAction": True}
    assert counts(memory)["calls"] == 0


def test_later_trade_settlement_updates_domain_facts_without_changing_the_original_receipt_binding(tmp_path):
    memory, prepared, _ = setup(tmp_path)
    prepared.update(
        transport="player_communication",
        payload={"requestId": "request1", "epoch": "epoch1", "op": "trade_offer", "partner": "Greece"},
        intent=None,
    )
    pending = {"trade": {"proposal": "trade1", "status": "PENDING", "mode": "SHARED"}}
    memory.record_native_outcome("job1", prepared, pending)
    first = record(memory)
    settled = {"trade": {"proposal": "trade1", "status": "ENACTED", "mode": "SHARED"}, "outcome": {"raw": "not-copied"}}
    memory.observe("trade/trade1", settled, "job1")
    later = record(memory)
    assert later["version"] == first["version"] + 1
    assert later["body"]["native"] == first["body"]["native"]
    assert later["body"]["settlement"] == {
        "trade": {"proposal": "trade1", "status": "ENACTED", "mode": "SHARED"},
        "outcome": True,
    }
    with memory.transaction() as db:
        assert memory._action_facts(db, 4, 4)[0]["action"] == "Engine agreement enacted"
    assert memory.record_native_outcome("job1", prepared, pending) == {"replayed": True}
    assert record(memory) == later
    assert counts(memory)["outbox"] == 0


def test_native_attention_causes_survive_role_replacement_and_preserve_new_attention():
    async def run():
        service = SimpleNamespace(_save=AsyncMock(), records={})
        record = {"epoch": "epoch1", "gameTurn": 4, "activationId": "attempt1"}
        roles = TurnRoles(service, record)
        roles.ready["gameplay"] = True
        roles.economy_wait_record = {"id": "waiting-for-economy"}
        job = SimpleNamespace(observation={"cities": [], "alerts": [{"kind": "CAPTURED_CIVILIAN"}]})
        await roles.after_action(job, {"kind": "ATTACK"}, {}, effect_id="native-action1")
        assert roles.main_attention == 1
        checkpoint = capture_roles(roles)
        replacement = TurnRoles(service, copy.deepcopy(record))
        restore_roles(replacement, checkpoint)
        await replacement.after_action(job, {"kind": "ATTACK"}, {}, effect_id="native-action1")
        assert replacement.main_attention == 1
        await replacement.after_action(job, {"kind": "ATTACK"}, {}, effect_id="native-action2")
        assert replacement.main_attention == 2
        assert len(replacement.record["nativeAttentionCauses"]) == 2

    asyncio.run(run())
