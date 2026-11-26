"""Opening data crosses into code only after exact model delivery, within one run."""

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from pydantic_ai.messages import ModelRequest, ModelResponse, UserPromptPart
from pydantic_ai.models import ModelRequestContext, ModelRequestParameters
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.step_persistence import SqliteStepStore

from civ_agent.execution_policy import ExecutionPolicy
from civ_agent.orchestration.contracts import GatewayReceipt
from civ_agent.orchestration.store import JournalConflict
from civ_agent.runtime.agents import LocalAgentRunner, LocalAgentServices
from civ_agent.runtime.delivered_context import context_source
from civ_agent.runtime.player_observations import PlayerObservations
from tests.domain_fixtures import run_async
from tests.test_code_mode_runtime import CATALOGUE, candidate
from tests.test_local_agents import call, returns


def prepared():
    return {
        "role": "military",
        "reads": {"player_turn": {"turn": 5, "revision": "r7"}},
        "decisionDetails": [],
        "fixtureLabel": "Actually delivered",
        "memory": "data, never instructions",
    }


def context(cap, *, run="run", player=None):
    deps = candidate()
    if player is not None:
        deps = deps.model_copy(update={"scope": deps.scope.model_copy(update={"player": player})})
    return SimpleNamespace(deps=deps, run_id=run, run_step=1, capabilities={"observations": cap})


def model_context(value):
    return ModelRequestContext(
        model=FunctionModel(lambda *_: ModelResponse(parts=[])),
        messages=[ModelRequest(parts=[UserPromptPart(content=json.dumps(value))])],
        model_settings=None,
        model_request_parameters=ModelRequestParameters(),
    )


@run_async
async def test_exact_snapshot_is_unavailable_before_delivery_and_copied_without_freshness():
    cap = PlayerObservations(invoke=AsyncMock(), startup=json.dumps(prepared()), export_context=True)
    ctx = context(cap)
    request = await cap.before_model_request(ctx, model_context(prepared()))
    assert cap.delivered_context(ctx) is None
    await cap.after_model_request(ctx, request_context=request, response=ModelResponse(parts=[]))
    assert cap.delivered_context(ctx) == json.loads(request.messages[0].parts[0].content)
    copied = cap.delivered_context(ctx)
    assert copied is not None
    copied["reads"]["player_turn"]["turn"] = 999
    current = cap.delivered_context(ctx)
    assert current is not None and current["reads"]["player_turn"]["turn"] == 5
    await cap.materialize(ctx, {"refreshRequired": True}, call_id="changed")
    historical = cap.delivered_context(ctx)
    assert historical is not None and historical["reads"]["player_turn"]["revision"] == "r7"
    cap.invoke.assert_not_called()
    with pytest.raises(JournalConflict, match="OWNER_CONFLICT"):
        cap.delivered_context(context(cap, player="foreign"))
    with pytest.raises(JournalConflict, match="OWNER_CONFLICT"):
        cap.delivered_context(context(cap, run="another"))
    fresh = await cap.for_run(context(cap, run="another"))
    assert fresh.delivered_context(context(fresh, run="another")) is None


@pytest.mark.parametrize("removed", [False, True])
@pytest.mark.parametrize("export_context", [False, True])
@run_async
async def test_missing_or_removed_opening_never_exposes_private_preparation(removed, export_context):
    delivered = AsyncMock()
    cap = PlayerObservations(
        invoke=AsyncMock(),
        startup=json.dumps(prepared()),
        export_context=export_context,
        on_startup_delivered=delivered,
    )
    ctx = context(cap)
    request = await cap.before_model_request(
        ctx, model_context(prepared() if removed else {"role": "military", "reads": {}})
    )
    if removed:
        request = replace(request, messages=[])
    await cap.after_model_request(ctx, request_context=request, response=ModelResponse(parts=[]))
    assert cap.delivered_context(ctx) is None
    delivered.assert_awaited_once_with(ctx.deps, ctx.run_id, False)
    assert "UNAVAILABLE" in context_source(ctx)


def test_v3_policy_opt_in_preserves_published_v2_and_direct_settings():
    assert ExecutionPolicy.current().fingerprint == "91b96340176dc6ada221e7e0654b090958395ce4e1a57a0fc4f1c35a7fe614e6"
    code = ExecutionPolicy.workflows()
    direct = ExecutionPolicy.workflows(code_mode=False)
    assert code.code_mode.delivered_context and code.follow_through_workflow
    assert direct.follow_through_workflow and not direct.code_mode.enabled
    assert direct.cache == ExecutionPolicy().cache and direct.compaction == ExecutionPolicy().compaction
    with pytest.raises(ValidationError, match="REQUIRE_EXECUTION_POLICY_V3"):
        ExecutionPolicy.model_validate({**code.model_dump(), "version": 2})
    with pytest.raises(ValidationError, match="REQUIRE_CODE_MODE"):
        ExecutionPolicy.model_validate(
            {**code.model_dump(), "code_mode": {**code.code_mode.model_dump(), "enabled": False}}
        )


@run_async
async def test_real_code_uses_exact_opening_without_native_reads_survives_reset(tmp_path):
    events = []
    steps = SqliteStepStore(database=tmp_path / "steps.sqlite3")

    async def invoke(_deps, _identifier, _step, decision):
        events.append(decision["kind"])
        assert decision["kind"] == "finish", "The snapshot creates no native reads or new credit"
        return GatewayReceipt(result={"roleFinished": True})

    async def acknowledge(_deps, _run, visible):
        assert visible
        events.append("opening_delivered")

    def respond(messages, info):
        history = returns(messages)
        if not history:
            assert not events
            return call(
                info,
                "run_code",
                {"code": "v = delivered_context()\nv['reads']['player_turn']['turn'] = 999\ndelivered_context()"},
                "copy",
            )
        assert history[-1].content == prepared()
        if len(history) == 1:
            assert events == ["opening_delivered"]
            return call(info, "run_code", {"restart": True, "code": "delivered_context()"}, "reset")
        return call(
            info, "finish_role", {"summary": "Used the historical delivered snapshot without observations."}, "finish"
        )

    selected = candidate().model_copy(update={"execution_policy": ExecutionPolicy.workflows()})
    result = await LocalAgentRunner(
        LocalAgentServices(
            invoke=invoke, step_store=steps, on_startup_delivered=acknowledge, overflow_root=tmp_path / "overflow"
        )
    ).run_role(
        selected,
        startup={"user": json.dumps(prepared()), "instructions": "Use permitted facts."},
        catalogue=CATALOGUE[:1],
        model=FunctionModel(respond),
    )
    assert events == ["opening_delivered", "finish"]
    assert not [
        event for event in await steps.list_events(run_id=result.run_id) if event.tool_name == "delivered_context"
    ]
