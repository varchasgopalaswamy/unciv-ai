"""Operator projections use only validated native controller publications."""

import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from civ_agent.commands import game
from civ_agent.container_support import ContainerError
from civ_agent.game_config import GameConfig
from civ_agent.monitoring import MonitorStore, path_for, records
from civ_agent.orchestration.managed_game_contracts import (
    ManagedGameRequest,
    ManagedGameResult,
    ManagedGameStatus,
    ManagedSeat,
)
from civ_agent.orchestration.managed_host import GameAuthority, ManagedLimits
from civ_agent.orchestration.managed_player import NativePlayerSession, NativePlayerSettings
from civ_agent.orchestration.native_status import native_projection
from civ_agent.protocol import JsonObject
from civ_agent.tls import write_private_json


def native_files(root: Path, *, identifier="game", epoch="epoch", turn=33):
    config = GameConfig().resolved()
    player = config.agent_players[0]
    request = ManagedGameRequest(
        authority=GameAuthority(game=identifier, epoch=epoch, lease_id="lease", limits=config.limits),
        seats=[
            ManagedSeat(
                civilization=player.civilization,
                session=NativePlayerSession(
                    game=identifier,
                    player="p0",
                    settings=NativePlayerSettings.model_validate(config.settings(player)),
                ),
            )
        ],
    )
    started = datetime(2026, 11, 5, tzinfo=UTC)
    common = {
        "game": identifier,
        "epoch": epoch,
        "initial_started_at": started,
        "deadline": started + timedelta(seconds=config.limits.max_seconds),
        "initial_turn": 1,
        "turn": turn,
        "model_invocations": 4,
        "rollovers": 0,
    }
    status = ManagedGameStatus(
        **common,
        phase="ACTIVE",
        mode="RUNNING",
        reason=None,
        quiesced=False,
        handoff=5,
        active_civilization=player.civilization,
        active_controller="AGENT",
        active_turn_id=None,
    )
    result = ManagedGameResult(**common, handoffs=6)
    directory = root / "native-runtime"
    for name, value in (("game", request), ("status", status), ("result", result)):
        write_private_json(directory / f"{name}.json", value.model_dump(mode="json"))
    return request, status, result


def host_publication(
    root: Path, *, epoch="epoch", match_id="engine-game-uuid", updated_ms=1234500, finished=True
) -> JsonObject:
    """Full AccountingMonitoring schema, including native per-purpose accounting."""

    def usage(requests):
        return {
            "input_tokens": requests * 10,
            "cached_input_tokens": requests * 3,
            "output_tokens": requests * 7,
            "reasoning_output_tokens": requests * 2,
            "total_tokens": requests * 17,
            "complete": True,
        }

    calls = {"calls": 2, "succeeded": 1, "errors": 0, "replayed": 1, "incomplete": 0, "elapsedMs": 42}
    edits = {
        "batches": 1,
        "accepted": 2,
        "rejected": 0,
        "rejectedBatches": 0,
        "rejectionReasons": {},
        "complete": True,
    }
    scores = [
        {
            "civilization": civilization,
            "controller": controller,
            "defeated": False,
            "score": 42,
            "rawScore": 42.5,
            "breakdown": {"Cities": 20.0, "Population": 22.5},
        }
        for civilization, controller in (("Rome", "AGENT"), ("Greece", "HUMAN"), ("Egypt", "BUILT_IN_AI"))
    ]
    value: JsonObject = {
        "schemaVersion": 1,
        "matchId": match_id,
        "epoch": epoch,
        "status": "completed" if finished else "incomplete",
        "result": {
            "epoch": epoch,
            "recordedAtMs": 1793835400000,
            "winner": "Rome",
            "victoryType": "Scientific",
            "victoryTurn": 330,
            "scores": scores,
        }
        if finished
        else None,
        "scores": scores,
        "startedAtMs": 1793835300000,
        "elapsedMs": 100000,
        "hostRevision": "host-revision",
        "engineRevision": "engine-revision",
        "seed": -42,
        "ruleset": "Civ V - Gods & Kings",
        "speed": "Standard",
        "difficulty": "Prince",
        "victoryTypes": ["Scientific", "Domination"],
        "maxTurns": 330,
        "usage": usage(64),
        "modelJobs": 14,
        "modelRequests": 64,
        "agentCalls": {"native_advice": calls},
        "memoryEdits": edits,
        "hostCalls": {"command:END_TURN": calls},
        "players": [
            {
                "civilization": score["civilization"],
                "usage": usage(64 if index == 0 else 0),
                "modelJobs": 14 if index == 0 else 0,
                "modelRequests": 64 if index == 0 else 0,
                "gameplayUsage": usage(32 if index == 0 else 0),
                "economyUsage": usage(8 if index == 0 else 0),
                "militaryUsage": usage(8 if index == 0 else 0),
                "consultationUsage": usage(0),
                "reviewUsage": usage(8 if index == 0 else 0),
                "chatUsage": usage(8 if index == 0 else 0),
                "agentCalls": {"native_advice": calls} if index == 0 else {},
                "memoryEdits": edits if index == 0 else None,
                "hostCalls": {"command:END_TURN": calls} if index == 0 else {},
                "activeMs": 20000 if index == 0 else 0,
                "jobElapsedMs": 30000 if index == 0 else 0,
            }
            for index, score in enumerate(scores)
        ],
        "hostTimings": {"accounting.commit": {"calls": 20, "elapsedMs": 123, "maxMs": 20}},
    }
    publish_host(root, value, updated_ms=updated_ms)
    return value


def publish_host(root, value, *, updated_ms=1234500, row_epoch=None):
    store = MonitorStore(path_for(root))
    try:
        store.put("state", "result", value, epoch=row_epoch or value["epoch"])
        with store.engine.begin() as db:
            db.execute(
                records.update()
                .where(records.c.collection == "state", records.c.key == "result")
                .values(updated_ms=updated_ms, epoch=row_epoch or value["epoch"])
            )
    finally:
        store.engine.dispose()


def test_missing_native_publications_are_pending_without_creating_files(tmp_path):
    projection = native_projection(tmp_path)
    assert projection.workflow_status is None and projection.workflow_result is None
    assert projection.result is None and projection.result_updated_at is None
    assert not list(tmp_path.iterdir())


def test_frozen_game_without_first_status_or_result_is_pending(tmp_path):
    native_files(tmp_path)
    (tmp_path / "native-runtime/status.json").unlink()
    (tmp_path / "native-runtime/result.json").unlink()
    assert native_projection(tmp_path).workflow_status is None
    assert native_projection(tmp_path).result is None


def test_native_result_may_be_newer_than_the_last_live_status(tmp_path):
    _, status, result = native_files(tmp_path)
    updated = result.model_copy(update={"turn": 35, "model_invocations": 8})
    path = tmp_path / "native-runtime/result.json"
    write_private_json(path, updated.model_dump(mode="json"))
    os.utime(path, (1234.5, 1234.5))
    projection = native_projection(tmp_path)
    assert projection.workflow_status == status and projection.workflow_result == updated
    assert projection.workflow_result_updated_at == 1234.5
    assert projection.result is None


@pytest.mark.parametrize("filename", ["game", "status", "result"])
@pytest.mark.parametrize("raw", ["{", '{"secret":"never include input in errors"}'])
def test_malformed_native_publications_fail_visibly_without_echoing_values(tmp_path, filename, raw):
    native_files(tmp_path)
    path = tmp_path / f"native-runtime/{filename}.json"
    path.write_text(raw)
    with pytest.raises(ContainerError, match="Invalid native runtime projection") as caught:
        native_projection(tmp_path)
    assert str(path) in str(caught.value) and "never include" not in str(caught.value)


@pytest.mark.parametrize("filename", ["status", "result"])
@pytest.mark.parametrize("field", ["game", "epoch"])
def test_native_publications_must_match_frozen_authority(tmp_path, filename, field):
    native_files(tmp_path)
    path = tmp_path / f"native-runtime/{filename}.json"
    value = json.loads(path.read_text())
    value[field] = "foreign"
    write_private_json(path, value)
    with pytest.raises(ContainerError, match="identity conflicts"):
        native_projection(tmp_path)


def test_native_deadline_must_match_frozen_limits(tmp_path):
    _, status, _ = native_files(tmp_path)
    value = status.model_copy(update={"deadline": status.deadline + timedelta(seconds=1)})
    write_private_json(tmp_path / "native-runtime/status.json", value.model_dump(mode="json"))
    with pytest.raises(ContainerError, match="deadline conflicts"):
        native_projection(tmp_path)


def test_native_status_and_result_share_the_initial_position(tmp_path):
    _, _, result = native_files(tmp_path)
    value = result.model_copy(update={"initial_turn": 2})
    write_private_json(tmp_path / "native-runtime/result.json", value.model_dump(mode="json"))
    with pytest.raises(ContainerError, match="initial position conflicts"):
        native_projection(tmp_path)


def test_newer_workflow_result_can_have_explicit_increased_limits(tmp_path):
    request, status, result = native_files(tmp_path)
    limits = ManagedLimits.model_validate(
        {**request.authority.limits.model_dump(), "max_seconds": request.authority.limits.max_seconds + 100}
    )
    result = result.model_copy(
        update={
            "effective_limits": limits,
            "deadline": result.initial_started_at + timedelta(seconds=limits.max_seconds),
        }
    )
    write_private_json(tmp_path / "native-runtime/result.json", result.model_dump(mode="json"))
    projection = native_projection(tmp_path)
    assert projection.workflow_status == status and projection.workflow_result == result
    assert projection.workflow_result is not None and projection.workflow_status is not None
    assert projection.workflow_result.deadline > projection.workflow_status.deadline


@pytest.mark.parametrize("change", [{"max_seconds": 1}, {"max_concurrent_jobs": 6}])
def test_native_workflow_limits_cannot_decrease_or_change_frozen_fields(tmp_path, change):
    request, status, _ = native_files(tmp_path)
    limits = ManagedLimits.model_validate({**request.authority.limits.model_dump(), **change})
    status = status.model_copy(
        update={
            "effective_limits": limits,
            "deadline": status.initial_started_at + timedelta(seconds=limits.max_seconds),
        }
    )
    write_private_json(tmp_path / "native-runtime/status.json", status.model_dump(mode="json"))
    with pytest.raises(ContainerError, match="limits conflict"):
        native_projection(tmp_path)


def test_workflow_result_cannot_undo_an_observed_limit_increase(tmp_path):
    request, status, _ = native_files(tmp_path)
    limits = ManagedLimits.model_validate(
        {**request.authority.limits.model_dump(), "max_seconds": request.authority.limits.max_seconds + 100}
    )
    status = status.model_copy(
        update={
            "effective_limits": limits,
            "deadline": status.initial_started_at + timedelta(seconds=limits.max_seconds),
        }
    )
    write_private_json(tmp_path / "native-runtime/status.json", status.model_dump(mode="json"))
    with pytest.raises(ContainerError, match="limits conflict"):
        native_projection(tmp_path)


def test_atomic_replacement_keeps_result_content_and_its_own_timestamp_together(tmp_path, monkeypatch):
    _, _, original = native_files(tmp_path)
    path = tmp_path / "native-runtime/result.json"
    os.utime(path, (1234.5, 1234.5))
    replacement = original.model_copy(update={"turn": 34})
    original_validate = ManagedGameResult.model_validate_json

    def validate(raw, *args, **kwargs):
        write_private_json(path, replacement.model_dump(mode="json"))
        os.utime(path, (2345.75, 2345.75))
        return original_validate(raw, *args, **kwargs)

    monkeypatch.setattr(ManagedGameResult, "model_validate_json", validate)
    observed = native_projection(tmp_path)
    assert observed.workflow_result == original and observed.workflow_result_updated_at == 1234.5
    assert path.stat().st_mtime == 2345.75


def test_game_status_preserves_host_result_and_names_workflow_projections(tmp_path, monkeypatch, capsys):
    from civ_agent import monitoring

    _, status, result = native_files(tmp_path)
    host = host_publication(tmp_path)
    write_private_json(tmp_path / "resolved.json", GameConfig().resolved().model_dump(mode="json"))

    def forbidden(*args, **kwargs):
        pytest.fail("Native game status must not use generic scheduler record reads")

    monkeypatch.setattr(monitoring, "read_record", forbidden)
    assert game.run(game.parser(action="status").parse_args(["--state-dir", str(tmp_path)])) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["runtime_status"] == status.model_dump(mode="json")
    assert output["runtime_result"] == result.model_dump(mode="json")
    assert output["result"] == host
    assert output["result_updated_at"] == 1234.5
    assert output["result"]["result"]["winner"] == "Rome"


def test_game_status_reports_corrupt_native_publication_as_an_error(tmp_path, capsys):
    native_files(tmp_path)
    (tmp_path / "native-runtime/status.json").write_text("{")
    assert game.run(game.parser(action="status").parse_args(["--state-dir", str(tmp_path)])) == 2
    output = capsys.readouterr()
    assert not output.out and "Invalid native runtime projection" in output.err


def test_native_projection_ignores_scheduler_state_even_when_it_is_corrupt(tmp_path):
    native_files(tmp_path)
    store = MonitorStore(path_for(tmp_path))
    with store.engine.begin() as db:
        db.execute(records.insert().values(collection="state", key="run", payload="{", updated_ms=0))
    store.engine.dispose()
    projection = native_projection(tmp_path)
    assert projection.result is None and projection.result_updated_at is None


def test_full_native_host_result_preserves_accounting_and_is_read_only(tmp_path):
    request, _, _ = native_files(tmp_path, identifier="container-game-id")
    host = host_publication(tmp_path)
    path = path_for(tmp_path)
    before = path.read_bytes(), path.stat().st_mtime_ns
    projection = native_projection(tmp_path)
    assert projection.result is not None and projection.result.model_dump(mode="json") == host
    assert projection.result.matchId != request.authority.game
    assert projection.result.usage.complete is True
    assert projection.result.usage.cached_input_tokens == 192
    assert projection.result.usage.reasoning_output_tokens == 128
    assert projection.result.players[0].reviewUsage.output_tokens == 56
    assert projection.result_updated_at == 1234.5
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_native_budget_pause_keeps_observed_usage_without_a_game_winner(tmp_path):
    native_files(tmp_path)
    host = host_publication(tmp_path, finished=False)
    host["usage"]["complete"] = False
    publish_host(tmp_path, host)
    result = native_projection(tmp_path).result
    assert result is not None and result.status == "incomplete" and result.result is None
    assert result.usage.complete is False and result.usage.input_tokens == 640


@pytest.mark.parametrize("conflict", ["row", "payload", "terminal", "no-authority"])
def test_native_host_result_requires_matching_epoch_provenance(tmp_path, conflict):
    if conflict != "no-authority":
        native_files(tmp_path)
    host = host_publication(tmp_path)
    if conflict == "payload":
        host["epoch"] = "foreign"
        host["result"]["epoch"] = "foreign"
    elif conflict == "terminal":
        host["result"]["epoch"] = "foreign"
    publish_host(tmp_path, host, row_epoch="foreign" if conflict == "row" else "epoch")
    with pytest.raises(ContainerError, match="epoch conflicts|Invalid native host result"):
        native_projection(tmp_path)


@pytest.mark.parametrize("invalid", ["missing_usage", "extra_content", "invalid_json", "wrong_total"])
def test_invalid_host_payloads_fail_without_exposing_private_values(tmp_path, invalid):
    native_files(tmp_path)
    host = host_publication(tmp_path)
    if invalid == "missing_usage":
        del host["usage"]["reasoning_output_tokens"]
    elif invalid == "extra_content":
        host["privateNotes"] = "PRIVATE MODEL CONTENT"
    elif invalid == "wrong_total":
        host["usage"]["total_tokens"] += 1
    publish_host(tmp_path, host)
    if invalid == "invalid_json":
        with closing(sqlite3.connect(path_for(tmp_path))) as writer:
            writer.execute("UPDATE monitor_records SET payload='{' WHERE collection='state' AND key='result'")
            writer.commit()
    with pytest.raises(ContainerError, match="Invalid native host result") as caught:
        native_projection(tmp_path)
    assert "PRIVATE MODEL CONTENT" not in str(caught.value)


@pytest.mark.parametrize("shape", ["empty", "incompatible", "corrupt"])
def test_host_publication_file_initialization_and_corruption(tmp_path, shape):
    native_files(tmp_path)
    path = path_for(tmp_path)
    path.parent.mkdir()
    if shape == "corrupt":
        path.write_bytes(b"broken SQLite publication")
    else:
        with closing(sqlite3.connect(path)) as writer:
            if shape == "incompatible":
                writer.execute("CREATE TABLE other(id INTEGER)")
    if shape == "empty":
        assert native_projection(tmp_path).result is None
    else:
        with pytest.raises(ContainerError, match="native host result publication"):
            native_projection(tmp_path)


def test_locked_host_publication_is_a_visible_error_and_not_an_old_result_fallback(tmp_path):
    native_files(tmp_path)
    host_publication(tmp_path)
    with closing(sqlite3.connect(path_for(tmp_path))) as writer:
        writer.execute("PRAGMA journal_mode=DELETE")
        writer.execute("BEGIN EXCLUSIVE")
        try:
            with pytest.raises(ContainerError, match="Cannot read native host result"):
                native_projection(tmp_path)
        finally:
            writer.rollback()
    assert native_projection(tmp_path).result is not None
