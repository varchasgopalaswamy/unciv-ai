import asyncio
import json
import subprocess
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from pydantic_ai.messages import (
    BinaryContent,
    ModelRequest,
    ModelResponse,
    RetryFeedbackPart,
    RetryInfo,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from textual.widgets import DataTable, RichLog, Static, TabbedContent

from civ_agent import cli, container_support, match_watch
from civ_agent.monitoring import MonitorStore, path_for
from civ_agent.tls import write_private_json
from civ_agent.watch_state import WatchReader
from tests.native_watch_fixtures import base_history, fixture, history, memory, persistent_files, publish, task


def put_state(root, key, value):
    assert key == "diplomacy", "Native watch never consumes scheduler state.run"
    publish(root, "state", key, value, epoch=value["epoch"])


def log_text(app):
    return "\n".join(line.text for line in app.query_one("#activity", RichLog).lines)


def test_reader_uses_native_accounting_and_public_sdk_history_without_writes(tmp_path):
    fixture(tmp_path)
    base_history(tmp_path)
    files = persistent_files(tmp_path)
    reader = WatchReader(tmp_path)
    view = reader.read()
    assert not view.errors and view.players == ["Rome", "Greece", "Egypt"]
    latest = view.latest_turn("Rome")
    assert latest is not None and len(view.turns) == 3 and latest["turn"]["gameTurn"] == 2
    assert view.result["usage"]["cached_input_tokens"] == 192
    assert [entry.text for entry in view.activity if entry.label == "Assistant"] == [
        "Greece is expanding.",
        "Rome is expanding.",
    ]
    assert any(entry.verbose and entry.label == "Developer instructions" for entry in view.activity)
    assert reader.read() == view
    assert files == persistent_files(tmp_path)


def test_selection_and_sdk_inherited_messages_preserve_exact_run_origin(tmp_path):
    fixture(tmp_path)
    prepared = task(tmp_path)
    first = ModelResponse([TextPart("Original decision")], run_id="first")
    asyncio.run(history(tmp_path, prepared, run_id="first", messages=[first]))
    asyncio.run(
        history(
            tmp_path,
            prepared,
            run_id="second",
            messages=[first, ModelResponse([TextPart("Corrected decision")], run_id="second")],
        )
    )
    base_history(tmp_path)
    view = WatchReader(tmp_path).read(("Rome", 2))
    assert sum(entry.text == "Original decision" for entry in view.activity) == 1
    assert sum(entry.text == "Corrected decision" for entry in view.activity) == 1
    assert all(entry.civilization == "Rome" and entry.turn == 2 for entry in view.activity)


def test_native_sdk_feedback_is_visible_even_when_original_command_arguments_are_invalid(tmp_path):
    fixture(tmp_path)
    prepared = task(tmp_path)
    asyncio.run(
        history(
            tmp_path,
            prepared,
            run_id="correction",
            messages=[
                ModelResponse(
                    [ToolCallPart("execute_commands", "{invalid JSON", tool_call_id="invalid")],
                    run_id="correction",
                ),
                ModelRequest(
                    [
                        ToolReturnPart(
                            "execute_commands",
                            "Supply canonical command arguments.",
                            tool_call_id="invalid",
                            outcome="retried",
                            retry=RetryInfo(cause="validation_error", attempt=1, max_retries=1),
                        ),
                        RetryFeedbackPart("Return the required output.", retry=RetryInfo(cause="model_retry")),
                    ],
                    run_id="correction",
                ),
            ],
        )
    )
    view = WatchReader(tmp_path).read(("Rome", 2))
    assert not view.errors
    assert any(entry.label == "Command request" and "validation" in entry.text for entry in view.activity)
    corrections = [entry for entry in view.activity if entry.label == "Correction"]
    assert len(corrections) == 2 and all(not entry.verbose for entry in corrections)


def test_uncommitted_native_rows_are_invisible_and_storage_failure_keeps_same_epoch_metrics(tmp_path):
    fixture(tmp_path)
    reader = WatchReader(tmp_path)
    before = reader.read()
    store = MonitorStore(path_for(tmp_path))
    with store.engine.connect() as db, db.begin() as transaction:
        db.exec_driver_sql("UPDATE monitor_records SET payload='not json' WHERE collection='turns'")
        assert reader.read() == before
        transaction.commit()
    failed = reader.read()
    assert failed.errors and failed.turns == before.turns
    store.engine.dispose()


def test_legacy_scheduler_rows_and_other_epoch_histories_are_not_a_fallback(tmp_path):
    publish(tmp_path, "state", "run", {"epoch": "old", "status": "running", "private": "old scheduler"})
    assert WatchReader(tmp_path).read().run == {}
    request, status = fixture(tmp_path)
    base_history(tmp_path)
    reader = WatchReader(tmp_path)
    assert reader.read().activity
    request = request.model_copy(update={"authority": request.authority.model_copy(update={"epoch": "next"})})
    write_private_json(tmp_path / "native-runtime/game.json", request.model_dump(mode="json"))
    changed = reader.read()
    assert changed.epoch == "next" and changed.activity == () and changed.result == {} and changed.errors
    assert "old scheduler" not in str(changed)


def test_literal_sdk_content_is_inert_and_multimodal_data_not_stringified(tmp_path):
    fixture(tmp_path)
    prepared = task(tmp_path)
    messages = [
        ModelRequest(
            [UserPromptPart(["Visible caption", BinaryContent(data=b"PRIVATE IMAGE BYTES", media_type="image/png")])],
            run_id="media",
        ),
        ModelResponse([TextPart("[bold]literal[/bold]\x1b[2J safe")], run_id="media"),
    ]
    asyncio.run(history(tmp_path, prepared, run_id="media", messages=messages))
    view = WatchReader(tmp_path).read()
    entry = next(entry for entry in view.activity if entry.label == "Assistant")
    assert entry.text == "[bold]literal[/bold] safe"
    assert "Visible caption" in str(view.activity) and "PRIVATE IMAGE BYTES" not in str(view.activity)
    assert "UFJJVkFURSBJTUFHRSBCWVRFUw==" not in str(view.activity)


def test_corrupt_native_sdk_database_is_bounded_and_does_not_drop_real_accounting(tmp_path):
    from civ_agent.player_paths import ManagedPlayerPaths

    fixture(tmp_path)
    prepared = task(tmp_path)
    asyncio.run(history(tmp_path, prepared))
    paths = ManagedPlayerPaths(tmp_path, 0, "Rome")
    (paths.store / "agent-steps.sqlite3").write_bytes(b"PRIVATE malformed sqlite")
    view = WatchReader(tmp_path).read()
    assert view.errors and len(view.turns) == 3 and view.result["modelRequests"] == 64
    assert "PRIVATE" not in str(view.errors) and view.activity == ()


def test_prepared_native_effort_is_distinct_from_frozen_operator_default(tmp_path):
    fixture(tmp_path)
    prepared = task(tmp_path)
    prepared = prepared.model_copy(
        update={"selection": prepared.selection.model_copy(update={"reasoning_effort": "low"})}
    )
    state = memory(tmp_path)
    with state.transaction() as db:
        record = state._meta(db, "nativeTurn:ticket-Rome-2")
        record["requests"]["gameplay:0"] = prepared.model_dump(mode="json")
        state._set(db, "nativeTurn:ticket-Rome-2", record)
    state.engine.dispose()
    view = WatchReader(tmp_path).read()
    assert view.run["sessions"]["Rome"]["configuredSettings"]["reasoningEffort"] == "medium"
    assert view.run["inFlight"]["effectiveSettings"]["reasoningEffort"] == "low"


def test_native_command_requests_and_recorded_receipt_status_remain_visible_in_concise_feed(tmp_path):
    fixture(tmp_path)
    prepared = task(tmp_path)
    messages = [
        ModelResponse(
            [
                ToolCallPart(
                    "execute_commands",
                    {"commands": [{"kind": "MOVE_UNIT", "unit": "private-detail"}]},
                    tool_call_id="command",
                )
            ],
            run_id="commands",
            timestamp=datetime(2026, 11, 5, 12, tzinfo=UTC),
        ),
        ModelRequest(
            [
                ToolReturnPart(
                    "execute_commands",
                    {"receipts": [{"status": "confirmed"}, {"status": "unexecuted"}]},
                    tool_call_id="command",
                )
            ],
            run_id="commands",
        ),
    ]
    asyncio.run(history(tmp_path, prepared, run_id="commands", messages=messages))
    concise = [entry for entry in WatchReader(tmp_path).read().activity if not entry.verbose]
    assert [(entry.label, entry.text) for entry in concise] == [
        ("Command request", "MOVE_UNIT"),
        ("Command receipts", "1 confirmed · 1 unexecuted"),
    ]
    assert "private-detail" not in str(concise)


def test_dashboard_native_metrics_navigation_freeze_and_quit(tmp_path):
    request, status = fixture(tmp_path)
    base_history(tmp_path)

    async def run():
        app = match_watch.MatchWatch(tmp_path, refresh=60)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            table = app.query_one("#players", DataTable)
            assert table.row_count == 3 and "gpt-6-luna" in str(table.get_row("Rome")[0])
            assert str(table.get_row("Egypt")[1]) == "Built-in AI"
            assert "Repeated startup" not in log_text(app) and "Rome is expanding" in log_text(app)
            table.focus()
            await pilot.press("enter", "v")
            assert app.civilization == "Rome" and "Greece is expanding" not in log_text(app)
            assert "Repeated startup" in log_text(app) and "Stale move" in log_text(app)
            await pilot.press("space")
            write_private_json(
                tmp_path / "native-runtime/status.json",
                status.model_copy(update={"mode": "PAUSED", "phase": "PAUSED", "quiesced": True}).model_dump(
                    mode="json"
                ),
            )
            await app.poll_records()
            assert app.snapshot.run["status"] == "running"
            await pilot.press("space", "a")
            assert app.snapshot.run["status"] == "paused" and app.civilization is None
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            assert app.query_one("#activity", RichLog).size.height >= 4
            tabs = app.query_one("#main-tabs", TabbedContent)
            assert tabs.active == "activity-tab"
            await pilot.press("q")
        assert app.return_code == 0

    asyncio.run(run())


def test_dashboard_current_native_failure_is_bounded_typed_notice(tmp_path):
    from civ_agent.orchestration.contracts import ModelFailure, Scope

    _, status = fixture(tmp_path)
    task(tmp_path)
    failure = ModelFailure(
        scope=Scope(game="game", player="p0", epoch="g1", activation="role"),
        run_id="same-sdk",
        model_step=1,
        attempt=1,
        category="quota",
        dispatch="rejected",
        retry_mode="automatic",
    )
    write_private_json(
        tmp_path / "native-runtime/status.json",
        status.model_copy(update={"model_failures": [failure]}).model_dump(mode="json"),
    )

    async def run():
        app = match_watch.MatchWatch(tmp_path, refresh=60)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert "quota · automatic recovery" in str(app.query_one("#status", Static).render())
            assert str(app.query_one("#players", DataTable).get_row("Rome")[1]) == "Model retrying"
            write_private_json(tmp_path / "native-runtime/status.json", status.model_dump(mode="json"))
            await app.poll_records()
            assert "automatic recovery" not in str(app.query_one("#status", Static).render())

    asyncio.run(run())


def test_watch_cli_dispatch_and_nonterminal_error(tmp_path, monkeypatch, capsys):
    run = Mock(return_value=7)
    monkeypatch.setattr(match_watch, "run", run)
    assert cli.main(["match", "watch", "--state-dir", str(tmp_path), "--verbose"]) == 7
    assert run.call_args.args[0].state_dir == tmp_path and run.call_args.args[0].verbose
    monkeypatch.undo()
    monkeypatch.setattr(match_watch.sys.stdin, "isatty", lambda: False)
    assert match_watch.run(match_watch.parser().parse_args(["--state-dir", str(tmp_path)])) == 2
    assert "interactive terminal" in capsys.readouterr().err and list(tmp_path.iterdir()) == []


def test_batch_manifest_rejects_duplicate_or_unsafe_game_ids(tmp_path):
    manifest = tmp_path / "batch.json"
    for values in (["g0001", "g0001"], ["../other"]):
        manifest.write_text(json.dumps({"format": 2, "runs": [{"id": value} for value in values]}))
        with pytest.raises(ValueError, match="Invalid game ID"):
            match_watch.game_directories(tmp_path)


def test_local_dashboard_can_view_container_game_without_starting_containers(tmp_path, monkeypatch):
    manifest = tmp_path / "container.json"
    manifest.write_text(
        json.dumps({"format": 2, "runtime": "pydantic-local-v1", "images": {"runtime": "sha256:pinned"}})
    )
    original = manifest.read_bytes()
    monkeypatch.setattr(container_support, "IN_CONTAINER", False)
    mounts, launch, viewer = Mock(return_value=[]), Mock(return_value=7), Mock()
    monkeypatch.setattr(container_support, "watch_mounts", mounts)
    monkeypatch.setattr(subprocess, "call", launch)
    monkeypatch.setattr(match_watch, "MatchWatch", viewer)
    monkeypatch.setattr(match_watch.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(match_watch.sys.stdout, "isatty", lambda: True)
    assert cli.main(["match", "watch", "--state-dir", str(tmp_path), "--verbose"]) == 7
    assert launch.call_args.args[0][-3:] == ["sha256:pinned", "watch", "--verbose"]
    assert cli.main(["match", "watch", "--state-dir", str(tmp_path), "--local"]) == 0
    viewer.return_value.run.assert_called_once_with()
    assert manifest.read_bytes() == original
