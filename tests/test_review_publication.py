"""Frozen review cycles publish atomically without owning native turn activation."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart

from civ_agent.game_config import GameConfig
from civ_agent.orchestration.contracts import ReviewResult, Scope, digest
from civ_agent.orchestration.managed_player import NativePlayerSession, NativePlayerSettings
from civ_agent.orchestration.review import validate_report
from civ_agent.orchestration.review_publication import NativeReviews
from civ_agent.orchestration.store import JournalConflict
from civ_agent.protocol import JsonObject
from civ_agent.review_capture import ReviewCapture
from civ_agent.runtime.review_cycles import ReviewCycle
from civ_agent.strategic_review import StrategicAdvice
from tests.step_history import save_history
from tests.test_local_review_services import frozen_inputs
from tests.test_native_handoff import Ledger, setup
from tests.test_strategic_review import assessment


def managed_settings(root, **settings):
    config = GameConfig.model_validate({"agents": {"model": "configured-model"}}).resolved()
    session = NativePlayerSession(
        game=root.game,
        player=root.player,
        settings=NativePlayerSettings.model_validate({**config.settings(config.agent_players[0]), **settings}),
        request_limit=6,
    )
    root.game_state["managedSession"] = session.model_dump(mode="json")
    root._apply_session_settings()
    return session


def source_inputs(epoch, source_turn, target_turn):
    inputs: JsonObject = frozen_inputs()
    inputs.update(
        turn=source_turn,
        reviewTiming={
            "kind": "opening" if target_turn == 0 else "periodic",
            "sourceTurn": source_turn,
            "targetTurn": target_turn,
        },
    )
    inputs["observation"].update(turn=source_turn, epoch=epoch)
    inputs["reads"]["player_turn"] = dict(inputs["observation"])
    inputs["reads"]["victory_progress"]["turn"] = source_turn
    inputs["reads"]["chat_read"]["turn"] = source_turn
    return inputs


def assert_no_activation(root):
    assert isinstance(root.activate, AsyncMock) and isinstance(root.op, AsyncMock)
    root.activate.assert_not_awaited()
    assert all(call.args[0] == "status" for call in root.op.await_args_list)


def test_raw_import_marks_only_its_missing_predecessor_unavailable(tmp_path):
    async def run():
        ledger = Ledger()
        root, artifacts, _, _, _, _ = await setup(tmp_path, ledger)
        managed_settings(root, review_interval=15)
        ledger.status.update(turn=16)
        root.op = AsyncMock(return_value={"status": ledger.status})
        root.activate = AsyncMock()
        connection = AsyncMock()
        reviews = NativeReviews(root, SimpleNamespace(connection=connection))
        root.reviews = reviews
        try:
            with pytest.raises(JournalConflict, match="REVIEW_CYCLE_MISSING"):
                await reviews.require_target(16)
            await reviews.initialize_import(16)
            cycle = await reviews.for_target(16)
            assert cycle is not None and cycle.status == "UNAVAILABLE" and cycle.inputs is None
            assert cycle.source_turn == 15 and cycle.request is None
            await reviews.require_target(16)
            ledger.status.update(turn=17)
            await reviews.initialize_import(16)
            assert await reviews.for_target(16) == cycle
            for turn in (0, 31):
                with pytest.raises(JournalConflict, match="REVIEW_CYCLE_MISSING"):
                    await reviews.require_target(turn)
            with pytest.raises(JournalConflict, match="REVIEW_IMPORT_ORIGIN_CONFLICT"):
                await reviews.initialize_import(17)
            connection.assert_not_awaited()
            assert_no_activation(root)
        finally:
            await root.close()
            artifacts.close()

    asyncio.run(run())


@pytest.mark.parametrize("review_timeout", [600, 36000])
def test_capture_and_begin_freeze_managed_settings_without_owning_native_transition(
    tmp_path, monkeypatch, review_timeout
):
    async def run():
        ledger = Ledger()
        root, artifacts, _, turn, _, _ = await setup(tmp_path, ledger)
        session = managed_settings(
            root,
            review_interval=250,
            review_attempts=3,
            review_timeout_seconds=review_timeout,
            strategy_guide=True,
            civilization_guide=True,
        )
        root.activate = AsyncMock()
        root.op = AsyncMock(return_value={"status": ledger.status})
        connection = SimpleNamespace(
            tool=AsyncMock(return_value={"epoch": turn.scope.epoch, "turn": 0}), list_tools=AsyncMock(return_value=[])
        )
        capture = AsyncMock(return_value=source_inputs(turn.scope.epoch, 0, 0))
        monkeypatch.setattr(ReviewCapture, "capture", capture)
        reviews = NativeReviews(root, SimpleNamespace(connection=AsyncMock(return_value=connection)))
        root.reviews = reviews
        try:
            cycle = await reviews.capture(0, 0, opening=True)
            assert cycle is not None and cycle.status == "PENDING"
            request = await reviews.begin(cycle, "generation")
            assert request is not None
            assert request.attempt_limit == 3 and request.request_limit == 6
            assert request.timeout_seconds == review_timeout
            assert request.strategy_guide and request.civilization_guide
            assert request.selection == session.get_selection("review")
            assert (request.source_turn, request.target_turn, request.review_cycle_id) == (0, 0, cycle.id)
            assert await reviews.capture(0, 0, opening=True) == await reviews.for_target(0)
            assert await reviews.begin(cycle, "generation") == request
            row = reviews.store.pending()
            assert row is not None and row["timeout"] == review_timeout
            capture.assert_awaited_once()
            with pytest.raises(JournalConflict, match="REVIEW_BARRIER_REQUIRED"):
                await reviews.require_target(0)
            assert_no_activation(root)
        finally:
            await root.close()
            artifacts.close()

    asyncio.run(run())


def test_end_turn_capture_uses_source_10_and_gates_target_11(tmp_path, monkeypatch):
    async def run():
        ledger = Ledger()
        root, artifacts, _, turn, _, _ = await setup(tmp_path, ledger)
        managed_settings(root)
        ledger.status.update(turn=10, handoff=41)
        root.op = AsyncMock(return_value={"status": ledger.status})
        root.activate = AsyncMock()
        connection = SimpleNamespace(
            tool=AsyncMock(return_value={"epoch": turn.scope.epoch, "turn": 10}), list_tools=AsyncMock(return_value=[])
        )
        capture = AsyncMock(return_value=source_inputs(turn.scope.epoch, 10, 11))
        monkeypatch.setattr(ReviewCapture, "capture", capture)
        reviews = NativeReviews(root, SimpleNamespace(connection=AsyncMock(return_value=connection)))
        root.reviews = reviews
        try:
            cycle = await reviews.capture(10, 11, handoff=41)
            assert cycle is not None and cycle.kind == "periodic"
            assert (cycle.source_turn, cycle.target_turn) == (10, 11)
            assert capture.await_args is not None
            assert capture.await_args.args[3:] == (10, turn.scope.epoch)
            assert cycle.inputs is not None
            inputs = artifacts.get(cycle.scope, cycle.inputs)
            assert inputs["turn"] == inputs["observation"]["turn"] == 10
            assert inputs["reviewTiming"] == {"kind": "periodic", "sourceTurn": 10, "targetTurn": 11}
            assert not cycle.committed and cycle.source_handoff == 41
            with pytest.raises(JournalConflict, match="REVIEW_HANDOFF_REQUIRED"):
                await reviews.begin(cycle, "generation")
            cycle = await reviews.commit(cycle)
            request = await reviews.begin(cycle, "generation")
            assert request is not None and (request.source_turn, request.target_turn) == (10, 11)
            with pytest.raises(JournalConflict, match="REVIEW_BARRIER_REQUIRED"):
                await reviews.require_target(11)
            assert_no_activation(root)
        finally:
            await root.close()
            artifacts.close()

    asyncio.run(run())


async def publication(tmp_path, *, invalid=False, unavailable=False, source_turn=0, target_turn=0):
    ledger = Ledger()
    root, artifacts, _, turn, _, _ = await setup(tmp_path, ledger)
    managed_settings(root)
    status = ledger.status
    status.update(turn=source_turn)
    root.activate = AsyncMock()
    root.op = AsyncMock(return_value={"status": status})
    reviews = NativeReviews(root, None)
    root.reviews = reviews
    inputs = source_inputs(turn.scope.epoch, source_turn, target_turn)
    scope = Scope(game=root.game, player=root.player, epoch=turn.scope.epoch, activation=digest(["review", inputs]))
    cycle = ReviewCycle(
        scope=scope,
        source_turn=source_turn,
        target_turn=target_turn,
        kind="opening" if target_turn == 0 else "periodic",
        inputs=artifacts.put(scope, inputs),
    )
    await root.io.call(reviews.store.prepare, cycle.id, inputs, {}, 30)
    await root.put(reviews.key(target_turn), cycle.model_dump(mode="json"))
    request = await reviews.begin(cycle, "generation")
    assert request is not None
    raw = assessment()
    validated = validate_report(request, StrategicAdvice.model_validate(raw), [], worker=reviews.validation)
    if invalid:
        raw["review"]["materialAssumptions"][0].update(status="inferred", sources=["invented-source"])
    messages: list[ModelMessage] = (
        [] if unavailable else [ModelResponse(parts=[ToolCallPart("submit_report", raw, "report1")])]
    )
    result = ReviewResult(
        scope=request.scope,
        status="UNAVAILABLE" if unavailable else "COMPLETED",
        report=None if unavailable else artifacts.put(request.scope, validated),
        history=artifacts.put(
            request.scope, (await save_history(artifacts, request.scope, messages)).model_dump(mode="json")
        ),
        usage={"requests": 1},
    )
    artifacts.reserve(request.scope, "report", {"test": "publication"})
    artifacts.complete(request.scope, "report", {"test": "publication"}, result.model_dump(mode="json"))
    return root, artifacts, reviews, cycle, request, result, status


def test_report_and_cycle_commit_atomically_then_replay_without_new_model_work(tmp_path, monkeypatch):
    async def run():
        root, artifacts, reviews, cycle, request, result, _ = await publication(tmp_path)
        try:
            save = root.memory._set

            def fail(db, key, value):
                if key == reviews.key(cycle.target_turn) and value.get("status") == "READY":
                    raise OSError("killed before cycle commit")
                return save(db, key, value)

            monkeypatch.setattr(root.memory, "_set", fail)
            with pytest.raises(OSError):
                await reviews.publish(cycle, request, result)
            row = reviews.store.at_turn(0)
            assert row is not None and row["status"] == "PENDING"
            with pytest.raises(JournalConflict, match="REVIEW_BARRIER_REQUIRED"):
                await reviews.require_target(0)
            assert await root.get("reviewPosition") is None
            monkeypatch.setattr(root.memory, "_set", save)
            await reviews.publish(cycle, request, result)
            await reviews.publish(cycle, request, result)
            await reviews.require_target(0)
            row = reviews.store.at_turn(0)
            assert row is not None and row["status"] == "COMPLETED"
            assert (await reviews.for_target(0)).status == "READY"
            assert_no_activation(root)
        finally:
            await root.close()
            artifacts.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "damage", ["source", "receipt", "player", "epoch", "position", "request_cycle", "request_turn", "lease_epoch"]
)
def test_invalid_or_stale_publication_never_opens_player_admission(tmp_path, damage):
    async def run():
        root, artifacts, reviews, cycle, request, result, status = await publication(
            tmp_path,
            invalid=damage == "source",
            unavailable=damage in {"position", "request_cycle", "request_turn"},
        )
        try:
            if damage == "position":
                inputs = artifacts.get(request.scope, request.inputs)
                inputs["observation"]["turn"] = inputs["reads"]["player_turn"]["turn"] = 99
                request = request.model_copy(update={"inputs": artifacts.put(request.scope, inputs)})
            elif damage == "receipt":
                result = result.model_copy(update={"usage": {"requests": 99}})
            elif damage == "player":
                cycle = cycle.model_copy(update={"scope": cycle.scope.model_copy(update={"player": "p1"})})
            elif damage == "epoch":
                with root.memory.transaction() as db:
                    root.memory._set(db, "epoch", "e2")
            elif damage == "request_cycle":
                request = request.model_copy(update={"review_cycle_id": "b" * 64})
            elif damage == "request_turn":
                request = request.model_copy(update={"target_turn": 1})
            elif damage == "lease_epoch":
                status["epoch"] = "e2"
            if damage in {"position", "request_cycle", "request_turn"}:
                current = await reviews.for_target(0)
                assert current is not None
                current.request = request
                await root.put(reviews.key(0), current.model_dump(mode="json"))
            with pytest.raises((JournalConflict, ValueError)):
                await reviews.publish(cycle, request, result)
            row = reviews.store.at_turn(0)
            assert row is not None and row["status"] == "PENDING"
            assert_no_activation(root)
        finally:
            await root.close()
            artifacts.close()

    asyncio.run(run())


def test_completed_off_turn_report_publishes_after_handoff_without_reactivating_player(tmp_path):
    async def run():
        root, artifacts, reviews, cycle, request, result, status = await publication(
            tmp_path, source_turn=10, target_turn=11
        )
        try:
            status.update(handoff=42, turn=11, activeCivilization="Greece")
            await reviews.publish(cycle, request, result)
            await reviews.require_target(11)
            assert (await reviews.for_target(11)).status == "READY"
            assert_no_activation(root)
        finally:
            await root.close()
            artifacts.close()

    asyncio.run(run())


def test_unavailable_report_finishes_cycle_without_changing_native_admission(tmp_path):
    async def run():
        root, artifacts, reviews, cycle, request, result, _ = await publication(tmp_path, unavailable=True)
        try:
            await reviews.publish(cycle, request, result)
            await reviews.require_target(0)
            row = reviews.store.at_turn(0)
            assert row is not None and row["status"] == "UNAVAILABLE"
            assert (await reviews.for_target(0)).status == "UNAVAILABLE"
            assert_no_activation(root)
        finally:
            await root.close()
            artifacts.close()

    asyncio.run(run())


def test_upgrade_reuses_committed_opening_report_at_later_world_position_without_new_model(tmp_path):
    async def run():
        root, artifacts, reviews, cycle, request, result, status = await publication(tmp_path)
        try:
            await reviews.publish(cycle, request, result)
            await root.put(reviews.key(0), None)
            status.update(turn=2, handoff=11, activeCivilization="France")
            recovered = await reviews.capture(0, 0, opening=True)
            assert recovered is not None and recovered.status == "READY" and recovered.inputs is None
            assert await reviews.begin(recovered, "new-generation") is None
            await reviews.require_target(0)
            assert_no_activation(root)
        finally:
            await root.close()
            artifacts.close()

    asyncio.run(run())


@pytest.mark.parametrize("advanced", [False, True])
def test_unstarted_capture_recovery_uses_native_handoff_to_commit_or_discard(tmp_path, monkeypatch, advanced):
    async def run():
        ledger = Ledger()
        root, artifacts, _, turn, _, _ = await setup(tmp_path, ledger)
        managed_settings(root)
        ledger.status.update(turn=10, handoff=41)
        root.op = AsyncMock(return_value={"status": ledger.status})
        root.activate = AsyncMock()
        connection = SimpleNamespace(
            tool=AsyncMock(return_value={"epoch": turn.scope.epoch, "turn": 10}),
            list_tools=AsyncMock(return_value=[]),
        )
        captured = AsyncMock(return_value=source_inputs(turn.scope.epoch, 10, 11))
        monkeypatch.setattr(ReviewCapture, "capture", captured)
        reviews = NativeReviews(root, SimpleNamespace(connection=AsyncMock(return_value=connection)))
        root.reviews = reviews
        try:
            cycle = await reviews.capture(10, 11, handoff=41)
            assert cycle is not None and not cycle.committed
            with pytest.raises(JournalConflict, match="REVIEW_HANDOFF_REQUIRED"):
                await reviews.begin(cycle, "generation")
            ledger.status["handoff"] = 42 if advanced else 41
            pending = await reviews.pending(ledger.status["handoff"])
            if advanced:
                assert len(pending) == 1 and pending[0].id == cycle.id and pending[0].committed
                assert await reviews.begin(pending[0], "generation") is not None
                assert reviews.store.at_turn(10) is not None
            else:
                assert pending == [] and await reviews.for_target(11) is None
                assert reviews.store.at_turn(10) is None
                # A rejected handoff can produce a new current observation.
                fresh = source_inputs(turn.scope.epoch, 10, 11)
                fresh["reads"]["research_details"]["technologies"][0]["cost"] = 33
                captured.return_value = fresh
                replacement = await reviews.capture(10, 11, handoff=41)
                assert replacement is not None and replacement.id != cycle.id
            assert_no_activation(root)
        finally:
            await root.close()
            artifacts.close()

    asyncio.run(run())


def test_capture_pending_row_and_cycle_metadata_commit_atomically(tmp_path, monkeypatch):
    async def run():
        ledger = Ledger()
        root, artifacts, _, turn, _, _ = await setup(tmp_path, ledger)
        managed_settings(root)
        ledger.status.update(turn=10, handoff=41)
        root.op = AsyncMock(return_value={"status": ledger.status})
        connection = SimpleNamespace(
            tool=AsyncMock(return_value={"epoch": turn.scope.epoch, "turn": 10}), list_tools=AsyncMock(return_value=[])
        )
        monkeypatch.setattr(ReviewCapture, "capture", AsyncMock(return_value=source_inputs(turn.scope.epoch, 10, 11)))
        reviews = NativeReviews(root, SimpleNamespace(connection=AsyncMock(return_value=connection)))
        root.reviews = reviews
        save = root.memory._set

        def interrupted(db, key, value):
            if key == reviews.key(11):
                raise OSError("Interrupted cycle publication")
            return save(db, key, value)

        try:
            monkeypatch.setattr(root.memory, "_set", interrupted)
            with pytest.raises(OSError, match="Interrupted cycle publication"):
                await reviews.capture(10, 11, handoff=41)
            assert reviews.store.at_turn(10) is None
            assert await reviews.for_target(11) is None
            monkeypatch.setattr(root.memory, "_set", save)
            cycle = await reviews.capture(10, 11, handoff=41)
            assert cycle is not None and not cycle.committed
            assert (await reviews.for_target(11)).id == cycle.id
            row = reviews.store.at_turn(10)
            assert row is not None and row["job"] == cycle.id
        finally:
            await root.close()
            artifacts.close()

    asyncio.run(run())


def test_zero_model_step_wait_exhaustion_is_terminal_without_fabricated_history(tmp_path):
    async def run():
        ledger = Ledger()
        root, artifacts, _, turn, _, _ = await setup(tmp_path, ledger)
        managed_settings(root)
        root.op = AsyncMock(return_value={"status": ledger.status})
        root.activate = AsyncMock()
        reviews = NativeReviews(root, None)
        root.reviews = reviews
        inputs = source_inputs(turn.scope.epoch, 0, 0)
        scope = turn.scope.model_copy(update={"activation": digest(["zero-step", inputs])})
        cycle = ReviewCycle(
            scope=scope, source_turn=0, target_turn=0, kind="opening", inputs=artifacts.put(scope, inputs)
        )
        await root.io.call(reviews.store.prepare, cycle.id, inputs, {}, 30)
        await root.put(reviews.key(0), cycle.model_dump(mode="json"))
        request = await reviews.begin(cycle, "generation")
        assert request is not None
        try:
            await reviews.unavailable_before_model(cycle, request)
            await reviews.unavailable_before_model(cycle, request)
            await reviews.require_target(0)
            assert (await reviews.for_target(0)).status == "UNAVAILABLE"
            row = reviews.store.at_turn(0)
            assert row is not None and row["status"] == "UNAVAILABLE"
            assert artifacts.receipt(request.scope, "report") is None
            assert not (artifacts.root / "agent-steps.sqlite3").exists()
            assert_no_activation(root)
        finally:
            await root.close()
            artifacts.close()

    asyncio.run(run())
