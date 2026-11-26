"""Real gateway guards and simulated native receipts under cross-role interleaving."""

import asyncio
import copy
from contextlib import asynccontextmanager

import pytest

from civ_agent.execution_windows import WindowBinding, describe_stale, record_invalidation
from civ_agent.orchestration.contracts import GatewayCall, GatewayJob, Scope
from civ_agent.orchestration.gateway_registry import GatewayRegistry
from civ_agent.orchestration.host_effect_contracts import NativeCommandApplication, NativeCommandFinish
from civ_agent.orchestration.host_effect_service import NativeCommandService
from civ_agent.orchestration.store import JournalConflict
from tests.domain_fixtures import command, managed, run_async
from tests.test_gateway_execution import executor
from tests.test_native_command_service import native, step


@asynccontextmanager
async def paired(tmp_path):
    async with managed(tmp_path) as m:
        military = await m.gateway.open("military", "military", "g1", m.host, {}, 0)
        m.roles.register(military)
        m.roles.ready["military"] = False
        original = m.host.tool

        async def host(name, arguments):
            if name == "player_receipt":
                return copy.deepcopy(m.host.seen.get(arguments["requestId"], {"error": "NOT_ENACTED"}))
            result = await original(name, arguments)
            if name == "player_command":
                result["epoch"] = arguments["epoch"]
            return result

        m.host.tool = host

        async def resolve(record):
            return m.roles.jobs[record.identity.purpose]

        owner = GatewayRegistry(m.gateway, game="game", player="p0", resolve=resolve)
        bindings = {}
        try:
            for job in (m.economy, military):
                _, state = await executor(m, job)
                binding = GatewayJob(
                    scope=Scope(game="game", player="p0", epoch="g1", activation="turn1"),
                    job=job.id,
                    purpose=job.purpose,
                )
                await owner.register(binding, state, await m.gateway.tools(job))
                bindings[job.purpose] = binding
            yield m, owner, NativeCommandService(owner), bindings
        finally:
            await owner.close()


@pytest.mark.parametrize("native_service", [False, True])
@pytest.mark.parametrize("focused_role", ["economy", "military"])
@pytest.mark.parametrize("focus", [False, True])
@run_async
async def test_read_action_window_prevents_own_role_invalidation_without_bypassing_guard(
    tmp_path, native_service, focused_role, focus
):
    async with paired(tmp_path) as (m, owner, service, bindings):
        read, queued = asyncio.Event(), asyncio.Event()
        other_role = "military" if focused_role == "economy" else "economy"
        selected, other = bindings[focused_role], bindings[other_role]
        actions = {
            "economy": command("SET_CITY_FOCUS", target="city2", selection="FoodFocus"),
            "military": command("MOVE_UNIT", target="unit1", destination={"x": 1, "y": 0}),
        }

        async def inspect(binding, call):
            result = await owner(
                binding, call, 1, {"kind": "inspect", "operations": [{"tool": "player_turn", "arguments": {}}]}
            )
            await owner.acknowledge_delivery(binding, call, 1)
            return result

        async def act(binding, call):
            decision = {"kind": "act", "operations": [actions[binding.purpose]]}
            if native_service:
                return await service.execute_local(
                    binding, GatewayCall(call_id=call, model_step=1, decision=decision), run_id="run1"
                )
            return await owner(binding, call, 1, decision)

        async def independent():
            await read.wait()
            queued.set()
            await inspect(other, "other-read")
            return await act(other, "other-action")

        # This task starts outside the private Code Mode context, as real
        # independently prepared roles do; it cannot inherit the focus binding.
        sibling = asyncio.create_task(independent())

        async def selected_work():
            await inspect(selected, "selected-read")
            read.set()
            await queued.wait()
            if focus:
                while not owner.windows.queue:
                    await asyncio.sleep(0)
                assert not owner.dispatch.locked(), "No dispatch/SQL lock is retained between calls"
            else:
                await sibling
            return await act(selected, "selected-action")

        if focus:
            bound = WindowBinding.create(selected, run_id="run1", parent_call_id="program1", code="read;check;act")
            async with owner.windows.focused(bound):
                result = await selected_work()
        else:
            result = await selected_work()
        await sibling
        row = result.result["receipts"][0]
        if focus:
            assert row["status"] == "confirmed"
            assert len(m.host.executed) == 2
        else:
            assert row["status"] == "unexecuted" and row["reason"] == "FRESH_OBSERVATION_REQUIRED"
            assert row["feedback"]["cause"] == "known_own_action"
            assert row["feedback"]["ownActions"][0]["role"] == other_role
            assert row["feedback"]["ownActions"][0]["operationId"] == m.host.executed[0]["requestId"]
            assert len(m.host.executed) == 1
        assert len({row["requestId"] for row in m.host.executed}) == len(m.host.executed)


@run_async
async def test_focused_window_does_not_grant_missing_previews_or_wrong_role_authority(tmp_path):
    async with paired(tmp_path) as (m, owner, service, bindings):
        selected = bindings["military"]
        bound = WindowBinding.create(selected, run_id="run1", parent_call_id="program1", code="act")
        for index, operation in enumerate(
            [
                command("SET_UNIT_DESTINATION", target="unit1", destination={"x": 1, "y": 0}),
                command("SET_CITY_FOCUS", target="city2", selection="FoodFocus"),
            ]
        ):
            async with owner.windows.focused(bound):
                call = GatewayCall(
                    call_id="guard" + str(index), model_step=1, decision={"kind": "act", "operations": [operation]}
                )
                if index == 0:
                    result = await service.execute_local(selected, call, run_id="run1")
                    row = result.result["receipts"][0]
                    assert row["reason"] == "FRESH_INSPECTION_REQUIRED"
                else:
                    with pytest.raises(ValueError, match="No matching union variant"):
                        await service.execute_local(selected, call, run_id="run1")
        assert not m.host.executed


def test_stale_feedback_requires_exact_linked_epoch_revision_provenance():
    from types import SimpleNamespace

    job = SimpleNamespace(epoch="g1", observation={"revision": "r1"}, prior_role_observation=None, memory={})
    by = {
        "epoch": "g1",
        "role": "economy",
        "tool": "player_command",
        "operationId": "request1",
        "fromRevision": "r1",
        "toRevision": "r2",
    }
    record_invalidation(job, by)
    job.prior_role_observation, job.observation = job.observation, None
    assert describe_stale(job, "r1")["cause"] == "known_own_action"
    assert describe_stale(job, "other")["cause"] == "unknown"
    record_invalidation(job, {**by, "fromRevision": "r2", "toRevision": "r3", "operationId": "request2"})
    assert len(describe_stale(job, "r1")["ownActions"]) == 2
    job.observation = {"revision": "external-r4"}
    assert describe_stale(job, "r1")["cause"] == "unknown"
    job.observation = None
    record_invalidation(job, {**by, "fromRevision": "r4", "toRevision": "r5"})
    assert describe_stale(job, "r1")["cause"] == "unknown"
    record_invalidation(job, {**by, "epoch": "different"})
    assert describe_stale(job, "r1")["cause"] == "unknown"


@run_async
async def test_two_command_focused_batch_charges_operations_once_and_releases_at_final_ack(tmp_path):
    async with native(tmp_path, purpose="gameplay") as m:
        bound = WindowBinding.create(m.identity, run_id="run1", parent_call_id="program1", code="commands")
        call = GatewayCall(
            call_id="two-commands",
            model_step=1,
            decision={
                "kind": "act",
                "operations": [command("DISMISS_NOTIFICATIONS", targets=[name]) for name in ("n1", "n2")],
            },
        )
        async with m.owner.windows.focused(bound, max_calls=2) as window:
            result = await m.service.execute_local(m.identity, call, run_id="run1")
            assert [row["status"] for row in result.result["receipts"]] == ["confirmed", "confirmed"]
            assert window.calls == 2
            assert window.ended == "native_batch_acknowledged"
        assert len(m.host.executed) == 2 and len(m.host.seen) == 2
        assert m.owner.windows.owner is None and not m.owner.windows.fenced


@run_async
async def test_successful_same_key_prepare_cannot_clear_lost_native_ack_fence(tmp_path):
    async with native(tmp_path) as m:
        request = step(m.identity)
        frozen = (await m.service.prepare(request)).effect
        assert frozen is not None
        parent = m.service.parent(request)

        async def lose_ack(_effect, _reply):
            raise OSError("lost acknowledgement after native commit")

        m.service.reply_checkpoint = lose_ack
        with pytest.raises(OSError):
            await m.service.execute(frozen)
        assert m.owner.windows.fenced == {parent} and len(m.host.executed) == 1
        # A hidden refresh makes preparation return a normal stale stop. It
        # neither delivers credit nor reconciles the already-committed request.
        await m.gateway.fresh_turn(m.economy, {}, "qualification-only")
        stopped = await m.service.prepare(request)
        assert stopped.stop == "FRESH_OBSERVATION_REQUIRED"
        assert m.owner.windows.fenced == {parent}
        with pytest.raises(JournalConflict, match="RECONCILIATION_REQUIRED"):
            await m.owner(
                m.identity, "new-work", 2, {"kind": "inspect", "operations": [{"tool": "player_turn", "arguments": {}}]}
            )
        m.service.reply_checkpoint = None
        original = await m.service.execute(frozen)
        assert m.owner.windows.fenced == {parent}, "A raw reply alone is not a committed domain acknowledgement"
        await m.service.apply(NativeCommandApplication(effect=frozen, receipt=original))
        assert not m.owner.windows.fenced and len(m.host.executed) == 1
        await m.service.finish(NativeCommandFinish(identity=m.identity, call=request.call))
        assert m.owner.load(m.identity).pending is None


@pytest.mark.parametrize("native_service", [False, True])
@run_async
async def test_expiry_after_first_commit_stops_fresh_batch_tail_and_retains_receipts(tmp_path, native_service):
    async with native(tmp_path, purpose="gameplay") as m:
        bound = WindowBinding.create(m.identity, run_id="run1", parent_call_id="program1", code="commands")
        operations = [command("DISMISS_NOTIFICATIONS", targets=[name]) for name in ("n1", "n2")]
        original = m.host.tool
        async with m.owner.windows.focused(bound) as window:

            async def expire_after_commit(name, arguments):
                result = await original(name, arguments)
                if name == "player_command":
                    window.end("expired")
                    assert m.owner.windows.owner is not None, "An admitted effect still owns settlement"
                return result

            m.host.tool = expire_after_commit
            if native_service:
                result = await m.service.execute_local(
                    m.identity,
                    GatewayCall(call_id="expiring", model_step=1, decision={"kind": "act", "operations": operations}),
                    run_id="run1",
                )
            else:
                result = await m.owner(m.identity, "expiring", 1, {"kind": "act", "operations": operations})
        assert [row["status"] for row in result.result["receipts"]] == ["confirmed", "unexecuted"]
        assert result.result["receipts"][1]["reason"] == "FOCUSED_WINDOW_EXPIRED"
        assert len(m.host.executed) == 1 and len(m.host.seen) == 1
        assert not m.owner.windows.fenced and m.owner.windows.owner is None


@run_async
async def test_advisory_batch_ack_does_not_refresh_native_state_or_change_delivered_authority(tmp_path, monkeypatch):
    async with native(tmp_path) as m:
        request = step(m.identity)
        await m.service.prepare(request)
        before = m.owner.load(m.identity).state

        async def no_connection_refresh(_record):
            raise AssertionError("An advisory acknowledgement must not reconnect or inspect native state")

        monkeypatch.setattr(m.service, "_job", no_connection_refresh)
        await m.service._acknowledge_attempts(
            m.identity,
            request.call,
            [{"operationId": m.service.child(request), "status": "unexecuted", "reason": "FRESH_OBSERVATION_REQUIRED"}],
            decision_id=m.service.parent(request),
        )
        after = m.owner.load(m.identity).state
        assert after.delivered_access == before.delivered_access
        assert after.pending_access == before.pending_access and after.queries == before.queries
        assert after.data["observation"] == before.data["observation"]
        assert after.data["memory"]["unexecutedActionIntents"][0]["status"] == "unexecuted"
        assert not m.host.executed
