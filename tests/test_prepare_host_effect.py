"""Freeze existing native envelopes without sending or creating Python receipts."""

import asyncio
import copy
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from civ_agent import memory_schema as tables
from civ_agent.host_effects import PreparedHostEffect, host_request_id
from civ_agent.observations import DetailQuery
from civ_agent.schemas import BoundaryModel
from tests.domain_fixtures import Host, managed
from tests.test_action_authority import MOVE, session


def counts(memory):
    with memory.transaction() as db:
        return {
            table.name: len(db.execute(table.select()).all())
            for table in (tables.calls, tables.outbox, tables.decision_journal)
        }


def forbid_journal(*args, **kwargs):
    raise AssertionError("Preparation attempted a legacy call/queue/receipt journal write")


def test_native_prepared_effect_decodes_inside_strict_durable_boundary():
    class Frozen(BoundaryModel):
        prepared: PreparedHostEffect

    prepared = PreparedHostEffect(
        name="player_command",
        arguments=copy.deepcopy(MOVE),
        transport="player_command",
        payload={**MOVE, "requestId": "effect", "epoch": "g1", "revision": "r1"},
    )
    assert Frozen.model_validate({"prepared": asdict(prepared)}).prepared == prepared


@pytest.mark.parametrize(
    "name,arguments,transport,expected",
    [
        (
            "player_command",
            {**MOVE, "intent": {"reason": "Explore safely"}},
            "player_command",
            {"kind": "MOVE_UNIT", "revision": "rev1"},
        ),
        (
            "trade_offer",
            {"partner": "Greece", "give": [], "receive": []},
            "player_communication",
            {"op": "trade_offer", "partner": "Greece"},
        ),
        (
            "trade_decide",
            {"proposalId": "trade1", "decision": "accept"},
            "player_communication",
            {"op": "trade_decide", "proposal": "trade1", "version": 7},
        ),
        (
            "friendship_offer",
            {"partner": "Greece"},
            "player_communication",
            {"op": "friendship_offer", "partner": "Greece"},
        ),
        (
            "chat_send",
            {"recipients": ["Greece"], "text": "Perhaps later.\n  This is not a promise."},
            "player_communication",
            {"op": "send", "text": "Perhaps later.\n  This is not a promise."},
        ),
        (
            "commitment_offer",
            {
                "participants": ["Rome", "Greece"],
                "kind": "peace",
                "terms": "Avoid war while useful",
                "conditions": "If interests align",
            },
            "player_communication",
            {"op": "offer", "terms": "Avoid war while useful", "conditions": "If interests align"},
        ),
        (
            "commitment_decide",
            {"proposalId": "offer1", "decision": "accept"},
            "player_communication",
            {"op": "accept", "proposal": "offer1", "version": 3},
        ),
    ],
)
def test_all_host_effect_families_prepare_the_exact_native_choice_without_dispatch(
    tmp_path, monkeypatch, name, arguments, transport, expected
):
    async def run():
        host = Host()
        async with session(tmp_path, host) as (gateway, memory, job, _):
            job.social = {
                "chatRecipients": ["Greece", "France"],
                "offers": [{"proposal": "offer1", "version": 3}],
                "turn": 0,
            }
            job.social_read = True
            job.trades["trade1"] = {"version": 7, "clauses": []}
            baseline, original = counts(memory), copy.deepcopy(arguments)
            original_tool = host.tool
            calls = []

            async def readonly(name, payload):
                calls.append((name, copy.deepcopy(payload)))
                if (
                    name == "player_command"
                    or name == "player_communication"
                    and payload.get("op") not in {"state", "negotiation", "trade_options", "trade_read"}
                ):
                    raise AssertionError("Preparation attempted a native effect")
                return await original_tool(name, payload)

            with monkeypatch.context() as patch:
                for method in ("call", "queue", "receipt", "uncertain"):
                    patch.setattr(memory, method, forbid_journal)
                patch.setattr(host, "tool", readonly)
                prepared = await gateway.prepare_host_effect(job, name, arguments, "prepared")
            assert isinstance(prepared, PreparedHostEffect) and prepared.result is None
            assert prepared.payload is not None
            assert prepared.transport == transport
            assert prepared.payload["requestId"] == host_request_id(job.id, "prepared")
            assert prepared.payload["epoch"] == "g1"
            assert all(prepared.payload[key] == value for key, value in expected.items())
            assert "intent" not in prepared.payload and "initiativeDecisions" not in prepared.payload
            assert prepared.arguments == original and arguments == original
            assert counts(memory) == baseline and not host.executed
            if "intent" in original:
                assert prepared.intent is not None
                assert prepared.intent["reason"] == original["intent"]["reason"]

    asyncio.run(run())


def test_prepared_wire_matches_existing_dispatch_and_is_detached_from_later_inputs(tmp_path, monkeypatch):
    async def run():
        host = Host()
        async with session(tmp_path, host) as (gateway, memory, job, _):
            arguments = {**copy.deepcopy(MOVE), "intent": {"reason": "Exact private rationale"}}
            prepared = await gateway.prepare_host_effect(job, "player_command", arguments, "one-effect")
            assert prepared.result is None
            assert prepared.payload is not None and prepared.intent is not None
            await gateway.call(job, "player_command", arguments, "one-effect")
            assert host.executed == [prepared.payload]
            arguments["destination"]["x"] = 99
            arguments["intent"]["reason"] = "Changed later"
            assert job.observation is not None
            job.observation["revision"] = "later-view"
            assert prepared.payload["destination"]["x"] == 1 and prepared.payload["revision"] == "rev1"
            assert prepared.intent["reason"] == "Exact private rationale"

    asyncio.run(run())


@pytest.mark.parametrize("field", ["requestId", "epoch", "revision", "reviewId"])
def test_prepare_rejects_model_supplied_bookkeeping_without_journal_or_native_effect(tmp_path, field):
    async def run():
        host = Host()
        async with session(tmp_path, host) as (gateway, memory, job, _):
            baseline = counts(memory)
            prepared = await gateway.prepare_host_effect(job, "player_command", {**MOVE, field: "spoofed"}, "invalid")
            assert prepared.result is not None
            assert prepared.result["error"] == "INVALID_TOOL_ARGUMENTS"
            assert prepared.payload is None and prepared.transport is None
            assert counts(memory) == baseline and not host.executed

    asyncio.run(run())


def test_prepare_retains_exact_preview_ownership_and_peace_authorization_guards(tmp_path):
    async def run():
        async with managed(tmp_path) as domain:
            await domain.gateway.call(domain.economy, "player_turn", {}, "initial")
            domain.roles.overrides["city2"] = "Main owns this city"
            baseline = counts(domain.memory)
            denied = await domain.gateway.prepare_host_effect(
                domain.economy,
                "player_command",
                {"kind": "SET_CITY_FOCUS", "target": "city2", "selection": "FoodFocus"},
                "denied",
            )
            assert denied.result["error"] == "CITY_OWNED_BY_OTHER_ROLE"
            domain.roles.overrides.pop("city2")
            arguments = {"kind": "SET_UNIT_DESTINATION", "target": "unit1", "destination": {"x": 3, "y": 1}}
            # Use Main without a role coordinator here to isolate exact source guards.
            coordinator = domain.gateway.role_coordinator
            domain.gateway.role_coordinator = None
            missing = await domain.gateway.prepare_host_effect(domain.main, "player_command", arguments, "preview")
            assert missing.result["error"] == "DETAIL_REQUIRED"
            domain.main.details_read.append(
                DetailQuery(tool="movement_preview", arguments={"target": "unit1", "destination": {"x": 3, "y": 1}})
            )
            authorizer = AsyncMock(return_value={"error": "PEACE_REVIEW_STALE", "applied": False})
            domain.gateway.peace_reviews = SimpleNamespace(authorize=authorizer)
            stale = await domain.gateway.prepare_host_effect(domain.main, "player_command", arguments, "preview")
            assert stale.result["error"] == "PEACE_REVIEW_STALE" and stale.payload is None
            authorizer.assert_awaited_once()
            domain.gateway.role_coordinator = coordinator
            assert counts(domain.memory) == baseline and not domain.host.executed

    asyncio.run(run())


def test_ordinary_chat_prepare_preserves_routing_boundary_without_sending_or_starting_workflow(tmp_path):
    async def run():
        host = Host()
        async with session(tmp_path, host) as (gateway, memory, job, _):
            job.social = {"chatRecipients": ["Greece", "France"]}
            route = AsyncMock()
            gateway.routing = SimpleNamespace(send=route)
            arguments = {"recipients": ["Greece"], "text": "  Exact text\n", "delivery": "immediate"}
            baseline = counts(memory)
            prepared = await gateway.prepare_host_effect(job, "chat_send", arguments, "draft")
            assert prepared.payload is not None
            assert prepared.routed and prepared.payload["text"] == arguments["text"]
            assert prepared.arguments["delivery"] == "immediate" and "delivery" not in prepared.payload
            route.assert_not_awaited()
            assert counts(memory) == baseline and not host.executed

    asyncio.run(run())


def test_prepare_keeps_review_and_intent_choices_for_apply_but_out_of_native_wire(tmp_path):
    async def run():
        host = Host()
        async with session(tmp_path, host) as (gateway, memory, main, _):
            job = await gateway.open("military-prepare", "military", "g1", host, {}, 0)
            job.observation, job.verified = copy.deepcopy(main.observation), True
            gateway.role_coordinator = SimpleNamespace(command_problem=lambda *_: None, executive=False)
            arguments = {**MOVE, "reviewAfter": True, "intent": {"reason": "Hold the chosen line"}}
            baseline = counts(memory)
            prepared = await gateway.prepare_host_effect(job, "player_command", arguments, "review")
            assert prepared.payload is not None
            assert prepared.arguments["reviewAfter"] is True
            assert prepared.arguments["intent"]["reason"] == "Hold the chosen line"
            assert "reviewAfter" not in prepared.payload and "intent" not in prepared.payload
            assert counts(memory) == baseline and not host.executed

    asyncio.run(run())


def test_native_end_turn_prepare_does_not_complete_role_and_trusted_handoff_keeps_review_token(tmp_path):
    async def run():
        async with managed(tmp_path) as domain:
            initial = dict(domain.roles.ready)
            baseline = counts(domain.memory)
            role = await domain.gateway.prepare_host_effect(
                domain.main, "player_command", {"kind": "END_TURN"}, "finish"
            )
            assert role.result["error"] == "ROLE_FINISH_REQUIRED" and domain.roles.ready == initial
            handoff = await domain.gateway.prepare_host_effect(
                domain.main, "player_command", {"kind": "END_TURN"}, "handoff", host_handoff=True
            )
            assert handoff.result is None and handoff.payload["kind"] == "END_TURN"
            assert handoff.payload["reviewId"] == domain.main.observation.get("turnReview", {}).get("id")
            assert counts(domain.memory) == baseline and not domain.host.executed

    asyncio.run(run())
