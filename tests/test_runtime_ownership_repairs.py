"""Desired-behavior regressions for the audited ownership boundaries."""

import asyncio
import json
import threading
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from civ_agent.orchestration.contracts import EventNotice, ModelSelection, Reference, TurnTicket
from civ_agent.orchestration.job_contracts import NativeJob
from civ_agent.runtime import state as state_module
from civ_agent.runtime.player import AttemptContext, LocalPlayer, Permit, PermitState
from tests.test_local_agents import request
from tests.test_local_controller import LocalController, Operator, Player, config, seed


def context():
    req = request()
    return AttemptContext(
        TurnTicket(scope=req.scope, turn=0, handoff=0, index=0),
        Permit(NativeJob(scope=req.scope, job=req.job, purpose=req.purpose), req.selection.model),
    )


def test_repeated_cancellation_drains_old_save_before_newer_state(tmp_path, monkeypatch):
    async def run():
        store = seed(tmp_path, config())
        accepted, release = threading.Event(), threading.Event()
        original = state_module.write_private_json

        def delayed(path, value):
            if path.name == "controller.json" and value["model_requests"] == 1:
                accepted.set()
                assert release.wait(5)
            original(path, value)

        monkeypatch.setattr(state_module, "write_private_json", delayed)
        first = asyncio.create_task(store.save(lambda state: setattr(state, "model_requests", 1)))
        await asyncio.to_thread(accepted.wait, 5)
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()
        second = asyncio.create_task(store.save(lambda state: setattr(state, "model_requests", 2)))
        await asyncio.sleep(0)
        assert not second.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await second
        assert json.loads(store.path.read_text())["model_requests"] == 2
        assert json.loads(store.status_path.read_text())["model_requests"] == 2
        await store.close()

    asyncio.run(run())


def test_lost_release_ack_reuses_the_frozen_envelope_and_prunes_after_publication():
    async def run():
        player: Any = object.__new__(LocalPlayer)
        player.state = SimpleNamespace(role=AsyncMock())
        permit = context().parent
        permit.lifecycle = PermitState.WAITING
        player.permits = {permit.job.job: permit}
        envelopes = []

        async def release(report):
            envelopes.append(report.model_dump_json())
            if len(envelopes) == 1:
                raise ConnectionError("ACK_LOST")
            return SimpleNamespace(granted=True)

        player.admission = SimpleNamespace(release=release)
        with pytest.raises(ConnectionError):
            await player._report(permit, "COMPLETED", release=True, complete=True)
        assert permit.lifecycle == PermitState.RELEASE_UNCERTAIN
        permit.requests += 1
        await player._report(permit, "INTERRUPTED", release=True)
        assert envelopes[0] == envelopes[1]
        assert permit.released and not player.permits

    asyncio.run(run())


def test_handoff_attempts_continue_past_the_old_boundary():
    async def run():
        player: Any = object.__new__(LocalPlayer)
        player.state = SimpleNamespace(current=SimpleNamespace(active_attempt=object()))
        player.handoffs = SimpleNamespace(
            prepare=AsyncMock(return_value=SimpleNamespace(result=SimpleNamespace(committed=False)))
        )
        for _ in range(64):
            attempt = context()
            for index in range(80):
                assert not await player.handoff(attempt)
                assert player.handoffs.prepare.await_args.args[0].attempt == index

    asyncio.run(run())


def test_legacy_sdk_totals_remain_unknown_while_new_observations_accumulate(tmp_path):
    state = seed(tmp_path, config())
    player: Any = object.__new__(LocalPlayer)
    player.state = state
    permit = context().parent
    deadline = state.current.deadline
    player._observe_sdk_requests(permit, 3)
    assert state.current.sdk_requests is None
    assert state.current.sdk_requests_observed == permit.sdk_requests == 3
    assert state.current.deadline == deadline
    asyncio.run(state.close())


def test_communication_exhaustion_defers_the_batch_durably_without_replacement(tmp_path, monkeypatch):
    from civ_agent.orchestration.communication_store import communication_scope
    from civ_agent.runtime import controller as controller_module
    from civ_agent.runtime.model_policy import FreshConversationRequired
    from civ_agent.runtime.state import ControllerStore

    async def run():
        scope = communication_scope("game", "p0", "g1")
        notice = EventNotice(scope=scope, id="a" * 64, native_id=1, content=Reference(sha256="b" * 64))
        state = seed(tmp_path, config())
        controller: Any = object.__new__(LocalController)
        controller.store, controller.closing = state, False
        collected = 0

        async def collect(_run):
            nonlocal collected
            collected += 1
            if collected == 3:
                controller.closing = True

        service = SimpleNamespace(
            collect=collect,
            pending=AsyncMock(return_value=[notice]),
            prepare=AsyncMock(return_value=request().model_copy(update={"purpose": "communication"})),
            store=lambda _scope: SimpleNamespace(signaled=lambda _notice: None),
        )
        player = SimpleNamespace(
            session=SimpleNamespace(
                game="game",
                player="p0",
                get_selection=lambda _purpose: ModelSelection(model="test"),
                get_request_limit=lambda _purpose: 24,
            ),
            root=SimpleNamespace(
                communication=service,
                io=SimpleNamespace(call=AsyncMock()),
                registry=SimpleNamespace(retire=AsyncMock()),
            ),
            communication_gate=asyncio.Event(),
            run_request=AsyncMock(side_effect=FreshConversationRequired("model_wait")),
            permits={},
        )
        player.communication_gate.set()
        incoming = SimpleNamespace(
            plan=AsyncMock(return_value=SimpleNamespace(classifications=[], consumed=[], ready=[notice]))
        )
        monkeypatch.setattr(controller_module, "NativeIncoming", lambda _service: incoming)
        await controller._background(player)
        player.run_request.assert_awaited_once()
        deferred = state.current.communication_deferrals["p0"]
        assert deferred.status == "UNAVAILABLE" and deferred.next_handoff == 1
        assert deferred.notices == [notice.model_dump(mode="json")]
        reloaded = ControllerStore(tmp_path)
        loaded = reloaded.load()
        assert loaded is not None and loaded.communication_deferrals["p0"] == deferred
        await reloaded.close()
        await state.close()

    asyncio.run(run())


@pytest.mark.parametrize("error_type", [ValueError, RuntimeError])
def test_required_worker_failure_interrupts_active_main_and_keeps_leaf_evidence(tmp_path, error_type):
    async def run():
        op = Operator()
        player: Any = Player(op)
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def role(purpose, context):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def background(player):
            await started.wait()
            raise ExceptionGroup("SECRET_SENTINEL", [error_type("SECRET_SENTINEL")])

        player.run_role = role
        controller = LocalController(tmp_path, config(), {"id": "game", "launch": "one"}, op, players={"Rome": player})
        controller._background = background
        result = await asyncio.wait_for(controller.run(), 5)
        assert cancelled.is_set() and player.dispatch_fenced
        assert result.mode == "FAILED"
        assert result.reason == error_type.__name__
        for name in ("controller.json", "status.json"):
            assert "SECRET_SENTINEL" not in (tmp_path / "native-runtime" / name).read_text()
        saved = (tmp_path / "native-runtime/failure.json").read_text()
        assert "SECRET_SENTINEL" not in saved
        assert json.loads(saved)["exception"]["exceptions"][0]["type"] == f"builtins.{error_type.__name__}"

    asyncio.run(run())


def test_shutdown_closes_all_resources_after_independent_cleanup_failures(tmp_path):
    async def run():
        closed = []

        async def close(name, error=None):
            closed.append(name)
            if error is not None:
                raise error

        controller: Any = object.__new__(LocalController)
        controller.store = seed(tmp_path, config())
        controller.op = AsyncMock(side_effect=ConnectionError("SECRET_BARRIER"))
        controller.communication_tasks = []
        controller.reviews = SimpleNamespace(close=lambda: close("reviews", ValueError("SECRET_REVIEW")))
        controller.players = {
            "Rome": SimpleNamespace(close=lambda: close("Rome", RuntimeError("SECRET_PLAYER"))),
            "Greece": SimpleNamespace(close=lambda: close("Greece")),
        }
        publish = AsyncMock(side_effect=OSError("SECRET_PUBLISH"))
        controller.publisher = SimpleNamespace(publish=publish, close=lambda: close("publisher"))
        await controller._shutdown()
        publish.assert_awaited_once()
        assert set(closed) == {"reviews", "Rome", "Greece", "publisher"}
        assert controller.store.io.closed
        saved = (tmp_path / "native-runtime/cleanup-failure.json").read_text()
        assert "SECRET_" not in saved
        assert {error["type"] for error in json.loads(saved)["exceptions"]} == {
            "builtins.ConnectionError",
            "builtins.OSError",
            "builtins.ValueError",
            "builtins.RuntimeError",
        }

    asyncio.run(run())


@given(st.lists(st.booleans(), min_size=64, max_size=80))
@settings(max_examples=5, deadline=None)
def test_same_player_skips_reopens_and_attempts_keep_unbounded_pass_identities(skips):
    async def run():
        player: Any = object.__new__(LocalPlayer)
        player.economy_lock = asyncio.Lock()
        player.run_request = AsyncMock()
        previous = None
        for turn, skip in enumerate(skips):
            current = context()
            current.ticket.turn = turn
            player.root = SimpleNamespace(prepare_role=AsyncMock(return_value=None))
            if skip:
                await player.run_role("economy", current)
                assert current.role_passes["economy"] == 0
            player.root.prepare_role.return_value = request().model_copy(update={"purpose": "economy"})
            for index in range(65):
                await player.run_role("economy", current)
                assert player.root.prepare_role.await_args.args[0].pass_number == index
            player.root.prepare_role.return_value = None
            assert await player.run_role("economy", current) is None
            if previous is not None:
                assert previous.role_passes["economy"] == 65
            previous = current

    asyncio.run(run())


class PermitTransitions(RuleBasedStateMachine):
    player: Any

    def __init__(self):
        super().__init__()
        self.player = cast(Any, object.__new__(LocalPlayer))
        self.player.state = SimpleNamespace(role=AsyncMock())
        self.permit = context().parent
        self.player.permits = {self.permit.job.job: self.permit}
        self.player.admission = SimpleNamespace(
            claim=AsyncMock(return_value=SimpleNamespace(granted=True)),
            report=AsyncMock(return_value=SimpleNamespace(granted=True)),
            release=AsyncMock(return_value=SimpleNamespace(granted=True)),
        )

    @rule(acquire=st.booleans())
    def compute(self, acquire):
        if acquire and (
            self.permit.released or (self.permit.pending_release and self.permit.pending_report is not None)
        ):
            with pytest.raises(RuntimeError, match="LOCAL_JOB_RELEASED"):
                asyncio.run(self.player._compute(self.permit, acquire))
        else:
            asyncio.run(self.player._compute(self.permit, acquire))

    @rule()
    def release(self):
        asyncio.run(self.player._report(self.permit, "INTERRUPTED", release=True))

    @rule()
    def lost_claim_ack(self):
        if self.permit.released or self.permit.pending_report is not None:
            return
        self.permit.lifecycle = PermitState.WAITING if self.permit.admitted else PermitState.NEW
        self.player.admission.claim.side_effect = ConnectionError("ACK_LOST")
        try:
            with pytest.raises(ConnectionError):
                asyncio.run(self.player._compute(self.permit, True))
            assert self.permit.lifecycle == PermitState.CLAIM_UNCERTAIN
            assert self.permit.job.job in self.player.permits
        finally:
            self.player.admission.claim.side_effect = None

    @rule()
    def lost_release_ack(self):
        if not self.permit.admitted or self.permit.released:
            return
        self.player.admission.release.side_effect = ConnectionError("ACK_LOST")
        try:
            with pytest.raises(ConnectionError):
                asyncio.run(self.player._report(self.permit, "INTERRUPTED", release=True))
            assert self.permit.pending_report is not None
            assert self.permit.job.job in self.player.permits
        finally:
            self.player.admission.release.side_effect = None

    @invariant()
    def released_authority_never_computes(self):
        assert not (self.permit.released and self.permit.computing)
        if self.permit.lifecycle == PermitState.RELEASED:
            assert self.permit.job.job not in self.player.permits


TestPermitTransitions = PermitTransitions.TestCase
TestPermitTransitions.settings = settings(max_examples=30, stateful_step_count=30, deadline=None)
