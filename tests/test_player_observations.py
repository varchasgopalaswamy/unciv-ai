"""Presentation baselines are private to one run, independent of native authority."""

import copy
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.step_persistence import SqliteStepStore

from civ_agent.observations import DetailQuery, detail_scope
from civ_agent.orchestration.contracts import GatewayJob, GatewayReceipt, PrepareRole
from civ_agent.orchestration.store import JournalConflict
from civ_agent.payload_updates import detail_response
from civ_agent.prepared_role_context import delivered_prepared_context
from civ_agent.runtime.agents import LocalAgentRunner, LocalAgentServices
from civ_agent.runtime.player_observations import PlayerObservations
from civ_agent.state_updates import overview_update
from tests.domain_fixtures import run_async
from tests.test_local_agents import request, returns
from tests.test_native_player_stores import production_stores


def context(*, run="run", player="player"):
    deps = request().model_copy(update={"scope": request().scope.model_copy(update={"player": player})})
    return SimpleNamespace(deps=deps, run_id=run, run_step=1)


def detail(*, sequence=1, sections=None, previous=None, value=1, baselines=None):
    query = {"tool": "city_details", "arguments": {"target": "city", "sections": sections or ["production"]}}
    key = detail_scope(DetailQuery.model_validate(query))
    return detail_response(
        previous,
        {"query": query, "data": {"constant": "x" * 900, "value": value}},
        "scope",
        sequence,
        key,
        baselines=baselines,
    )


@run_async
async def test_detail_full_delta_unchanged_cross_query_and_invalidation():
    cap = PlayerObservations(invoke=AsyncMock())
    ctx = context()
    first = await cap.materialize(ctx, detail(), call_id="full")
    state = copy.deepcopy(next(iter(cap.details.values())))
    delta = detail(sequence=2, previous=state, value=2)
    assert delta["detailUpdate"]["kind"] == "delta"
    assert (await cap.materialize(ctx, delta, call_id="delta"))["data"]["value"] == 2
    state = copy.deepcopy(next(iter(cap.details.values())))
    unchanged = detail(sequence=3, previous=state, value=2)
    assert unchanged["detailUpdate"]["kind"] == "unchanged"
    assert (await cap.materialize(ctx, unchanged, call_id="same"))["data"]["value"] == 2
    cross = detail(sequence=4, value=2, sections=["citizens"], baselines=cap.details)
    assert "baseQuery" in cross["detailUpdate"]
    before = copy.deepcopy(cap.details)
    assert (await cap.materialize(ctx, cross, call_id="cross"))["data"]["value"] == 2
    assert all(cap.details[key] == value for key, value in before.items())
    old = await cap.materialize(ctx, detail(), call_id="old")
    assert old["data"]["value"] == 2 and first["data"]["value"] == 1
    invalidation = {"invalidatedDetails": [cross["query"]]}
    assert await cap.materialize(ctx, invalidation, call_id="invalidate") == invalidation
    assert detail_scope(DetailQuery.model_validate(cross["query"])) in cap.details
    cap.invoke.assert_not_called()


@run_async
async def test_repeated_native_invalidation_retains_private_patch_bases_without_delivering_old_facts():
    cap = PlayerObservations(invoke=AsyncMock())
    ctx = context()
    full = await cap.materialize(ctx, detail(), call_id="seed")
    for sequence in range(2, 9):
        previous = copy.deepcopy(next(iter(cap.details.values())))
        invalidation = {
            "kind": "NATIVE_ACTION",
            "actionSummary": "Committed once",
            "invalidatedDetails": [previous["query"]],
        }
        # An action receipt must not expose historical details as fresh evidence.
        assert await cap.materialize(ctx, invalidation, call_id=f"action-{sequence}") == invalidation
        response = detail(sequence=sequence, previous=previous, value=sequence)
        assert response["detailUpdate"]["kind"] == "delta"
        current = await cap.materialize(ctx, response, call_id=f"read-{sequence}")
        assert current["data"]["value"] == sequence
    # More than four legitimate revalidations must not consume recovery reads.
    cross = detail(sequence=9, value=9, sections=["citizens"], baselines=cap.details)
    assert "baseQuery" in cross["detailUpdate"]
    assert (await cap.materialize(ctx, cross, call_id="section-read"))["data"]["value"] == 9
    assert full["data"]["value"] == 1 and cap.recoveries == 0
    cap.invoke.assert_not_called()


@run_async
async def test_missing_baseline_recovers_only_a_bounded_read_and_retains_native_effect():
    seed = PlayerObservations(invoke=AsyncMock())
    await seed.materialize(context(), detail(), call_id="seed")
    state = next(iter(seed.details.values()))
    missing = detail(sequence=2, previous=state, value=2)
    full = detail(sequence=3, value=3)
    invoke = AsyncMock(return_value=GatewayReceipt(result={"receipts": [{"status": "confirmed", "result": full}]}))
    cap = PlayerObservations(invoke=invoke)
    raw = {
        "receipts": [
            {
                "operationId": "committed",
                "tool": "player_command",
                "status": "confirmed",
                "result": {"kind": "START_CONSTRUCTION", "actionSummary": "Started Worker"},
            },
            {"result": missing},
        ]
    }
    materialized = await cap.materialize(context(), raw, call_id="action")
    assert materialized["receipts"][0] == raw["receipts"][0]
    assert materialized["receipts"][1]["result"]["data"]["value"] == 3
    decision = invoke.call_args.args[3]
    assert decision["kind"] == "inspect" and decision["operations"][0]["tool"] == "city_details"
    assert cap.recovery_receipts and "data" not in missing
    for index in range(12):
        cap.details.clear()
        await cap.materialize(context(), missing, call_id=str(index))
    assert invoke.await_count == 13


@run_async
async def test_overview_reducers_survive_compaction_but_new_runs_and_players_are_fenced():
    cap = PlayerObservations(invoke=AsyncMock())
    ctx = context()
    full = {"turn": 1, "revision": "rev1", "stateId": "s1", "cities": []}
    await cap.materialize(ctx, full, call_id="full")
    updated = {**full, "revision": "rev2", "stateId": "s2"}
    delta = {"stateUpdate": overview_update(full, updated, [])}
    # Removing all model history does not remove capability state.
    assert (await cap.materialize(ctx, delta, call_id="after-compaction"))["revision"] == "rev2"
    fresh = await cap.for_run(context(run="new"))
    assert fresh.overview is None and not fresh.details and fresh.recoveries == 0
    for conflicting in (context(run="new"), context(player="different")):
        with pytest.raises(JournalConflict, match="OWNER_CONFLICT"):
            await cap.materialize(conflicting, full, call_id="wrong-owner")
    cap.invoke.assert_not_called()


@run_async
async def test_native_unavailable_and_turn_ended_preserve_receipts_without_recovery():
    cap = PlayerObservations(invoke=AsyncMock())
    for kind in ("unavailable", "turn_ended"):
        value = {"kind": "MOVE_UNIT", "actionSummary": "Moved once", "stateUpdate": {"kind": kind}}
        delivered = await cap.materialize(context(), value, call_id=kind)
        assert delivered["actionSummary"] == "Moved once" and delivered["refreshRequired"]
        assert "stateUpdate" not in delivered
    cap.invoke.assert_not_called()


@run_async
async def test_overview_materialization_preserves_explicit_focused_review():
    cap = PlayerObservations(invoke=AsyncMock())
    full = {"turn": 12, "stateId": "s1", "militaryCompletion": {"remainingTargets": 2}}
    await cap.materialize(context(), full, call_id="baseline")
    review = {"basis": "current", "items": [{"target": "unit1"}, {"target": "city1"}], "nextCursor": None}
    result = await cap.materialize(
        context(),
        {"militaryCompletion": review, "stateUpdate": {"kind": "unchanged", "baseStateId": "s1", "stateId": "s1"}},
        call_id="focused",
    )
    assert result["militaryCompletion"] == review
    assert cap.overview is not None
    assert cap.overview["militaryCompletion"] == {"remainingTargets": 2}
    cap.invoke.assert_not_called()


@run_async
async def test_actual_sdk_request_and_tool_return_have_full_details(tmp_path):
    prepared = detail()
    cap = PlayerObservations(invoke=AsyncMock())
    await cap.materialize(context(), prepared, call_id="baseline")
    delta = detail(sequence=2, previous=next(iter(cap.details.values())), value=2)
    calls = []

    def model(messages, info):
        returned = returns(messages)
        if not returned:
            opening = json.loads(messages[0].parts[0].content)
            assert opening["decisionDetails"][0]["data"]["value"] == 1
            return ModelResponse(
                parts=[ToolCallPart("city_details", {"target": "city", "sections": ["production"]}, "read")]
            )
        assert cast(dict, returned[0].content)["receipts"][0]["result"]["data"]["value"] == 2
        assert returned[0].metadata["raw_player_observation"]["receipts"][0]["result"] == delta
        return ModelResponse(parts=[ToolCallPart("finish_role", {"summary": "Read current production."}, "finish")])

    async def invoke(deps, identity, step, decision):
        calls.append(decision)
        return GatewayReceipt(
            result={"roleFinished": True}
            if decision["kind"] == "finish"
            else {"receipts": [{"status": "confirmed", "result": delta}]}
        )

    runner = LocalAgentRunner(
        LocalAgentServices(invoke=invoke, step_store=SqliteStepStore(database=tmp_path / "steps.sqlite3"))
    )
    result = await runner.run_role(
        request(),
        startup={
            "instructions": "Inspect.",
            "user": json.dumps({"role": "gameplay", "reads": {}, "decisionDetails": [prepared]}),
        },
        catalogue=[{"name": "finish_role", "inputSchema": {}}, {"name": "city_details", "inputSchema": {}}],
        model=FunctionModel(model),
    )
    assert result.usage.requests == 2 and len(calls) == 2


@run_async
async def test_startup_credit_is_derived_from_saved_model_input(tmp_path):
    async with production_stores(tmp_path) as p:
        p.host.view["cities"][0]["constructionChoice"] = {"availableSinceChoice": ["Granary"]}
        role = await p.root.prepare_role(PrepareRole(ticket=p.ticket, purpose="gameplay", pass_number=0))
        assert role is not None
        startup = p.store.get(role.scope, role.startup)
        value = json.loads(startup["user"])
        assert value["decisionDetails"]
        native = p.root.registry.load(GatewayJob(scope=role.scope, job=role.job, purpose=role.purpose))
        for reply in value["decisionDetails"]:
            assert reply["query"] in native.state.delivered_access["details"]
        omitted = [reply["query"] for reply in value["decisionDetails"]]
        value["decisionDetails"] = []
        hidden = delivered_prepared_context(
            p.root.gateway, p.root.turn_roles.jobs["gameplay"], native.catalogue, json.dumps(value)
        )
        assert all(query not in hidden.delivered_access["details"] for query in omitted)


@pytest.mark.parametrize("compact_opening", [False, True])
@run_async
async def test_sdk_credits_only_the_exact_opening_after_a_response_and_not_compacted_facts(
    tmp_path, monkeypatch, compact_opening
):
    class CompactOpening(AbstractCapability):
        async def before_model_request(self, ctx, request_context):
            if not compact_opening:
                return request_context
            messages = copy.deepcopy(list(request_context.messages))
            for message in messages:
                for index, part in enumerate(message.parts):
                    if isinstance(part, UserPromptPart):
                        message.parts[index] = replace(part, content="Compacted history; inspect current facts.")
            return replace(request_context, messages=messages)

    async with production_stores(tmp_path) as p:
        p.host.view["cities"][0]["constructionChoice"] = {"availableSinceChoice": ["Granary"]}
        role = await p.root.prepare_role(PrepareRole(ticket=p.ticket, purpose="gameplay", pass_number=0))
        assert role is not None
        startup = p.store.get(role.scope, role.startup)
        identity = GatewayJob(scope=role.scope, job=role.job, purpose=role.purpose)
        prepared = startup["preparedDelivery"]
        assert prepared["access"]["details"]
        await p.root.registry.startup_delivery(identity, prepared, delivered=False)
        acknowledged = []

        async def delivered(deps, run_id, visible):
            assert deps == role and run_id and visible == (not compact_opening)
            acknowledged.append(visible)
            await p.root.registry.startup_delivery(identity, prepared, delivered=visible)

        def model(messages, info):
            assert not acknowledged
            assert not p.root.registry.load(identity).state.delivered_access["details"]
            user = next(
                part.content for message in messages for part in message.parts if isinstance(part, UserPromptPart)
            )
            assert isinstance(user, str)
            if compact_opening:
                assert user == "Compacted history; inspect current facts."
            else:
                assert json.loads(user)["decisionDetails"]
            return ModelResponse(
                parts=[ToolCallPart("finish_role", {"summary": "Inspected the actual model context."}, "finish")]
            )

        async def invoke(*args):
            assert acknowledged == [not compact_opening]
            access = p.root.registry.load(identity).state.delivered_access
            assert access["details"] == ([] if compact_opening else prepared["access"]["details"])
            return GatewayReceipt(result={"roleFinished": True})

        runner = LocalAgentRunner(
            LocalAgentServices(
                invoke=invoke,
                step_store=SqliteStepStore(database=tmp_path / "steps.sqlite3"),
                on_startup_delivered=delivered,
            )
        )
        capabilities = runner._capabilities
        monkeypatch.setattr(
            runner, "_capabilities", lambda *args, **kwargs: [CompactOpening(), *capabilities(*args, **kwargs)]
        )
        await runner.run_role(
            role, startup=startup, catalogue=[{"name": "finish_role", "inputSchema": {}}], model=FunctionModel(model)
        )
        assert not p.host.executed
