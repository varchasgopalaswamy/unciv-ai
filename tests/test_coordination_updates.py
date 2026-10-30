"""Lossless coordination delivery, fresh startup, and durable replay boundaries."""

import asyncio
import copy
import json

import pytest

from civ_agent.coordination_context import working_context
from civ_agent.coordination_updates import apply_coordination_update, content, response, snapshot
from civ_agent.protocol import JsonObject
from civ_agent.startup_context import compact_startup, prepare_startup, startup_prompt
from tests.test_economy_optimization import managed


def context() -> JsonObject:
    return {
        "rolesFinished": {"gameplay": False, "economy": False},
        "requests": [{"id": "req1", "status": "pending", "text": "Protect growth"}],
        "cityOverrides": {},
        "militaryActive": True,
        "warState": {
            "episodes": {"war1": {"version": 1, "goals": ["Defend Rome"]}},
            "escorts": {"unit1": {"civilian": "unit2"}},
            "messages": [{"id": "msg1", "author": "gameplay", "to": "economy", "text": "HISTORY_SENTINEL " * 200}],
        },
    }


@pytest.mark.parametrize("change", ["append", "edit", "remove", "reorder", "goals", "requests", "typed", "empty-war"])
def test_every_coordination_change_reconstructs_exactly(change):
    before = context()
    before["warState"]["messages"].append({"id": "msg2", "text": "Another message"})
    after = copy.deepcopy(before)
    if change == "append":
        after["warState"]["messages"].append({"id": "msg3", "text": "New urgent fact"})
    elif change == "edit":
        after["warState"]["messages"][1]["text"] = "Corrected fact"
    elif change == "remove":
        after["warState"]["messages"] = after["warState"]["messages"][1:]
    elif change == "reorder":
        after["warState"]["messages"].reverse()
    elif change == "goals":
        after["warState"]["episodes"]["war1"] = {"version": 2, "goals": ["Preserve army"]}
        after["warState"]["escorts"] = {}
    elif change == "requests":
        after["requests"][0]["status"] = "completed"
        after["requests"].append({"id": "req2", "status": "pending"})
        after["cityOverrides"]["city1"] = "Emergency"
    elif change == "typed":
        after["militaryActive"] = 1
    else:
        after["warState"] = {}
    a, b = snapshot(before, "job", "epoch", "scope", 1), snapshot(after, "job", "epoch", "scope", 2)
    original = copy.deepcopy((a, b))
    delta = response(a, b)
    assert apply_coordination_update(a, delta) == b
    assert apply_coordination_update(b, delta) == b
    assert (a, b) == original
    assert apply_coordination_update(b, {"economyCoordination": a}) == b
    assert apply_coordination_update(None, response(None, a)) == a
    with pytest.raises(ValueError, match="FULL_COORDINATION_REQUIRED"):
        apply_coordination_update(None, delta)
    if change not in {"remove", "empty-war", "reorder"}:
        assert "HISTORY_SENTINEL" not in json.dumps(delta)
        assert len(json.dumps(delta)) < len(json.dumps(b)) / 2


def test_unchanged_state_emits_nothing_and_scope_is_conversation_and_epoch_bound():
    a = snapshot(context(), "job", "epoch", "scope", 1)
    assert response(a, snapshot(context(), "job", "epoch", "scope", 2)) == {}
    assert response(a, a, full=True) == {"economyCoordination": a}
    for job, epoch, scope in [
        ("new-job", "epoch", "scope"),
        ("job", "new-epoch", "scope"),
        ("job", "epoch", "new-scope"),
    ]:
        assert snapshot(context(), job, epoch, scope, 1)["stateId"] != a["stateId"]
    changed = snapshot({**context(), "militaryActive": False}, "job", "epoch", "scope", 2)
    other = snapshot(context(), "job", "epoch", "other", 1)
    with pytest.raises(ValueError, match="FULL_COORDINATION_REQUIRED"):
        apply_coordination_update(other, response(a, changed))


def test_startup_keeps_one_copy_without_changing_native_or_authored_data():
    state = context()
    prepared: JsonObject = {
        "coreTools": [],
        "readInstructions": {},
        "warState": state["warState"],
        "economyCoordination": snapshot(state, "j", "e", "s", 1),
        "reads": {
            "player_turn": {"turn": 1, "economyCoordination": state},
            "chat_read": {"economyCoordination": state},
        },
        "decisionDetails": [{"economyCoordination": state, "data": {"native": "preserved"}}],
    }
    original = copy.deepcopy(prepared)
    prompt = startup_prompt(prepared, "gameplay", "Play")
    assert prompt.user.count("HISTORY_SENTINEL") == 200
    assert "native" in prompt.user and "preserved" in prompt.user
    assert prompt.metadata()["layoutVersion"] == 11
    assert prompt.metadata()["payloadCharacters"]["coordinationMessages"] > 0
    assert prepared == original
    prepared["decisionDetails"][0]["data"]["economyCoordination"] = state
    compact = compact_startup(prepared)
    assert compact["decisionDetails"][0]["data"]["economyCoordination"] == state
    assert prepared["decisionDetails"][0]["economyCoordination"] == state


def test_startup_captures_late_messages_and_commits_only_when_conversation_starts(tmp_path):
    async def run():
        async with managed(tmp_path) as m:
            original = m.gateway.select_startup_memory

            async def select(*args):
                await original(*args)
                m.roles.war_state["messages"] = [
                    {"id": "late", "author": "gameplay", "to": "economy", "text": "Arrived during startup"}
                ]
                # Synchronize the durable source read at the end of preparation.
                with m.memory.transaction() as db:
                    m.memory._set(db, "war:state", copy.deepcopy(m.roles.war_state))

            m.gateway.select_startup_memory = select
            prepared = await prepare_startup(m.gateway, m.economy)
            assert m.economy.coordination_seen is None
            assert prepared["economyCoordination"]["warState"]["messages"][0]["id"] == "late"
            assert "economyCoordination" not in prepared["reads"]["player_turn"]
            m.gateway.commit_startup_coordination(m.economy)
            assert m.economy.economy_seen["warState"]["messages"][0]["id"] == "late"
            unchanged = await m.gateway.call(m.economy, "city_details", {"target": "city2"}, "detail")
            assert "economyCoordination" not in unchanged and "coordinationUpdate" not in unchanged
            before = m.economy.coordination_seen
            assert before is not None
            m.roles.requests.append(
                {"id": "late-request", "status": "pending", "priority": "urgent", "text": "Inspect"}
            )
            changed = await m.gateway.call(m.economy, "city_details", {"target": "city2"}, "detail2")
            assert "HISTORY_SENTINEL" not in json.dumps(changed)
            assert apply_coordination_update(before, changed) == m.economy.coordination_seen
            restored = copy.deepcopy(m.economy.coordination_seen)
            assert await m.gateway.call(m.economy, "city_details", {"target": "city2"}, "detail") == unchanged
            assert m.economy.coordination_seen == restored
            full = await m.gateway.call(m.economy, "player_turn", {}, "full")
            assert content(full["economyCoordination"]) == working_context(m.roles.context(), "economy")
            m.gateway.reset_coordination(m.economy)
            fresh = await m.gateway.call(m.economy, "city_details", {"target": "city2"}, "new-conversation")
            assert "economyCoordination" in fresh and fresh["economyCoordination"]["scopeId"] != before["scopeId"]

    asyncio.run(run())


def test_failed_response_commit_does_not_acknowledge_or_repeat_a_role_message(tmp_path):
    async def run():
        async with managed(tmp_path) as m:
            await m.gateway.call(m.main, "player_turn", {}, "start")
            before = copy.deepcopy(m.main.coordination_seen)
            original = m.memory.call
            fail = True

            def commit(job, call_id, name, args, result=None):
                nonlocal fail
                if call_id == "message" and result is not None and fail:
                    fail = False
                    raise OSError("Interrupted reply persistence")
                return original(job, call_id, name, args, result)

            m.memory.call = commit
            args = {"to": "economy", "text": "One new instruction"}
            with pytest.raises(OSError, match="Interrupted reply"):
                await m.gateway.call(m.main, "role_message", args, "message")
            assert m.main.coordination_seen == before
            result = await m.gateway.call(m.main, "role_message", args, "message")
            assert len(m.gateway.wars.state()["messages"]) == 1
            assert apply_coordination_update(before, result) == m.main.coordination_seen
            assert await m.gateway.call(m.main, "role_message", args, "message") == result
            assert len(m.gateway.wars.state()["messages"]) == 1

    asyncio.run(run())
