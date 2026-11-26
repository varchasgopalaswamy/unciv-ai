"""Managed players share game authority while preserving their own native state."""

import asyncio
import copy
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from civ_agent import memory_schema as tables
from civ_agent.game_config import AgentDefaults, EconomyModel, GameConfig, Player
from civ_agent.operator_connection import Operator, OperatorError
from civ_agent.orchestration.contracts import GatewayJob
from civ_agent.orchestration.coordinator import NativeCoordinator
from civ_agent.orchestration.managed_player import (
    ConfigureManagedPlayer,
    NativeModelRole,
    NativePlayerSession,
    PrepareManagedTurn,
)
from civ_agent.orchestration.preparation import prepare_job
from civ_agent.orchestration.store import JournalConflict, WorkflowStore
from tests.domain_fixtures import StartupHost


def session(player="p0", **changes):
    config = GameConfig(
        agents=AgentDefaults(
            model="gpt-managed",
            reasoning_effort="xhigh",
            communication_reasoning_effort="minimal",
            fast=True,
            economy_interval=7,
            military_interval=9,
            review_interval=11,
            review_attempts=3,
            strategy_guide=True,
            civilization_guide=True,
            native_advice_enabled=True,
        )
    )
    player_config = Player(
        name=player,
        economy_model=EconomyModel(provider="openrouter", model="vendor/economy", reasoning_effort="low"),
        military_model=EconomyModel(provider="openrouter", model="vendor/military", reasoning_effort="high"),
    )
    settings = {**config.settings(player_config), **changes}
    return NativePlayerSession.model_validate({"game": "game", "player": player, "settings": settings})


class SharedOperator:
    host_url = "https://host.invalid"

    def __init__(self):
        self.status = {
            "epoch": "g1",
            "handoff": 42,
            "turn": 250,
            "activeCivilization": "Rome",
            "leaseId": "managed-root-lease",
            "phase": "ACTIVE",
            "serverInstance": "host-instance",
        }
        self.calls = []

    def call(self, operation, **fields):
        self.calls.append((operation, copy.deepcopy(fields)))
        if operation in {"activate", "connect"}:
            assert fields["leaseId"] == self.status["leaseId"]
            assert fields["epoch"] == self.status["epoch"]
            if operation == "activate":
                assert (fields["civilization"], fields["handoff"]) == (
                    self.status["activeCivilization"],
                    self.status["handoff"],
                )
            return {"status": copy.deepcopy(self.status), "playerToken": "token-" + fields["civilization"]}
        assert operation == "status", "A player must never acquire, renew or pause the game lease"
        return {"status": copy.deepcopy(self.status)}


@asynccontextmanager
async def coordinator(tmp_path, *, player="p0", civilization="Rome", operator=None):
    shared = operator or SharedOperator()
    store = WorkflowStore(tmp_path / "worker", player=player)
    host = StartupHost(
        SimpleNamespace(
            service=SimpleNamespace(records={"active": {"epoch": "g1", "gameTurn": 250}}, active_id="active")
        )
    )
    host.civilization = civilization
    host.view["cities"][0]["constructionQueue"] = ["Worker"]
    original_tool, advice_calls = host.tool, []

    async def tool(name, args):
        if name == "native_advice":
            advice_calls.append(copy.deepcopy(args))
            return {"available": True, "epoch": args["epoch"], "revision": args["revision"], "recommendations": []}
        return await original_tool(name, args)

    host.tool = tool
    root = NativeCoordinator(
        game="game",
        player=player,
        civilization=civilization,
        state_dir=tmp_path / "coordinator",
        store=store,
        operator=cast(Operator, shared),
        host_ca=tmp_path / "unused.pem",
        connection_factory=lambda *_: host,
        external_lease_id="managed-root-lease",
    )
    try:
        yield SimpleNamespace(root=root, store=store, host=host, operator=shared, advice_calls=advice_calls)
    finally:
        await root.close()
        store.close()


def test_actual_resolved_settings_map_native_role_auth_effort_and_priority_without_loss():
    configured = session()
    assert configured.request_limit == 24
    assert configured.settings.review_attempts == 3
    assert configured.review_interval == 11
    expected: dict[NativeModelRole, tuple[str, str, str, str, bool]] = {
        "gameplay": ("openai", "subscription", "gpt-managed", "xhigh", True),
        "economy": ("openrouter", "api_key", "vendor/economy", "low", False),
        "military": ("openrouter", "api_key", "vendor/military", "high", False),
        "communication": ("openai", "subscription", "gpt-managed", "minimal", True),
    }
    for role, values in expected.items():
        selected = configured.get_selection(role)
        assert (
            selected.provider,
            selected.authentication,
            selected.model,
            selected.reasoning_effort,
            selected.fast,
        ) == values
    assert (
        configured.get_selection("review")
        == configured.get_selection("peace_main")
        == configured.get_selection("gameplay")
    )
    assert configured.get_selection("peace_military") == configured.get_selection("military")
    assert configured.get_request_limit("gameplay") == configured.get_request_limit("communication") == 24
    assert configured.get_request_limit("review") == 12 and configured.get_request_limit("peace_military") == 2
    assert NativePlayerSession.model_validate_json(configured.model_dump_json()) == configured


@pytest.mark.parametrize(
    "changes",
    [
        {"reasoning_effort": "provider-custom"},
        {"model": "scripted-v1"},
        {"economy_model": {"provider": "openai", "model": "gpt-economy"}},
    ],
)
def test_unsupported_or_unresolved_native_settings_fail_before_session_admission(changes):
    with pytest.raises(ValidationError, match="MANAGED_MODEL_SETTINGS_UNSUPPORTED"):
        session(**changes)


def test_two_players_share_one_lease_and_offturn_configuration_grants_no_observations(tmp_path):
    async def run():
        shared = SharedOperator()
        async with coordinator(tmp_path / "rome", operator=shared) as rome:
            async with coordinator(tmp_path / "greece", player="p1", civilization="Greece", operator=shared) as greece:
                for item in (rome, greece):
                    configured = session(item.root.player)
                    await item.root.configure_managed_session(configured)
                    with item.root.memory.transaction() as db:
                        assert item.root.memory._meta(db, "epoch") is None
                    await item.root.configure_managed_player(
                        ConfigureManagedPlayer(session=configured, expected_epoch="g1")
                    )
                    with item.root.memory.transaction() as db:
                        assert item.root.memory._meta(db, "epoch") == "g1"
                        assert not db.execute(tables.jobs.select()).all()
                        assert not db.execute(tables.records.select()).all()
                    assert await item.root.lease() == shared.status
                    assert item.root.game_state["lease"] == "managed-root-lease"
                assert {name for name, _ in shared.calls} == {"status"}
                ticket = await rome.root.prepare_managed_turn(
                    PrepareManagedTurn(session=session(), index=10_000, expected_epoch="g1", expected_handoff=42)
                )
                assert ticket.turn == 250 and ticket.index == 10_000
                assert (
                    await rome.root.prepare_managed_turn(
                        PrepareManagedTurn(session=session(), index=10_000, expected_epoch="g1", expected_handoff=42)
                    )
                    == ticket
                )
                assert not greece.root.game_state["tickets"]
                assert {name for name, _ in shared.calls} == {"status", "activate"}
                shared.status["handoff"] = 43
                with pytest.raises(JournalConflict, match="NATIVE_TURN_REQUEST_CHANGED"):
                    await rome.root.prepare_managed_turn(
                        PrepareManagedTurn(session=session(), index=10_000, expected_epoch="g1", expected_handoff=43)
                    )

    asyncio.run(run())


def test_pilot_request_limits_retain_bounded_review_and_peace_allowances(tmp_path):
    async def run():
        store = WorkflowStore(tmp_path / "worker", player="p0")
        root = NativeCoordinator(
            game="game",
            player="p0",
            civilization="Rome",
            state_dir=tmp_path / "pilot",
            store=store,
            operator=cast(Operator, SimpleNamespace()),
            host_ca=tmp_path / "unused.pem",
        )
        try:
            root.game_state["request"] = {"request_limit": 24}
            assert root.get_request_limit("gameplay") == root.get_request_limit("communication") == 24
            assert root.get_request_limit("review") == 12
            assert all(root.get_request_limit(role) == 2 for role in ("peace", "peace_main", "peace_military"))
        finally:
            await root.close()
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize("lease", [None, "managed-root-lease", "another-root"])
def test_managed_heartbeat_only_observes_and_operation_lease_remains_strict(tmp_path, monkeypatch, lease):
    async def run():
        async with coordinator(tmp_path) as item:
            item.operator.status["leaseId"] = lease
            sleep = AsyncMock(side_effect=asyncio.CancelledError())
            monkeypatch.setattr(asyncio, "sleep", sleep)
            with pytest.raises(JournalConflict if lease == "another-root" else asyncio.CancelledError):
                await item.root.heartbeat()
            assert item.operator.calls == [("status", {})]
            if lease == "managed-root-lease":
                await item.root.lease()
            else:
                with pytest.raises(JournalConflict, match="NATIVE_LEASE_CONFLICT"):
                    await item.root.lease()
            for operation in ("acquire", "heartbeat", "pause"):
                with pytest.raises(JournalConflict, match="NATIVE_EXTERNAL_LEASE_OWNER_REQUIRED"):
                    await item.root.op(operation, leaseId="managed-root-lease")
            assert {name for name, _ in item.operator.calls} == {"status"}

    asyncio.run(run())


def test_managed_observer_survives_long_host_outage_and_reconnects_without_renewal(tmp_path, monkeypatch, capsys):
    async def run():
        async with coordinator(tmp_path) as item:
            original, attempts = item.root.op, []

            async def observe(operation, **fields):
                attempts.append((operation, fields))
                if len(attempts) <= 20:
                    raise OperatorError("synthetic host downtime", "TRANSPORT_UNAVAILABLE")
                return await original(operation, **fields)

            async def sleep(seconds):
                if seconds == 5:
                    raise asyncio.CancelledError()
                assert seconds == 2

            monkeypatch.setattr(item.root, "op", observe)
            monkeypatch.setattr(asyncio, "sleep", sleep)
            with pytest.raises(asyncio.CancelledError):
                await item.root.heartbeat()
            assert attempts == [("status", {})] * 21
            assert item.operator.calls == [("status", {})]

    asyncio.run(run())
    assert capsys.readouterr().out.splitlines() == ["HOST-RECONNECTING", "HOST-RECONNECTED"]


@pytest.mark.parametrize("fields", [{"status": 401}, {"code": "LEASE_LOST"}])
def test_managed_observer_still_stops_on_authentication_or_native_fence(tmp_path, monkeypatch, fields):
    async def run():
        async with coordinator(tmp_path) as item:
            error = OperatorError("synthetic rejected observer", **fields)
            observe = AsyncMock(side_effect=error)
            monkeypatch.setattr(item.root, "op", observe)
            with pytest.raises(OperatorError) as caught:
                await item.root.heartbeat()
            assert caught.value is error
            observe.assert_awaited_once_with("status")

    asyncio.run(run())


@pytest.mark.parametrize("damage", ["owner", "epoch", "handoff", "civilization"])
def test_managed_turn_rejects_changed_scope_or_position_before_activation(tmp_path, damage):
    async def run():
        async with coordinator(tmp_path) as item:
            configured = session("other" if damage == "owner" else "p0")
            request = PrepareManagedTurn(
                session=configured,
                index=0,
                expected_epoch="wrong" if damage == "epoch" else "g1",
                expected_handoff=43 if damage == "handoff" else 42,
            )
            if damage == "civilization":
                item.operator.status["activeCivilization"] = "Greece"
            with pytest.raises(JournalConflict):
                await item.root.prepare_managed_turn(request)
            assert not item.root.game_state["tickets"]
            assert "activate" not in {name for name, _ in item.operator.calls}

    asyncio.run(run())


def test_session_settings_survive_reopen_are_detached_and_cannot_change_with_same_lease(tmp_path):
    async def run():
        configured = session()
        async with coordinator(tmp_path) as item:
            await item.root.configure_managed_session(configured, expected_epoch="g1")
            copy_of_settings = item.root.settings
            copy_of_settings["model"] = "unrelated"
            detached = item.root.managed_session
            detached.settings.economy_model.model = "changed-external-copy"
            assert item.root.settings == configured.settings.model_dump(mode="json")
        async with coordinator(tmp_path) as reopened:
            assert reopened.root.managed_session == configured
            assert (
                reopened.root.economy_interval,
                reopened.root.military_interval,
                reopened.root.review_interval,
                reopened.root.review_attempts,
            ) == (7, 9, 11, 3)
            assert reopened.root.gateway.native_advice_enabled
            with pytest.raises(JournalConflict, match="MANAGED_SESSION_CHANGED"):
                await reopened.root.configure_managed_session(session(review_interval=12))
            for lease in (None, "different-root"):
                with pytest.raises(JournalConflict, match="NATIVE_LEASE_BINDING_CONFLICT"):
                    NativeCoordinator(
                        game="game",
                        player="p0",
                        civilization="Rome",
                        state_dir=tmp_path / "coordinator",
                        store=reopened.store,
                        operator=cast(Operator, reopened.operator),
                        host_ca=tmp_path / "unused.pem",
                        external_lease_id=lease,
                    )

    asyncio.run(run())


def test_interrupted_managed_startup_retains_frozen_role_settings_and_rejects_tampering(tmp_path):
    async def run():
        async with coordinator(tmp_path) as item:
            await item.root.configure_managed_session(session(), expected_epoch="g1")
            from civ_agent.orchestration.contracts import Scope

            binding = GatewayJob(
                scope=Scope(game="game", player="p0", epoch="g1", activation="communication"),
                job="communication",
                purpose="communication",
            )
            original, _ = await prepare_job(item.root, binding, item.host)
            item.root.memory.call(binding.job, "startup:0:chat", "chat_read", {}, {})
            recovered, prefix = await prepare_job(item.root, binding, item.host)
            assert prefix == "startup:1:" and recovered.memory == original.memory
            with item.root.memory.transaction() as db:
                row = db.execute(tables.jobs.select().where(tables.jobs.c.id == binding.job)).mappings().one()
                assert json.loads(row["settings"]) == item.root.job_settings("communication")
                db.execute(tables.jobs.update().where(tables.jobs.c.id == binding.job).values(settings="{}"))
            with pytest.raises(JournalConflict, match="PREPARATION_ALREADY_ADMITTED"):
                await prepare_job(item.root, binding, item.host)

    asyncio.run(run())
