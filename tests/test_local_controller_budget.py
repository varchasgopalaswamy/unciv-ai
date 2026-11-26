"""Real SDK failure accounting across automatic role retries."""

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.step_persistence import SqliteStepStore

from civ_agent.orchestration.contracts import GatewayReceipt, TurnTicket
from civ_agent.orchestration.job_contracts import NativeJob
from civ_agent.runtime.model_policy import FreshConversationRequired
from civ_agent.runtime.player import AttemptContext, LocalPlayer, Permit
from tests.test_local_agents import request
from tests.test_local_controller import config, seed


def test_role_continues_past_legacy_cap_across_conversations_and_keeps_usage(tmp_path):
    async def run():
        state = seed(tmp_path, config())
        await state.save()
        req = request().model_copy(update={"request_limit": 6})
        startup = {"user": "Review the position.", "instructions": "Use the completion schema."}
        catalogue = [{"name": "finish_role", "inputSchema": {}}]

        attempts = 0

        def model(_messages, _info):
            nonlocal attempts
            attempts += 1
            args = {} if attempts <= 30 else {"summary": "Completed after correcting the report."}
            return ModelResponse(parts=[ToolCallPart("finish_role", args, f"finish-{attempts}")])

        player: Any = object.__new__(LocalPlayer)
        player.root = SimpleNamespace(
            store=SimpleNamespace(
                root=tmp_path / "artifacts",
                get=lambda _scope, reference: startup if reference == req.startup else catalogue,
            ),
            check_role=AsyncMock(return_value=SimpleNamespace(finished=True)),
        )
        player.state = state
        player.permits, player._request_permits = {}, {}
        player.role_passes = {"gameplay": 0, "economy": 0, "military": 0}
        player.role_usage, player.role_requests = {}, {}
        player.steps = SqliteStepStore(database=tmp_path / "steps.sqlite3")
        player.gateway = None
        player.model_limiter = None
        player.model_factory = lambda _request: FunctionModel(model)
        permit = Permit(NativeJob(scope=req.scope, job=req.job, purpose=req.purpose), req.selection.model)
        context = AttemptContext(TurnTicket(scope=req.scope, turn=0, handoff=0, index=0), permit)

        async def compute(*_args, **_kwargs):
            pass

        player._compute = compute
        player._invoke = AsyncMock(return_value=GatewayReceipt(result={"roleFinished": True}))
        known = 0
        for conversation in range(6):
            with pytest.raises(UnexpectedModelBehavior):
                await player.run_request(req, permit=permit, context=context)
            assert context.allowances["gameplay"].provider_attempts == (conversation + 1) * 5
            assert permit.usage.input_tokens > known
            known = permit.usage.input_tokens
        result = await player.run_request(req, permit=permit, context=context)
        assert result.completion["roleFinished"]
        assert context.allowances["gameplay"].provider_attempts == state.current.model_requests == 31
        assert permit.usage.input_tokens > known
        assert context.allowances["gameplay"].usage.requests == 31
        player._invoke.assert_awaited_once()

    asyncio.run(run())


@pytest.mark.parametrize("attempt_limit", [1, 2])
def test_repeated_output_failures_stop_fresh_military_jobs_at_recorded_attempt_limit(monkeypatch, attempt_limit):
    async def run():
        req = request("military").model_copy(update={"attempt_limit": attempt_limit})
        parent = Permit(NativeJob(scope=req.scope, job="main", purpose="gameplay"), req.selection.model)
        context = AttemptContext(TurnTicket(scope=req.scope, turn=12, handoff=36, index=0), parent)
        failure = UnexpectedModelBehavior("Exceeded maximum output retries (4)")
        player: Any = object.__new__(LocalPlayer)
        player.root = SimpleNamespace(
            prepare_role=AsyncMock(
                side_effect=[req.model_copy(update={"job": f"military-{i}"}) for i in range(attempt_limit + 1)] + [None]
            )
        )
        player.run_request = AsyncMock(side_effect=failure)
        sleep = AsyncMock()
        monkeypatch.setattr("civ_agent.runtime.player.asyncio.sleep", sleep)

        with pytest.raises(UnexpectedModelBehavior) as caught:
            await player.run_role("military", context)

        assert caught.value is failure
        assert player.run_request.await_count == player.root.prepare_role.await_count == attempt_limit
        assert context.role_passes["military"] == attempt_limit
        assert [call.args[0].pass_number for call in player.root.prepare_role.await_args_list] == list(
            range(attempt_limit)
        )
        assert all(call.kwargs["permit"].job.parent == parent.job for call in player.run_request.await_args_list)
        assert sleep.await_count == attempt_limit - 1

    asyncio.run(run())


def test_fresh_context_restarts_and_successful_role_wakeups_keep_their_existing_behavior(monkeypatch):
    async def run():
        req = request("military")
        parent = Permit(NativeJob(scope=req.scope, job="main", purpose="gameplay"), req.selection.model)
        context = AttemptContext(TurnTicket(scope=req.scope, turn=12, handoff=36, index=0), parent)
        player: Any = object.__new__(LocalPlayer)
        player.root = SimpleNamespace(prepare_role=AsyncMock(return_value=req))
        completed = object()
        player.run_request = AsyncMock(
            side_effect=[
                FreshConversationRequired("REFRESH", retry_after=3),
                FreshConversationRequired("REFRESH"),
                UnexpectedModelBehavior("Invalid completion"),
                completed,
                UnexpectedModelBehavior("Invalid completion after new work"),
                completed,
            ]
        )
        sleep = AsyncMock()
        monkeypatch.setattr("civ_agent.runtime.player.asyncio.sleep", sleep)

        assert await player.run_role("military", context) is completed
        assert await player.run_role("military", context) is completed
        assert context.role_passes["military"] == player.run_request.await_count == 6
        assert [call.args[0] for call in sleep.await_args_list] == [3, 2, 2]

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["cancelled", "ROLE_CAPACITY_TOO_SMALL"])
def test_failed_native_claim_releases_local_compute_capacity(failure):
    async def run():
        player: Any = object.__new__(LocalPlayer)
        player.permits = {}
        player.capacity = asyncio.Semaphore(1)
        player.state = SimpleNamespace(role=AsyncMock())

        async def claim(_job):
            if failure == "cancelled":
                raise asyncio.CancelledError
            return SimpleNamespace(granted=False, reason=failure)

        player.admission = SimpleNamespace(claim=claim)
        req = request()
        permit = Permit(NativeJob(scope=req.scope, job=req.job, purpose=req.purpose), req.selection.model)
        with pytest.raises(asyncio.CancelledError if failure == "cancelled" else RuntimeError):
            await player._compute(permit, True)
        assert not player.capacity.locked() and not permit.computing

    asyncio.run(run())
