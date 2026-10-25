"""Content fidelity, durable delivery and role briefs retain action safeguards."""

import asyncio
import copy

import pytest
from jsonschema import Draft202012Validator

from civ_agent.economy_view import economy_overview
from civ_agent.feedback_view import compact_feedback, expand_feedback
from civ_agent.memory_records import EconomyMemoryUpdate, MemoryUpdate
from civ_agent.payload_updates import (
    apply_memory_receipt,
    apply_value,
    compact_memory_receipt,
    detail_response,
    encoded,
)
from civ_agent.protocol import JsonObject
from civ_agent.startup_context import decision_queries, prepare_startup
from civ_agent.state_updates import apply_overview_update, overview_update, stamp
from civ_agent.tool_schemas import tool_model
from tests.test_economy_optimization import managed


@pytest.mark.parametrize(
    "after",
    [
        {"rows": [{"id": "b", "value": True}, {"id": "a", "value": 1}]},
        {"rows": []},
        {"rows": [{"id": "a", "value": False}, {"id": "b", "value": True}]},
        {"rows": [{"id": "a", "value": 1}, {"id": "b", "value": True}]},
        {"new/~field": {"value": None}},
    ],
)
def test_detail_roundtrip_preserves_types_order_and_removals(after):
    before = {"rows": [{"id": "a", "value": True}, {"id": "b", "value": True}], "reference": "Stable fact " * 300}
    after = {**after, "reference": before["reference"]}
    first = detail_response(None, {"data": before}, "scope", 1, "query")
    state = apply_value(None, first["detailUpdate"], first["data"])
    reply = detail_response(state, {"data": after, "evidence": "new-evidence"}, "scope", 2, "query")
    restored = apply_value(state, reply["detailUpdate"], reply.get("data"))
    assert encoded(restored["value"]) == encoded(after)
    assert "new-evidence" in encoded(reply)
    assert apply_value(restored, first["detailUpdate"], first["data"]) == restored
    assert apply_value(restored, reply["detailUpdate"], reply.get("data")) == restored
    assert len(encoded(reply)) < len(encoded(after)) / 2
    with pytest.raises(ValueError, match="FULL_PAYLOAD_REQUIRED"):
        apply_value(None, reply["detailUpdate"])
    other = apply_value(None, detail_response(None, {"data": before}, "other", 1, "query")["detailUpdate"], before)
    with pytest.raises(ValueError, match="FULL_PAYLOAD_REQUIRED"):
        apply_value(other, reply["detailUpdate"])


def test_role_schema_retains_every_city_plan_operation_and_omits_forbidden_types():
    schema = tool_model("memory_update", "economy").model_json_schema()
    assert tool_model("memory_update", "economy") is EconomyMemoryUpdate
    assert len(encoded(schema)) < len(encoded(MemoryUpdate.model_json_schema())) / 2
    assert "Strategy" not in schema.get("$defs", {})
    for changes in [
        [{"op": "create", "content": {"type": "city_plan", "city": "city1", "reasons": {"growth": "Feed population"}}}],
        [{"op": "amend", "id": "mem1", "fields": {"reviewTurn": 8}}],
        [{"op": "retire", "id": "mem1", "reason": "completed"}],
    ]:
        Draft202012Validator(schema).validate({"changes": changes})
    assert not Draft202012Validator(schema).is_valid(
        {"changes": [{"op": "create", "content": {"type": "strategy", "assessment": "Forbidden"}}]}
    )


def test_feedback_facts_roundtrip_and_small_updates_do_not_repeat_history():
    facts = {"timeline": "Observed result " * 100}
    original = {
        "choices": [
            {"id": "a", "observedChanges": facts, "reviewDue": False},
            {"id": "b", "observedChanges": facts, "reviewDue": False},
        ]
    }
    compact = compact_feedback(original)
    assert expand_feedback(compact) == original
    assert len(encoded(compact)) < len(encoded(original)) * 0.7
    before = stamp({"decisionFeedback": compact}, "job", "epoch", "r1")
    changed = copy.deepcopy(compact)
    changed["choices"][1]["reviewDue"] = True
    after = stamp({"decisionFeedback": changed}, "job", "epoch", "r2")
    update = overview_update(before, after, [])
    assert update["patch"] and "Observed result" not in encoded(update)
    assert apply_overview_update(before, update) == after


def test_preloads_follow_available_choices_and_keep_other_required_inspections():
    unit: JsonObject = {
        "handle": "u1",
        "promotions": {"experience": 4},
        "upgrades": [{"available": False}],
        "inspections": {
            "PROMOTE_UNIT": {"tool": "unit_details", "arguments": {"target": "u1", "sections": ["promotions"]}},
            "UPGRADE_UNIT": {"tool": "unit_details", "arguments": {"target": "u1", "sections": ["economy"]}},
            "FOUND_CITY": {"tool": "unit_details", "arguments": {"target": "u1", "sections": ["settlement"]}},
        },
    }
    assert decision_queries({"units": [unit]}) == [("unit_details", {"target": "u1", "sections": ["settlement"]})]
    unit["promotions"]["choices"] = [{"available": True, "name": "Drill I"}]
    unit["upgrades"][0]["available"] = True
    assert set(decision_queries({"units": [unit]})[0][1]["sections"]) == {"promotions", "economy", "settlement"}


def test_economy_brief_retains_native_constraints_and_does_not_imply_missing_rosters_are_empty():
    full = {
        "economy": {"availableGold": 20, "committedGold": 480, "happiness": -1},
        "alerts": [{"text": "Food shortage"}],
        "tacticalReview": {"visibleHostiles": ["Brute"]},
        "units": [
            {"handle": "m1", "name": "Warrior", "military": True},
            {"handle": "w1", "name": "Worker", "military": False, "workerJobTurnsRemaining": 2},
        ],
        "settlement": {"newCityHappiness": {"after": -3}, "candidates": [{"position": "site"}]},
        "recentProgress": {"old": "history"},
    }
    saved = copy.deepcopy(full)
    social = {"agreements": [{"terms": "Supply a defensive unit"}]}
    brief = economy_overview(full, social)
    assert brief["economy"] == full["economy"] and brief["alerts"] == full["alerts"]
    assert brief["tacticalReview"] == full["tacticalReview"]
    assert brief["settlement"]["newCityHappiness"] == {"after": -3}
    assert brief["economyBrief"]["militaryUnitCount"] == 1
    assert brief["economyBrief"]["agreements"]["agreements"] == social["agreements"]
    assert brief["units"][0]["workerJobTurnsRemaining"] == 2
    assert economy_overview(brief, social) == brief and full == saved


def test_memory_receipts_reconstruct_authoritative_records_and_keep_partial_failures():
    content = {"type": "city_plan", "city": "c1", "reasons": {"growth": "Good rationale " * 20}}
    requests = [{"op": "create", "content": content}, {"op": "amend", "id": "missing", "fields": {"priority": 2}}]
    record = {**content, "id": "mem1", "state": "active", "priority": 3, "confirmedActions": {}}
    original = {
        "changes": [
            {"accepted": True, "id": "mem1", "version": 1, "record": record},
            {"accepted": False, "id": "missing", "reason": "UNKNOWN_MEMORY"},
        ]
    }
    compact = compact_memory_receipt(original, requests, {})
    assert "recordUpdate" in compact["changes"][0]
    assert compact["changes"][1] == original["changes"][1]
    assert apply_memory_receipt(requests, compact, {}) == original
    amend = [{"op": "amend", "id": "mem1", "fields": {"priority": 4}}]
    amended = {"changes": [{"accepted": True, "id": "mem1", "version": 2, "record": {**record, "priority": 4}}]}
    delta = compact_memory_receipt(amended, amend, {"mem1": record})
    assert apply_memory_receipt(amend, delta, {"mem1": record}) == amended
    with pytest.raises(ValueError, match="FULL_RECORD_REQUIRED"):
        apply_memory_receipt(amend, delta, {})
    # Two edits to one record use the preceding successful result, including a
    # reversion to its original value. A failed edit cannot become a baseline.
    batch = [amend[0], {"op": "amend", "id": "mem1", "fields": {"priority": 3}}]
    batch_result = {"changes": [amended["changes"][0], {**original["changes"][0], "version": 3}]}
    compact_batch = compact_memory_receipt(batch_result, batch, {"mem1": record})
    assert apply_memory_receipt(batch, compact_batch, {"mem1": record}) == batch_result


def test_gateway_revalidates_unchanged_details_resets_baselines_and_keeps_full_reads(tmp_path):
    async def run():
        async with managed(tmp_path) as m:
            prepared = await prepare_startup(m.gateway, m.economy)
            assert "chat_read" not in prepared["reads"] and "victory_progress" not in prepared["reads"]
            assert prepared["reads"]["player_turn"]["economyBrief"]["fullRead"]["arguments"] == {"view": "full"}
            m.gateway.commit_startup_coordination(m.economy)
            args = {"target": "city2"}
            first = await m.gateway.call(m.economy, "city_details", args, "d1")
            state = apply_value(None, first["detailUpdate"], first["data"])
            m.economy.details_read.clear()
            second = await m.gateway.call(m.economy, "city_details", args, "d2")
            assert second["detailUpdate"]["kind"] == "unchanged" and "data" not in second
            assert m.economy.details_read  # Revalidated evidence still registers the inspection.
            assert apply_value(state, second["detailUpdate"])["value"] == first["data"]
            full = await m.gateway.call(m.economy, "city_details", {**args, "full": True}, "d3")
            assert full["data"] == first["data"]
            m.gateway.reset_coordination(m.economy)
            fresh = await m.gateway.call(m.economy, "city_details", args, "d4")
            assert fresh["detailUpdate"]["kind"] == "full" and fresh["data"] == first["data"]
            scope = m.economy.coordination_scope
            assert await m.gateway.call(m.economy, "city_details", args, "d2") == second
            assert m.economy.coordination_scope == scope
            recovery = await m.gateway.call(m.economy, "player_turn", {}, "recovery")
            assert "economyBrief" in recovery and m.economy.overview_view == "economy"
            complete = await m.gateway.call(m.economy, "player_turn", {"view": "full"}, "complete")
            assert "economyBrief" not in complete and m.economy.overview_view == "full"
            later = await m.gateway.call(m.economy, "player_turn", {}, "later")
            assert "economyBrief" not in later and m.economy.overview_view == "full"

    asyncio.run(run())


def test_memory_receipt_and_delivery_baseline_survive_interruption_without_repeating_an_edit(tmp_path):
    async def run():
        async with managed(tmp_path) as m:
            await prepare_startup(m.gateway, m.economy)
            m.gateway.commit_startup_coordination(m.economy)
            plan = next(record for record in m.economy.memory_seen.values() if record.get("type") == "city_plan")
            args = {"changes": [{"op": "amend", "id": plan["id"], "fields": {"reasons": {"growth": "Grow while fed"}}}]}
            before = copy.deepcopy(m.economy.memory_seen)
            original = m.memory.call
            fail = True

            def commit(job, call_id, name, arguments, result=None):
                nonlocal fail
                if call_id == "edit" and result is not None and fail:
                    fail = False
                    raise OSError("Interrupted outer reply")
                return original(job, call_id, name, arguments, result)

            m.memory.call = commit
            with pytest.raises(OSError, match="Interrupted outer reply"):
                await m.gateway.call(m.economy, "memory_update", args, "edit")
            assert m.economy.memory_seen == before
            committed = original(m.economy.id, "edit", "memory_update", args)
            assert committed is not None and committed["changes"][0]["accepted"]
            result = await m.gateway.call(m.economy, "memory_update", args, "edit")
            assert result == committed
            restored = apply_memory_receipt(args["changes"], result, before)["changes"][0]["record"]
            assert m.economy.memory_seen[plan["id"]] == restored
            version = result["changes"][0]["version"]
            assert await m.gateway.call(m.economy, "memory_update", args, "edit") == result
            assert m.economy.memory_versions[plan["id"]] == version
            assert m.memory.memory_edit_counts(m.economy.id)["accepted"] == 1

    asyncio.run(run())
