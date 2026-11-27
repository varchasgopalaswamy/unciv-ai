"""Controller scheduling, explicit restart authority and native handoff decisions."""

import asyncio
import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from civ_agent.execution_policy import ExecutionPolicy, legacy_execution_policy
from civ_agent.game_config import GameConfig
from civ_agent.player_paths import PlayerPaths
from civ_agent.runtime.controller import LocalController, build_players, configuration_digest, limit_reason
from civ_agent.runtime.player import LocalPlayer
from civ_agent.runtime.state import AttemptRecord, ControllerState, ControllerStore, RoleStatus


@pytest.mark.parametrize("custom_socket", [False, True])
def test_heartbeat_starts_before_awaiting_player_provider_preparation(tmp_path, monkeypatch, custom_socket):
    async def run():
        renewed = asyncio.Event()
        socket = tmp_path / "private-broker.sock" if custom_socket else None
        operator = Operator()
        operator.status["phase"] = "FINISHED"
        original = operator.call

        def call(name, **fields):
            if name == "heartbeat":
                renewed.set()
            return original(name, **fields)

        operator.call = call

        async def prepare(*args, **kwargs):
            assert kwargs["advisory_socket"] == socket
            # Before this fix startup waited here without any renewal task.
            await asyncio.wait_for(renewed.wait(), 1)
            operator.status["phase"] = "FINISHED"
            return {}

        class Publisher:
            async def start(self):
                pass

            async def run(self):
                await asyncio.Event().wait()

            async def publish(self):
                pass

            async def close(self):
                pass

        monkeypatch.setattr("civ_agent.runtime.controller.build_players", prepare)
        monkeypatch.setattr("civ_agent.operator_views.OperatorPublisher", lambda *args, **kwargs: Publisher())
        controller = LocalController(tmp_path, config(), {"id": "startup-heartbeat"}, operator, advisory_socket=socket)
        state = await controller.run()
        assert state.mode == "FINISHED" and renewed.is_set()

    asyncio.run(run())


class Operator:
    def __init__(self):
        self.status = {
            "epoch": "g1",
            "turn": 0,
            "handoff": 0,
            "phase": "PAUSED",
            "activeController": "AGENT",
            "activeCivilization": "Rome",
            "serverInstance": "native1",
            "modelInvocations": 0,
        }
        self.calls = []
        self.world = {"city": "Rome", "messages": ["Already delivered"], "queue": ["Monument"]}

    def call(self, name, **fields):
        self.calls.append((name, fields))
        if name == "barrier" and self.status["phase"] != "FINISHED":
            self.status["phase"] = "PAUSED"
        return {"status": copy.deepcopy(self.status)}


class Player(LocalPlayer):
    def __init__(self, operator):
        self.operator = operator

        async def retire(_ticket):
            return copy.deepcopy(operator.status)

        self.root = SimpleNamespace(civilization="Rome", routing=None, retire_local_attempt=retire)
        self.session = SimpleNamespace(
            game="game", player="p0", settings=SimpleNamespace(execution_policy=legacy_execution_policy())
        )
        self.tasks = set()
        self.started = []
        self.recovered = []
        self.arrived = asyncio.Event()
        self.specialists = 0
        self.held = True
        self.closed = False
        self.roles = SimpleNamespace(
            ready=dict.fromkeys(("gameplay", "economy", "military"), False), changed=asyncio.Event()
        )

    async def begin(self, attempt) -> Any:
        self.started.append(attempt.id)

    async def prepare_roles(self, context):
        return self.roles

    async def hold_communication(self):
        self.held = True

    async def release_communication(self):
        self.held = False

    async def run_role(self, purpose, context):
        if purpose in {"economy", "military"}:
            self.specialists += 1
            if self.specialists == 2:
                self.arrived.set()
            await asyncio.wait_for(self.arrived.wait(), 1)
        self.roles.ready[purpose] = True
        self.roles.changed.set()

    async def handoff(self, context, on_committed=None):
        assert self.held
        self.operator.status.update(turn=1, handoff=1, phase="FINISHED")
        return True

    async def recover(self, attempt):
        self.recovered.append(attempt.id)

    async def close(self):
        self.closed = True


def config():
    return GameConfig().resolved()


async def sleeping_background(player):
    await asyncio.Event().wait()


def seed(root, requested, *, authorization=None):
    store = ControllerStore(root)
    now = datetime.now(UTC)
    store.state = ControllerState(
        game="game",
        epoch="g1",
        lease_id="lease",
        config_sha256=configuration_digest(requested),
        initial_started_at=now,
        deadline=now + timedelta(hours=1),
        initial_turn=0,
        mode="RUNNING",
        limits=requested.limits.model_dump(mode="json"),
        resume_authorization=authorization,
        active_attempt=AttemptRecord(
            id="old", player="p0", civilization="Rome", epoch="g1", turn=0, handoff=0, status="RUNNING"
        ),
    )
    return store


def test_desktop_progress_projects_only_current_ai_turn_stages(tmp_path):
    async def run():
        store = seed(tmp_path, config())
        state = store.current
        state.active_civilization, state.active_controller = "Rome", "AGENT"
        try:
            for phase, stage in (
                ("PREPARING", "PREPARING"),
                ("WAITING_REVIEW", "REVIEWING"),
                ("ROLE_WORK", "PLAYING"),
                ("MAIN_FOLLOWUP", "PLAYING"),
                ("HANDOFF", "FINISHING"),
            ):
                await store.phase(phase)
                assert json.loads(store.desktop_progress_path.read_text()) == {
                    "epoch": "g1",
                    "turn": 0,
                    "handoff": 0,
                    "stage": stage,
                }
            await store.phase("ROLE_WORK")
            await store.role(RoleStatus(purpose="gameplay", job="main", phase="WAITING_FOR_MODEL", model="private"))
            await store.role(RoleStatus(purpose="communication", job="dm", phase="INFERENCE", model="private"))
            assert json.loads(store.desktop_progress_path.read_text())["stage"] == "WAITING_FOR_MODEL"
            await store.role(RoleStatus(purpose="economy", job="economy", phase="INFERENCE", model="private"))
            assert json.loads(store.desktop_progress_path.read_text())["stage"] == "PLAYING"
            state.handoff += 1
            await store.save()
            assert not store.desktop_progress_path.exists()
        finally:
            await store.close()

    asyncio.run(run())


def test_shutdown_publishes_terminal_state_before_closing_histories(tmp_path):
    async def run():
        requested = config()
        controller = LocalController(tmp_path, requested, {"id": "game"}, Operator())
        controller.store = seed(tmp_path, requested)
        controller.store.current.mode = "FAILED"
        controller.store.current.phase = "FAILED"
        controller.store.current.reason = "MODEL_PROVIDER_ERROR"
        order = []

        class Publisher:
            async def publish(self):
                assert not player.closed
                assert controller.store.current.mode == "FAILED"
                assert controller.store.current.reason == "MODEL_PROVIDER_ERROR"
                order.append("published")

            async def close(self):
                order.append("closed")

        player = Player(controller.operator)
        controller.players = {"Rome": player}
        controller.publisher = Publisher()
        await controller._shutdown()
        assert order == ["published", "closed"]
        assert player.closed

    asyncio.run(run())


@pytest.mark.parametrize("custom_socket,enabled", [(False, True), (True, True), (True, False)])
def test_build_players_scopes_advisory_socket_and_preserves_frozen_seat_settings(tmp_path, custom_socket, enabled):
    async def run():
        direct, code_mode = ExecutionPolicy(), ExecutionPolicy.current()
        requested = GameConfig.model_validate(
            {
                "game": {"master_seed": 42, "civilizations": ["Rome", "Greece"]},
                "agents": {
                    "model": "scripted-v1",
                    "decisions_enabled": enabled,
                    "decisions_effort_enabled": enabled,
                    "decisions_backend": "jev",
                },
                "players": [
                    {"name": "Direct", "execution_policy": direct.model_dump(mode="json")},
                    {"name": "Code Mode", "execution_policy": code_mode.model_dump(mode="json")},
                ],
                "runtime": {"scripted_models": True},
            }
        ).resolved()
        operator = Operator()
        operator.status["leaseId"] = "lease"
        state = seed(tmp_path, requested)
        socket = tmp_path / "private-broker.sock" if custom_socket else None
        players = {}
        try:
            players = await build_players(
                tmp_path, requested, {"id": "game"}, operator, state, None, None, advisory_socket=socket
            )
            assert [seat["session"]["settings"]["execution_policy"]["version"] for seat in state.current.seats] == [
                1,
                2,
            ]
            for index, (civilization, player) in enumerate(players.items()):
                settings = player.session.settings
                assert settings.execution_policy == (direct if index == 0 else code_mode)
                assert settings.decisions_enabled == enabled
                assert settings.decisions_effort_enabled == enabled
                assert settings.decisions_backend == "jev"
                if enabled:
                    broker = player.root.gateway.routing.broker
                    assert broker is player.root.gateway.advisories.services.broker
                    assert broker.socket_path == str(socket or Path("/advisory-ipc/broker.sock"))
                    assert broker.token_file == (
                        PlayerPaths(tmp_path, index, civilization).coordinator_security / "decisions-client.json"
                    )
                    assert broker.backend == "jev"
                    assert broker.last_health == {"status": "unchecked", "reason": "not_checked"}
                else:
                    assert player.root.gateway.routing is None and player.root.gateway.advisories is None
            assert all(name == "status" for name, _fields in operator.calls)
        finally:
            for player in players.values():
                await player.close()
            await state.io.close()

    asyncio.run(run())


def test_specialists_overlap_and_join_before_native_handoff(tmp_path):
    async def run():
        operator = Operator()
        player = Player(operator)
        controller = LocalController(
            tmp_path, config(), {"id": "game", "launch": "one"}, operator, players={"Rome": player}
        )
        controller._background = sleeping_background
        result = await controller.run()
        assert result.mode == "FINISHED"
        assert player.specialists == 2 and player.closed
        assert result.completed_attempts[0].status == "COMPLETED"
        assert not any(name == "restore" for name, _ in operator.calls)

    asyncio.run(run())


def test_automatic_process_restart_cannot_restart_an_unfinished_attempt(tmp_path):
    async def run():
        requested, operator = config(), Operator()
        state = seed(tmp_path, requested)
        await state.save()
        player = Player(operator)
        controller = LocalController(
            tmp_path, requested, {"id": "game", "launch": "one"}, operator, players={"Rome": player}
        )
        result = await controller.run(resume=False)
        assert result.mode == "PAUSED" and result.reason == "EXPLICIT_RESUME_REQUIRED"
        assert not player.started and not player.recovered

    asyncio.run(run())


def test_explicit_resume_preserves_world_spend_deadline_and_uses_new_attempt(tmp_path):
    async def run():
        requested, operator = config(), Operator()
        state = seed(tmp_path, requested)
        state.current.model_invocations = 19
        deadline = state.current.deadline
        await state.save()
        player = Player(operator)

        # The communication service is a separate transport; recovery only resets
        # unfinished delivery assignments, never native messages.
        async def restart(_scope):
            return None

        player.root.communication = SimpleNamespace(restart=restart)
        world = copy.deepcopy(operator.world)
        controller = LocalController(
            tmp_path, requested, {"id": "game", "launch": "resume1"}, operator, players={"Rome": player}
        )
        controller._background = sleeping_background
        result = await controller.run(resume=True)
        assert result.mode == "FINISHED"
        assert player.recovered == ["old"] and player.started[0] != "old"
        assert operator.world == world
        assert result.epoch == "g1" and result.model_invocations == 19 and result.deadline == deadline
        assert result.resume_authorization == "resume1"

    asyncio.run(run())


def test_resume_authorization_is_not_reused_after_another_process_crash(tmp_path):
    async def run():
        requested, operator = config(), Operator()
        state = seed(tmp_path, requested, authorization="resume1")
        await state.save()
        player = Player(operator)
        controller = LocalController(
            tmp_path, requested, {"id": "game", "launch": "resume1"}, operator, players={"Rome": player}
        )
        result = await controller.run(resume=True)
        assert result.reason == "EXPLICIT_RESUME_REQUIRED"
        assert not player.started and not player.recovered

    asyncio.run(run())


@pytest.mark.parametrize("notification", ["command_error", "heartbeat"])
def test_mid_turn_victory_settles_work_and_completes_without_handoff(tmp_path, notification):
    from civ_agent.operator_connection import OperatorError

    async def run():
        operator = Operator()
        cancelled = asyncio.Event()

        class WinningPlayer(Player):
            async def run_roles(self, context):
                operator.status["phase"] = "FINISHED"
                if notification == "command_error":
                    raise ExceptionGroup("post-victory work", [OperatorError("private", "PLAYER_UNAVAILABLE", 409)])
                await controller.observed_status("heartbeat", leaseId="lease")
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

            async def handoff(self, context, on_committed=None):
                raise AssertionError("A native victory requires no END_TURN.")

        player = WinningPlayer(operator)
        controller = LocalController(tmp_path, config(), {"id": "game"}, operator, players={"Rome": player})
        controller._background = sleeping_background
        world = copy.deepcopy(operator.world)
        result = await controller.run()
        assert (result.mode, result.phase, result.reason) == ("FINISHED", "COMPLETED", None)
        assert result.handoff == 0 and operator.world == world
        assert player.dispatch_fenced and player.recovered == player.started
        assert result.active_attempt is None and result.completed_attempts[-1].status == "COMPLETED"
        assert not (tmp_path / "native-runtime/failure.json").exists()
        if notification == "heartbeat":
            assert cancelled.is_set(), "The active role must quiesce before its receipt recovery."

    asyncio.run(run())


def test_resume_of_completed_native_game_settles_old_attempt_without_new_models(tmp_path):
    async def run():
        requested, operator = config(), Operator()
        state = seed(tmp_path, requested)
        state.current.mode, state.current.phase, state.current.reason = "FAILED", "FAILED", "ExceptionGroup"
        state.current.model_requests = 19
        deadline = state.current.deadline
        await state.save()
        operator.status.update(phase="FINISHED", turn=237, handoff=711)
        player = Player(operator)
        controller = LocalController(
            tmp_path, requested, {"id": "game", "launch": "finish1"}, operator, players={"Rome": player}
        )

        async def forbidden(player=None):
            raise AssertionError("A completed game must not start models, communication, or reviews.")

        controller._prepare_reviews = forbidden
        controller._background = forbidden
        result = await controller.run(resume=True)
        assert result.mode == "FINISHED" and result.phase == "COMPLETED"
        assert result.epoch == "g1" and result.deadline == deadline and result.model_requests == 19
        assert result.turn == 237 and result.handoff == 711 and result.active_attempt is None
        assert not player.started and player.recovered == ["old"]
        assert result.completed_attempts[-1].status == "COMPLETED"

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["ACTIVE", "HALTED"])
def test_player_unavailable_without_native_completion_remains_a_failure(tmp_path, phase):
    from civ_agent.operator_connection import OperatorError

    async def run():
        operator = Operator()

        class RejectedPlayer(Player):
            async def run_roles(self, context):
                operator.status["phase"] = phase
                raise OperatorError("private rejection", "PLAYER_UNAVAILABLE", 409)

        player = RejectedPlayer(operator)
        controller = LocalController(tmp_path, config(), {"id": "game"}, operator, players={"Rome": player})
        controller._background = sleeping_background
        result = await controller.run()
        assert result.mode == "FAILED" and not player.recovered
        assert (tmp_path / "native-runtime/failure.json").exists()

    asyncio.run(run())


def test_unsettled_winning_receipt_prevents_successful_terminal_publication(tmp_path):
    from civ_agent.operator_connection import OperatorError

    async def run():
        operator = Operator()

        class UnsettledPlayer(Player):
            async def run_roles(self, context):
                operator.status["phase"] = "FINISHED"
                raise OperatorError("private", "PLAYER_UNAVAILABLE", 409)

            async def recover(self, attempt):
                raise RuntimeError("NATIVE_RECEIPT_UNCERTAIN")

        player = UnsettledPlayer(operator)
        controller = LocalController(tmp_path, config(), {"id": "game"}, operator, players={"Rome": player})
        controller._background = sleeping_background
        result = await controller.run()
        assert result.mode == "FAILED" and result.active_attempt is not None
        assert (tmp_path / "native-runtime/failure.json").exists()

    asyncio.run(run())


def test_phase_clocks_survive_heartbeats_and_change_only_with_phase(tmp_path):
    async def run():
        state = seed(tmp_path, config())
        await state.save()
        global_started = state.current.phase_started_at
        await state.save(lambda value: setattr(value, "model_requests", 1))
        assert state.current.phase_started_at == global_started
        first = RoleStatus(purpose="gameplay", job="main", phase="RUNNING")
        await state.role(first)
        await state.role(RoleStatus(purpose="gameplay", job="main", phase="RUNNING", model_requests=1))
        assert state.current.active_attempt is not None
        same = state.current.active_attempt.roles[0]
        assert same.started_at == first.started_at and same.phase_started_at == first.phase_started_at
        await state.role(RoleStatus(purpose="gameplay", job="main", phase="WAITING_FOR_MODEL"))
        assert state.current.active_attempt is not None
        waiting = state.current.active_attempt.roles[0]
        assert waiting.started_at == first.started_at and waiting.phase_started_at > first.phase_started_at
        await state.phase("MAIN_FOLLOWUP")
        assert state.current.phase_started_at > global_started
        loaded = ControllerStore(tmp_path).load()
        assert loaded is not None and loaded.active_attempt is not None
        assert loaded.phase_started_at == state.current.phase_started_at
        assert loaded.active_attempt.roles[0].phase_started_at == waiting.phase_started_at

    asyncio.run(run())


def test_concurrent_observers_serialize_request_and_apply_of_delayed_native_status(tmp_path):
    async def run():
        controller = LocalController(tmp_path, config(), {"id": "game"}, Operator())
        controller.store = seed(tmp_path, config())
        await controller.store.save()
        captured, release, second_requested = asyncio.Event(), asyncio.Event(), asyncio.Event()
        requests = 0

        async def op(name, **fields):
            nonlocal requests
            requests += 1
            snapshot = copy.deepcopy(controller.operator.status)
            if requests == 1:
                captured.set()
                await release.wait()
            else:
                second_requested.set()
                snapshot.update(handoff=1, turn=1)
            return {"status": snapshot}

        controller.op = op
        first = asyncio.create_task(controller.observed_status("heartbeat"))
        await captured.wait()
        second = asyncio.create_task(controller.observed_status("heartbeat"))
        await asyncio.sleep(0)
        assert not second_requested.is_set()
        release.set()
        await asyncio.gather(first, second)
        assert controller.store.current.handoff == controller.store.current.turn == 1
        assert second_requested.is_set()

    asyncio.run(run())


def test_limit_classification_does_not_raise_on_arbitrary_bug_details():
    deadline = datetime.now(UTC) + timedelta(seconds=1)
    assert limit_reason(RuntimeError({"internal": "details"}), deadline) is None
    assert limit_reason(RuntimeError("ROLE_CAPACITY_TOO_SMALL"), deadline) == "ROLE_CAPACITY_TOO_SMALL"
    assert limit_reason(RuntimeError("MODEL_INVOCATION_LIMIT"), deadline) == "MODEL_INVOCATION_LIMIT"
