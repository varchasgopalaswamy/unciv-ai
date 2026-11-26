"""A trusted visible Code Mode plan replaces duplicate fallback injection."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import TypeAdapter
from pydantic_ai.messages import (
    CompactionPart,
    ModelRequest,
    ModelResponse,
    TextContent,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai_harness.planning import PlanItem
from pydantic_ai_harness.planning._toolset import PLAN_ORIGIN, framed_plan

from civ_agent.orchestration.player_plans import RolePlanning
from tests.domain_fixtures import run_async


def prepared(*, visible=True, trusted=True, serialized=False, changed=False):
    old = [PlanItem(id="one", content="Preserve exact authored wording?", status="in_progress")]
    current = [PlanItem(id="one", content="A changed plan", status="in_progress")] if changed else old
    frame = framed_plan(old)
    child = ToolReturnPart(
        tool_name="update_task_status",
        tool_call_id="code__1",
        content=frame,
        metadata={"origin": PLAN_ORIGIN if trusted else "authored", "plan": frame},
    )
    child = TypeAdapter(ToolReturnPart).dump_python(child, mode="json") if serialized else child
    parent = ToolReturnPart(
        tool_name="run_code",
        tool_call_id="code",
        content={"output": frame if visible else "Output spilled."},
        metadata={"tool_returns": {"code__1": child}},
    )
    planner = RolePlanning()
    planner._read_plan = AsyncMock(return_value=current)
    enqueued = []
    ctx = SimpleNamespace(enqueue=enqueued.append)
    request = SimpleNamespace(messages=[ModelRequest(parts=[parent])])
    return planner, ctx, request, enqueued


@pytest.mark.parametrize("serialized", [False, True])
@run_async
async def test_visible_current_child_plan_prevents_duplicate_user_reminder(serialized):
    planner, ctx, request, enqueued = prepared(serialized=serialized)
    assert await planner.before_model_request(ctx, request) is request
    assert enqueued == []


@pytest.mark.parametrize("options", [{"visible": False}, {"trusted": False}, {"changed": True}])
@run_async
async def test_hidden_authored_or_stale_plan_does_not_suppress_current_reminder(options):
    planner, ctx, request, enqueued = prepared(**options)
    await planner.before_model_request(ctx, request)
    assert len(enqueued) == 1
    assert isinstance(enqueued[0], UserPromptPart)


@run_async
async def test_compacted_away_plan_and_new_run_receive_current_reminder():
    planner, ctx, request, enqueued = prepared()
    request.messages += [
        ModelResponse(parts=[CompactionPart(content="Historical summary")]),
        ModelRequest(parts=[UserPromptPart(content="Continue")]),
    ]
    await planner.before_model_request(ctx, request)
    assert len(enqueued) == 1
    enqueued.clear()
    request.messages = [ModelRequest(parts=[UserPromptPart(content="Fresh run")])]
    await planner.before_model_request(ctx, request)
    assert len(enqueued) == 1


@run_async
async def test_more_recent_plan_reminder_takes_precedence_over_older_code_output():
    planner, ctx, request, enqueued = prepared()
    request.messages.append(
        ModelRequest(
            parts=[
                UserPromptPart(
                    content=[
                        TextContent(
                            framed_plan([PlanItem(id="new", content="A more recent different plan")]),
                            metadata={"origin": PLAN_ORIGIN},
                        )
                    ]
                )
            ]
        )
    )
    await planner.before_model_request(ctx, request)
    assert len(enqueued) == 1
