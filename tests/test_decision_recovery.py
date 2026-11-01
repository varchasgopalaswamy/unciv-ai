"""Decisions retain native idempotency and require delivered prerequisites."""

import asyncio
import copy
import json

import pytest

from civ_agent.civilization_state import StateConflict
from civ_agent.decision_contracts import DecisionContract
from civ_agent.military_schedule import MilitarySchedule
from civ_agent.observations import required_detail
from civ_agent.payload_updates import apply_detail_response
from civ_agent.response_compaction import expand_response
from civ_agent.startup_context import prepare_startup
from civ_agent.structured_decisions import DecisionLoop, capture_answer, generation_event, journal
from tests.test_economy_optimization import managed
from tests.test_economy_roles import run_async


def command(kind, **arguments):
    return {"tool": "player_command", "arguments": {"command": {"kind": kind, **arguments}}}


def saved_decision(memory, identity):
    value = journal(memory, identity)
    assert value is not None
    return value


async def begin(m, job=None, record=None):
    job = job or m.economy
    await m.gateway.call(job, "player_turn", {}, "delivered-start")
    loop = DecisionLoop(
        m.gateway, job, DecisionContract(job.purpose, await m.gateway.tools(job)), record if record is not None else {}
    )
    await loop.begin("Delivered position")
    return loop


@pytest.mark.parametrize("delivery", ["auto", "full"])
@run_async
async def test_decision_receipts_preserve_gateway_compaction_and_replay(tmp_path, delivery):
    async with managed(tmp_path) as m:
        warning = "The estimate assumes current yields and does not predict future changes. " * 8
        m.host.view["cities"][0]["estimates"] = [{"warnings": [warning]}, {"warnings": [warning]}]
        m.economy.compact_overviews = True
        loop = await begin(m)
        result = await loop.complete(
            loop.contract.encode(
                {
                    "kind": "inspect",
                    "operations": [
                        {
                            "tool": "city_details",
                            "arguments": {"target": "city1"},
                            **({"recoverBaseline": "missing"} if delivery == "full" else {}),
                        }
                    ],
                }
            )
        )
        receipt = result["receipts"][0]
        gateway_result = m.memory.call(
            m.economy.id,
            receipt["operationId"],
            "city_details",
            {"target": "city1", **({"delivery": "full"} if delivery == "full" else {})},
        )
        assert gateway_result is not None
        assert ("sharedValues" in gateway_result) == (delivery == "auto")
        assert receipt["result"] == gateway_result
        expanded = expand_response(receipt["result"])
        assert expanded["data"]["entity"]["estimates"] == [{"warnings": [warning]}, {"warnings": [warning]}]
        loop.delivered(result)
        assert expanded["query"] in loop.delivered_access["details"]
        message = await loop.begin(result)
        assert json.loads(message["toolOutput"]["output"]) == result
        restarted = DecisionLoop(m.gateway, m.economy, loop.contract, {})
        restarted.current = saved_decision(m.memory, result["decisionId"])
        assert await restarted.execute(recovery=True) == result


@run_async
async def test_normal_inspections_revalidate_and_recovery_restores_identical_data(tmp_path):
    async with managed(tmp_path) as m:
        m.economy.compact_overviews = True
        loop = await begin(m)
        reads = []
        observe_detail = m.memory.observe_detail

        def observe(*args):
            reads.append(args[0])
            return observe_detail(*args)

        m.memory.observe_detail = observe
        replies, baselines = [], {}
        for recovery in (None, None, "missing"):
            response = await loop.complete(
                loop.contract.encode(
                    {
                        "kind": "inspect",
                        "operations": [
                            {
                                "tool": "city_details",
                                "arguments": {"target": "city2"},
                                **({"recoverBaseline": recovery} if recovery else {}),
                            }
                        ],
                    }
                )
            )
            reply = response["receipts"][0]["result"]
            replies.append(reply)
            current = apply_detail_response(baselines, reply)
            baselines.update(m.economy.detail_seen)
            loop.delivered(response)
            await loop.begin(response)
        assert len(reads) == 3
        assert [reply["detailUpdate"]["kind"] for reply in replies] == ["full", "unchanged", "full"]
        assert current["value"] == expand_response(replies[0])["data"]
        assert len(json.dumps(replies[1])) < len(json.dumps(replies[2]))


@run_async
async def test_independent_city_actions_share_batch_but_changed_targets_stop(tmp_path):
    async with managed(tmp_path) as m:
        loop = await begin(m)
        operations = [command("SET_CITY_FOCUS", target=city, selection="FoodFocus") for city in ("city1", "city2")]
        result = await loop.complete(loop.contract.encode({"kind": "act", "operations": operations}))
        assert [r["status"] for r in result["receipts"]] == ["confirmed", "confirmed"]
        assert len(m.host.executed) == 2
        loop.delivered(result)
        await loop.begin(result)
        native = m.host.tool

        async def change_sibling(name, arguments):
            value = await native(name, arguments)
            if name == "player_command":
                m.host.view["cities"][1]["population"] = 9
            return value

        m.host.tool = change_sibling
        result = await loop.complete(loop.contract.encode({"kind": "act", "operations": operations}))
        assert result["receipts"][1]["reason"] == "TARGET_CHANGED"
        assert len(m.host.executed) == 3


@run_async
async def test_intermediate_host_observation_never_grants_preview_credit(tmp_path):
    async with managed(tmp_path) as m:
        loop = await begin(m)
        move = {"kind": "SET_UNIT_DESTINATION", "target": "unit1", "destination": {"x": 1, "y": 0}}
        detail = required_detail(move)
        assert detail is not None
        # Simulate a host refresh that inspected a newly legal option without
        # delivering it to the model. Only delivered() may advance access.
        m.economy.details_read.append(detail)
        assert loop._prerequisite("player_command", move, loop.delivered_access, 1) == "FRESH_INSPECTION_REQUIRED"
        loop.delivered({"acknowledged": True})
        assert loop._prerequisite("player_command", move, loop.delivered_access, 1) == "FRESH_INSPECTION_REQUIRED"
        loop.delivered({"available": True, "query": detail.model_dump()})
        assert loop._prerequisite("player_command", move, loop.delivered_access, 1) is None
        m.economy.details_read.clear()
        assert loop._prerequisite("player_command", move, loop.delivered_access, 1) == "FRESH_INSPECTION_REQUIRED"


@run_async
async def test_failed_action_stops_and_keeps_individual_native_receipt(tmp_path):
    async with managed(tmp_path) as m:
        loop = await begin(m)
        native = m.host.tool

        async def reject(name, arguments):
            if name == "player_command":
                return {"error": "INSUFFICIENT_GOLD", "applied": False, "requestId": arguments["requestId"]}
            return await native(name, arguments)

        m.host.tool = reject
        decision = {"kind": "act", "operations": [command("SET_CITY_FOCUS", target="city1", selection="FoodFocus")] * 2}
        result = await loop.complete(loop.contract.encode(decision))
        assert result["receipts"][0]["status"] == "rejected"
        assert result["receipts"][1]["status"] == "unexecuted"
        identity = result["receipts"][0]["operationId"]
        assert m.memory.call_receipt(m.economy.id, identity)["error"] == "INSUFFICIENT_GOLD"


@pytest.mark.parametrize("uncertain", [False, True])
@run_async
async def test_restart_reconciles_lost_receipt_and_never_reexecutes(uncertain, tmp_path):
    async with managed(tmp_path) as m:
        record = {}
        loop = await begin(m, record=record)
        native = m.host.tool
        reconciliations = []

        async def lost(name, arguments):
            if name == "player_receipt":
                reconciliations.append(arguments["requestId"])
                if uncertain:
                    return {"error": "NOT_ENACTED"}
                return m.host.seen[arguments["requestId"]]
            value = await native(name, arguments)
            if name == "player_command":
                raise asyncio.CancelledError()
            return value

        m.host.tool = lost
        decision = {
            "kind": "act",
            "operations": [
                command("SET_CITY_FOCUS", target=city, selection="FoodFocus") for city in ("city1", "city2")
            ],
        }
        with pytest.raises(asyncio.CancelledError):
            await loop.complete(loop.contract.encode(decision))
        old_id = record["activeDecision"]
        assert saved_decision(m.memory, old_id)["status"] == "uncertain"
        restarted = DecisionLoop(m.gateway, m.economy, loop.contract, record)
        if uncertain:
            with pytest.raises(StateConflict, match="DECISION_EXECUTION_UNCERTAIN"):
                await restarted.begin("Resumed")
            assert len(m.host.executed) == 1 and record["activeDecision"] == old_id
        else:
            message = await restarted.begin("Resumed")
            assert "Recovered decision receipts" in message["input"][0]["text"]
            saved = saved_decision(m.memory, old_id)
            assert saved["receipts"][0]["status"] == "confirmed"
            assert saved["receipts"][1]["reason"] == "RECOVERY_REQUIRES_DECISION"
            assert len(m.host.executed) == 1
        assert len(reconciliations) == 1


@run_async
async def test_completed_receipts_replay_after_restart_and_rewind_fences_old_epoch(tmp_path):
    async with managed(tmp_path) as m:
        record = {}
        loop = await begin(m, record=record)
        result = await loop.complete(
            loop.contract.encode(
                {"kind": "act", "operations": [command("SET_CITY_FOCUS", target="city2", selection="FoodFocus")]}
            )
        )
        saved = copy.deepcopy(loop.current)
        restarted = DecisionLoop(m.gateway, m.economy, loop.contract, record)
        response = await restarted.begin({"instruction": "Resume"})
        assert json.loads(response["toolOutput"]["output"])["recoveredDecision"] == result
        assert len(m.host.executed) == 1
        checkpoint = tmp_path / "checkpoint.json"
        digest = m.memory.backup(checkpoint)
        m.memory.restore(checkpoint, digest, "g2")
        restarted.current = saved
        with pytest.raises((StateConflict, ValueError)):
            await restarted.execute()
        assert len(m.host.executed) == 1


@pytest.mark.parametrize("targets", [0, 23])
@run_async
async def test_military_finish_binds_only_delivered_pages_and_allows_zero_targets(targets, tmp_path):
    async with managed(tmp_path) as m:
        template = m.host.view["map"]["units"][0]
        m.host.view["map"]["units"] = [
            {**copy.deepcopy(template), "handle": f"u{i}", "nearbyHostiles": [{"distance": 1}]} for i in range(targets)
        ]
        m.host.view["diplomacy"] = {"knownCivilizations": [], "cityStates": []}
        job = await m.gateway.open("military", "military", "g1", m.host, {}, 0)
        m.roles.register(job)
        m.roles.military_schedule = MilitarySchedule(m.memory)
        m.roles.resume("military", "Review current threats")
        loop = await begin(m, job)
        if targets:

            def read(cursor):
                return {
                    "kind": "inspect",
                    "operations": [{"tool": "military_review", "arguments": {"cursor": cursor, "limit": 20}}],
                }

            first = await loop.complete(loop.contract.encode(read(0)))
            loop.delivered(first)
            await loop.begin(first)
            early = await loop.complete(
                loop.contract.encode(
                    {"kind": "finish", "arguments": {"decisions": [{"targets": ["u22"], "reason": "Hold"}]}}
                )
            )
            assert early["error"] == "MILITARY_REVIEW_NOT_DELIVERED"
            await loop.begin(early)
            second = await loop.complete(loop.contract.encode(read(20)))
            loop.delivered(second)
            await loop.begin(second)
        finish = {
            "kind": "finish",
            "arguments": {"decisions": [{"targets": [f"u{i}" for i in range(targets)], "reason": "Defend in place"}]}
            if targets
            else {},
        }
        result = await loop.complete(loop.contract.encode(finish))
        assert result["receipts"][0]["result"]["roleFinished"]
        assert m.roles.ready["military"]


@run_async
async def test_targeted_economy_pass_and_wait_keep_native_scope(tmp_path):
    async with managed(tmp_path) as m:
        prepared = await prepare_startup(m.gateway, m.economy)
        assert [c["handle"] for c in prepared["reads"]["player_turn"]["cities"]] == ["city2"]
        loop = DecisionLoop(m.gateway, m.economy, DecisionContract("economy", await m.gateway.tools(m.economy)), {})
        await loop.begin("Targeted city2 review")
        response = await loop.complete(loop.contract.encode({"kind": "finish", "arguments": {}}))
        assert response["receipts"][0]["result"]["roleFinished"]
        main = await begin(m, m.main)
        result = await main.complete(main.contract.encode({"kind": "wait", "arguments": {}}))
        assert result["receipts"][0]["result"]["economyWait"]["state"] == "idle"
        assert not m.roles.ready["gameplay"]


@run_async
async def test_late_generation_events_do_not_change_another_decision(tmp_path):
    async with managed(tmp_path) as m:
        record = {"threadId": "thread"}
        loop = await begin(m, record=record)
        await loop.bind("first")
        assert loop.current is not None
        old_id = loop.current["id"]
        result = await loop.complete(
            loop.contract.encode({"kind": "inspect", "operations": [{"tool": "player_turn", "arguments": {}}]})
        )
        await loop.begin(result)
        await loop.bind("second")
        await generation_event(
            m.gateway,
            record,
            "thread/tokenUsage/updated",
            {
                "threadId": "thread",
                "turnId": "first",
                "tokenUsage": {"last": {"inputTokens": 50}, "total": {"inputTokens": 50}},
            },
        )
        assert saved_decision(m.memory, old_id)["usage"]["inputTokens"] == 50
        assert "usage" not in saved_decision(m.memory, loop.current["id"])
        await generation_event(
            m.gateway,
            record,
            "service/transportFailure",
            {"threadId": "thread", "turnId": "first", "reason": "CODEX_DISCONNECTED"},
        )
        assert saved_decision(m.memory, old_id)["status"] == "completed"


def test_final_answer_beats_late_unphased_or_commentary_text():
    answers = {}
    for phase, text in (
        (None, "old"),
        ("final_answer", "decision"),
        (None, "closing prose"),
        ("commentary", "more prose"),
    ):
        capture_answer(answers, {"turnId": "turn", "item": {"type": "agentMessage", "phase": phase, "text": text}})
    assert answers["turn"] == "decision"


@run_async
async def test_escort_transfer_stops_batch_and_revokes_sibling_authority(tmp_path):
    async with managed(tmp_path) as m:
        worker = {**m.host.view["map"]["units"][0], "handle": "worker", "name": "Worker", "military": False}
        m.host.view["map"]["units"].append(worker)
        military = await m.gateway.open("military", "military", "g1", m.host, {}, 0)
        m.roles.register(military)
        m.roles.resume("military", "Review units")
        loop = await begin(m, m.main)
        result = await loop.complete(
            loop.contract.encode(
                {
                    "kind": "act",
                    "operations": [
                        {
                            "tool": "escort_assignment",
                            "arguments": {"unit": "unit1", "civilian": "worker", "reason": "Protect work crew"},
                        },
                        command("SET_UNIT_ORDER", target="unit1", selection="Fortify"),
                    ],
                }
            )
        )
        assert result["receipts"][0]["status"] == "confirmed"
        assert result["receipts"][1]["reason"] == "PREREQUISITES_CHANGED"
        sibling = await begin(m, military)
        rejected = await sibling.complete(
            sibling.contract.encode(
                {"kind": "act", "operations": [command("SET_UNIT_ORDER", target="unit1", selection="Fortify")]}
            )
        )
        assert rejected["receipts"][0]["result"]["error"] == "UNIT_OWNED_BY_OTHER_ROLE"
        assert not m.host.executed


@run_async
async def test_request_ids_cannot_acknowledge_role_messages(tmp_path):
    async with managed(tmp_path) as m:
        request = await m.gateway.call(
            m.main, "economy_request", {"city": "city2", "text": "Consider a Warrior", "priority": "routine"}, "request"
        )
        loop = await begin(m, m.main)
        result = await loop.complete(
            loop.contract.encode(
                {
                    "kind": "act",
                    "operations": [
                        {
                            "tool": "role_message",
                            "arguments": {"action": "acknowledge", "ids": [request["request"]["id"]]},
                        }
                    ],
                }
            )
        )
        assert result["receipts"][0]["result"]["error"] == "UNKNOWN_ROLE_MESSAGE"


@run_async
async def test_independent_coordination_and_memory_actions_share_a_batch(tmp_path):
    async with managed(tmp_path) as m:
        loop = await begin(m, m.main)
        result = await loop.complete(
            loop.contract.encode(
                {
                    "kind": "act",
                    "operations": [
                        {
                            "tool": "memory_update",
                            "arguments": {
                                "changes": [
                                    {"op": "create", "content": {"type": "note", "text": "Hold the eastern approach."}}
                                ]
                            },
                        },
                        {"tool": "economy_request", "arguments": {"city": "city1", "text": "Consider a Granary"}},
                        {"tool": "economy_request", "arguments": {"city": "city2", "text": "Consider a Warrior"}},
                        {
                            "tool": "memory_update",
                            "arguments": {
                                "changes": [
                                    {"op": "create", "content": {"type": "note", "text": "Scout the western approach."}}
                                ]
                            },
                        },
                    ],
                }
            )
        )
        assert [r["status"] for r in result["receipts"]] == ["confirmed"] * 4
        assert not result["unexecuted"]


@run_async
async def test_created_memory_does_not_grant_an_unseen_intent_citation(tmp_path):
    async with managed(tmp_path) as m:
        prior = next(key for key, value in m.main.memory["records"].items() if value["category"] == "strategy")
        retired = await m.gateway.call(
            m.main,
            "memory_update",
            {"changes": [{"op": "retire", "id": prior, "reason": "superseded"}]},
            "retire-prior",
        )
        assert retired["changes"][0]["accepted"]
        loop = await begin(m, m.main)
        before = copy.deepcopy(loop.delivered_access)
        response = await loop.complete(
            loop.contract.encode(
                {
                    "kind": "act",
                    "operations": [
                        {
                            "tool": "memory_update",
                            "arguments": {
                                "changes": [
                                    {
                                        "op": "create",
                                        "content": {"type": "strategy", "assessment": "Defend the eastern approach"},
                                    }
                                ]
                            },
                        }
                    ],
                }
            )
        )
        reference = response["receipts"][0]["result"]["changes"][0]["id"]
        assert reference is not None
        arguments = {
            "kind": "SET_RESEARCH_QUEUE",
            "queue": ["Mining"],
            "intent": {"reason": "Develop", "strategy": reference},
        }
        assert loop._prerequisite("player_command", arguments, before, 1) == "FRESH_OBSERVATION_REQUIRED"
        loop.delivered(response)
        assert loop._prerequisite("player_command", arguments, loop.delivered_access, 1) is None


@pytest.mark.parametrize("same_record", [False, True])
@run_async
async def test_memory_batch_checks_only_referenced_versions(tmp_path, same_record):
    async with managed(tmp_path) as m:
        initial = await m.gateway.call(
            m.main,
            "memory_update",
            {"changes": [{"op": "create", "content": {"type": "note", "text": text}} for text in ("East", "West")]},
            "seed",
        )
        ids = [change["id"] for change in initial["changes"]]
        loop = await begin(m, m.main)
        result = await loop.complete(
            loop.contract.encode(
                {
                    "kind": "act",
                    "operations": [
                        {
                            "tool": "memory_update",
                            "arguments": {"changes": [{"op": "amend", "id": key, "fields": {"priority": priority}}]},
                        }
                        for key, priority in zip((ids[0], ids[0] if same_record else ids[1]), (4, 5), strict=True)
                    ],
                }
            )
        )
        assert result["receipts"][0]["status"] == "confirmed"
        if same_record:
            assert result["receipts"][1]["reason"] == "FRESH_OBSERVATION_REQUIRED"
        else:
            assert result["receipts"][1]["status"] == "confirmed"


@pytest.mark.parametrize("same_operation", [False, True])
@run_async
async def test_military_notes_continue_only_for_independent_operations(tmp_path, same_operation):
    async with managed(tmp_path) as m:
        job = await m.gateway.open("military", "military", "g1", m.host, {}, 0)
        m.roles.register(job)
        m.roles.resume("military", "Plan defense")
        loop = await begin(m, job)
        result = await loop.complete(
            loop.contract.encode(
                {
                    "kind": "act",
                    "operations": [
                        {
                            "tool": "military_operation",
                            "arguments": {
                                "id": key,
                                "plan": "Defend",
                                "progress": "Holding",
                                "reconsiderWhen": "Enemy movement",
                            },
                        }
                        for key in ("east", "east" if same_operation else "west")
                    ],
                }
            )
        )
        assert result["receipts"][0]["status"] == "confirmed"
        if same_operation:
            assert result["receipts"][1]["reason"] == "PREREQUISITES_CHANGED"
        else:
            assert result["receipts"][1]["status"] == "confirmed"


@run_async
async def test_usage_includes_internal_continuations_and_repairs_without_double_counting(tmp_path):
    from civ_agent.tool_failures import decision_usage

    async with managed(tmp_path) as m:
        record = {"threadId": "thread"}
        loop = await begin(m, record=record)
        await loop.bind("first")
        assert loop.current is not None
        old_id = loop.current["id"]

        async def usage(turn, total, last):
            await generation_event(
                m.gateway,
                record,
                "thread/tokenUsage/updated",
                {
                    "threadId": "thread",
                    "turnId": turn,
                    "tokenUsage": {"total": total, "last": last},
                },
            )

        first = {"inputTokens": 100, "cachedInputTokens": 40, "outputTokens": 10}
        total = {"inputTokens": 210, "cachedInputTokens": 130, "outputTokens": 25}
        await usage("first", first, first)
        await usage("first", total, {"inputTokens": 110, "cachedInputTokens": 90, "outputTokens": 15})
        invalid = await loop.complete("not JSON")
        await loop.begin(invalid)
        await loop.bind("repair")
        await usage("repair", {"inputTokens": 310, "cachedInputTokens": 170, "outputTokens": 35}, first)
        # A repeated notification and a late old-turn update cannot add usage.
        await usage("repair", {"inputTokens": 310, "cachedInputTokens": 170, "outputTokens": 35}, first)
        await usage("first", total, total)
        rows = [saved_decision(m.memory, identity) for identity in (old_id, loop.current["id"])]
        result = decision_usage(rows)[0]
        assert result["decisionRequests"] == 2 and result["repairRequests"] == 1
        assert result["inputTokens"] == 310 and result["uncachedInputTokens"] == 140
        assert result["repairInputTokens"] == 100 and result["repairOutputTokens"] == 10

        # A crash after the journal write but before the controller record save
        # must not bill the entire resumed thread again to the next decision.
        repair_id = loop.current["id"]
        record = {"threadId": "thread", "activeDecision": repair_id}
        resumed = await begin(m, record=record)
        await resumed.bind("resumed")
        await usage("resumed", {"inputTokens": 410, "cachedInputTokens": 210, "outputTokens": 45}, first)
        assert resumed.current is not None
        resumed_row = saved_decision(m.memory, resumed.current["id"])
        assert resumed_row["usage"]["inputTokens"] == 100
        assert decision_usage([*rows, resumed_row])[0]["inputTokens"] == 410
