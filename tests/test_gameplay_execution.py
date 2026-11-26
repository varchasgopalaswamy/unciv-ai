import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from civ_agent.gameplay_execution import EvaluationController
from civ_agent.provider_attempts import AttemptAdmission, ProviderAttemptAccount
from civ_agent.runtime.review_cycles import ReviewScheduler
from civ_agent.runtime.state import ControllerStore
from tests.domain_fixtures import run_async
from tests.test_local_controller import config


@run_async
async def test_normal_evaluation_shutdown_drains_admitted_review_before_native_barrier(tmp_path):
    budget = ProviderAttemptAccount(tmp_path / "provider.sqlite3")
    entered, complete = asyncio.Event(), asyncio.Event()

    async def model(messages, info):
        entered.set()
        await complete.wait()
        return ModelResponse(parts=[TextPart("Reviewed.")])

    agent = Agent(FunctionModel(model), capabilities=[AttemptAdmission(budget=budget, namespace="review")])
    review = asyncio.create_task(agent.run("Assess."))
    await entered.wait()
    controller = EvaluationController(tmp_path, config(), {}, None, stop_requested=lambda: True)
    state = SimpleNamespace(mode="STOPPED", lease_id="owned-lease")
    controller.store = cast(ControllerStore, SimpleNamespace(state=state, close=AsyncMock()))
    controller.communications = AsyncMock()
    controller.reviews = cast(ReviewScheduler, SimpleNamespace(tasks={"review": review}, close=AsyncMock()))

    async def operator(name, **fields):
        assert review.done(), "Native pause/barrier must follow the settled provider stream."

    controller.op = AsyncMock(side_effect=operator)
    shutdown = asyncio.create_task(controller._shutdown())
    await asyncio.sleep(0)
    assert not shutdown.done() and not review.cancelled()
    assert budget.report()["state"] == "OPEN"
    complete.set()
    await asyncio.wait_for(shutdown, 2)
    report = budget.report()
    assert report["state"] == "OPEN" and report["attempts"] == 1
    assert report["components"]["review"]["completed"] == 1
    controller.op.assert_awaited_once_with("barrier", leaseId="owned-lease")


def test_requested_cohort_stop_is_checked_without_native_pause(tmp_path):
    controller = EvaluationController(tmp_path, config(), {}, None, stop_requested=lambda: True)
    assert controller._limit() == "EVALUATION_COHORT_STOP"
