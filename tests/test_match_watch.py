import asyncio
import json
import subprocess
from unittest.mock import Mock

import pytest
from textual.widgets import DataTable, RichLog, Static, TabbedContent

from civ_agent import cli, container_support, match_watch
from civ_agent.monitoring import (
    MonitorStore,
    path_for,
    put_record,
    record_key,
)
from civ_agent.monitoring import records as table
from civ_agent.protocol import JsonObject
from civ_agent.runner_history import EventHistory
from civ_agent.watch_state import WatchReader


def put_state(root, key, value):
    MonitorStore(path_for(root)).put("state", key, value)


def put_history(root, name, turn, job, values):
    log = EventHistory(path_for(root), "g1", name, job, game_turn=turn)
    log.append(values)


def append_events(root, values):
    put_history(root, "Rome", 2, "job2", values)


def put_source(root, value):
    MonitorStore(path_for(root)).put("streams", record_key("g1", "Rome", "job2"), value)


def event(sequence, kind, **data):
    return {"sequence": sequence, "time": f"2026-09-21T12:00:{sequence:02d}+00:00", "kind": kind, "data": data}


def fixture(root) -> tuple[JsonObject, list[JsonObject]]:
    usage = {"input_tokens": 2000, "cached_input_tokens": 1500, "output_tokens": 100, "reasoning_output_tokens": 20}
    records: list[JsonObject] = []
    for handoff, (name, turn, phase) in enumerate(
        (("Rome", 1, "ended"), ("Greece", 1, "ended"), ("Rome", 2, "active"))
    ):
        job = f"job{handoff}"
        put_history(
            root,
            name,
            turn,
            job,
            [
                event(0, "developer_message", text="Repeated startup context", source="service_startup"),
                event(1, "assistant_delta", itemId="a", text=f"{name} is "),
                event(2, "assistant_message", itemId="a", text=f"{name} is expanding."),
                event(3, "action", itemId="call1", description=f"{name} founded a city on turn {turn}."),
                event(4, "action", itemId="call1", description="Duplicate action must be hidden"),
                event(5, "tool_call", itemId="call2", tool="movement_preview", arguments={}),
                event(6, "tool_result", itemId="call2", result={"structuredContent": {"error": "Stale move"}}),
            ],
        )
        records.append(
            {
                "turn": {"epoch": "g1", "civilization": name, "gameTurn": turn, "handoff": handoff, "status": phase},
                "jobs": [job],
                "accountingComplete": phase == "ended",
                "usage": usage,
                "gameplayElapsedMs": 25000 if phase == "ended" else None,
                "finalizationElapsedMs": 1500 if phase == "ended" else None,
                "hostCalls": {"tool:player_command": {"calls": 3}, "command:FOUND_CITY": {"calls": 1}},
                "modelRequests": 5,
                "memoryEdits": {
                    "batches": 2,
                    "accepted": 3,
                    "rejected": 1,
                    "rejectedBatches": 1,
                    "rejectionReasons": {"SOURCE_NOT_DELIVERED": 1},
                    "complete": True,
                },
            }
        )
    run = {
        "epoch": "g1",
        "status": "running",
        "sessions": {
            "Rome": {"effectiveSettings": {"provider": "openai", "model": "gpt-6-luna", "reasoningEffort": "high"}},
            "Greece": {
                "effectiveSettings": {
                    "provider": "openrouter",
                    "model": "anthropic/claude-sonnet-4.5",
                    "reasoningEffort": "high",
                }
            },
        },
        "inFlight": {
            "civilization": "Rome",
            "gameTurn": 2,
            "handoff": 2,
            "activationId": "job2",
            "agentPhase": "RUNNING",
        },
        "pendingFinalizations": [],
        "usage": usage,
    }
    put_state(root, "run", run)
    for row in records:
        MonitorStore(path_for(root)).put("turns", "g1/" + str(row["turn"]["handoff"]), row)
    put_state(
        root,
        "result",
        {
            "epoch": "g1",
            "status": "running",
            "usage": {**usage, "complete": False},
            "scores": [
                {"civilization": "Rome", "score": 42, "controller": "AGENT"},
                {"civilization": "Greece", "score": 38, "controller": "AGENT"},
                {"civilization": "Egypt", "score": 50, "controller": "BUILT_IN_AI"},
            ],
            "players": [
                {"civilization": "Rome", "usage": usage, "hostCalls": records[-1]["hostCalls"], "modelRequests": 10}
            ],
            "hostCalls": records[-1]["hostCalls"],
            "modelRequests": 15,
        },
    )
    return run, records


def test_reader_preserves_accounting_deduplicates_messages_and_never_writes(tmp_path):
    fixture(tmp_path)
    before = path_for(tmp_path).read_bytes()
    reader = WatchReader(tmp_path)
    view = reader.read()
    assert view.players == ["Rome", "Greece", "Egypt"]
    assert len(view.turns) == 3 and not view.errors
    latest = view.latest_turn("Rome")
    assert latest is not None and latest["usage"]["cached_input_tokens"] == 1500
    recent = [entry for entry in view.activity if entry.civilization == "Rome" and entry.turn == 2]
    assert [e.text for e in recent if e.label == "Assistant"] == ["Rome is expanding."]
    assert len([e for e in recent if e.label == "Action"]) == 1
    assert [e for e in recent if e.label == "Developer instructions"][0].verbose
    assert not [e for e in recent if e.label == "Error"][0].verbose
    assert reader.read() == view
    assert before == path_for(tmp_path).read_bytes()


def test_uncommitted_events_are_invisible_and_failed_reads_retain_previous_display(tmp_path):
    fixture(tmp_path)
    reader = WatchReader(tmp_path)
    original = reader.read()
    store = MonitorStore(path_for(tmp_path))
    key = record_key("g1", "Rome", "job2")
    with store.engine.connect() as writer, writer.begin() as transaction:
        put_record(
            writer,
            "events",
            record_key(key, 7),
            event(7, "assistant_message", itemId="b", text="New decision"),
            epoch="g1",
            civilization="Rome",
            activation="job2",
            sequence=7,
        )
        assert reader.read().activity == original.activity
        transaction.rollback()
    assert reader.read().activity == original.activity
    append_events(tmp_path, [event(7, "assistant_message", itemId="b", text="New decision")])
    updated = reader.read()
    assert any(e.text == "New decision" for e in updated.activity)
    store.put(
        "events",
        record_key(key, 8),
        {"sequence": 8, "kind": "assistant_message", "data": None},
        epoch="g1",
        civilization="Rome",
        activation="job2",
        sequence=8,
    )
    failed = reader.read()
    assert failed.errors and failed.activity == updated.activity


def test_old_epoch_and_other_players_history_are_not_mixed(tmp_path):
    run, records = fixture(tmp_path)
    reader = WatchReader(tmp_path)
    past = reader.read(("Rome", 1))
    assert all(e.civilization == "Rome" and e.turn == 1 for e in past.activity)
    put_state(tmp_path, "run", {**run, "epoch": "g2"})
    view = reader.read()
    assert view.result == {} and view.turns == () and view.activity == ()
    assert len(view.errors) >= 1
    put_state(tmp_path, "run", run)
    put_source(tmp_path, {"epoch": "g1", "civilization": "Greece", "gameTurn": 2, "activationId": "job2"})
    view = reader.read(("Rome", 2))
    assert view.activity == () and view.errors


def test_chunked_startup_is_hidden_and_control_sequences_are_inert(tmp_path):
    fixture(tmp_path)
    payload = json.dumps({"source": "service_startup", "text": "Private repeated preload"})
    put_history(
        tmp_path,
        "Rome",
        2,
        "chunked",
        [
            event(0, "developer_message", encoding="json", chunkIndex=0, chunkCount=2, text=payload[:20]),
            event(1, "developer_message", encoding="json", chunkIndex=1, chunkCount=2, text=payload[20:]),
            event(2, "assistant_message", itemId="visible", text="[bold]literal[/bold]\x1b[2J safe"),
        ],
    )
    put_state(
        tmp_path, "run", {"epoch": "g1", "inFlight": {"civilization": "Rome", "gameTurn": 2, "activationId": "chunked"}}
    )
    with MonitorStore(path_for(tmp_path)).engine.begin() as db:
        db.execute(table.delete().where(table.c.collection == "turns"))
    view = WatchReader(tmp_path).read(("Rome", 2))
    assert view.activity[0].verbose and view.activity[0].text == "Private repeated preload"
    assert view.activity[1].text == "[bold]literal[/bold] safe"


def log_text(app):
    return "\n".join(line.text for line in app.query_one("#activity", RichLog).lines)


def test_dashboard_shows_review_wait_before_gameplay_starts(tmp_path):
    run, _ = fixture(tmp_path)
    flight = run.pop("inFlight")
    run["reviewPhase"] = {
        "civilizations": ["Rome", "Greece"],
        "gameTurn": 2,
        "reviewTurn": 1,
        "stage": "Reviewing",
    }
    put_state(tmp_path, "run", run)

    async def check():
        app = match_watch.MatchWatch(tmp_path, refresh=60)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert "Shared review after round 1" in str(app.query_one("#status", Static).render())
            assert str(app.query_one("#players", DataTable).get_row("Rome")[1]) == "Reviewing"
            run.pop("reviewPhase")
            run["inFlight"] = flight
            put_state(tmp_path, "run", run)
            await app.poll_records()
            await pilot.pause()
            assert "Shared review" not in str(app.query_one("#status", Static).render())
            assert str(app.query_one("#players", DataTable).get_row("Rome")[1]) == "Running"

    asyncio.run(check())


def test_dashboard_model_warning_clears_on_recovery_and_stays_in_history(tmp_path):
    run, _ = fixture(tmp_path)
    notice = {
        "state": "retrying",
        "message": "Model connection idle timeout; retrying (1/5).",
        "elapsedSeconds": 301,
    }
    run["inFlight"]["modelStatus"] = notice
    put_state(tmp_path, "run", run)
    append_events(tmp_path, [event(7, "model_status", **notice)])

    async def check():
        app = match_watch.MatchWatch(tmp_path, refresh=60)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert "retrying (1/5)" in str(app.query_one("#status", Static).render())
            assert "301s" in str(app.query_one("#status", Static).render())
            assert str(app.query_one("#players", DataTable).get_row("Rome")[1]) == "Model retrying"
            assert "idle timeout" in log_text(app)
            resumed = {"state": "responding", "message": "Model response resumed after 315s without progress."}
            run["inFlight"]["modelStatus"] = resumed
            put_state(tmp_path, "run", run)
            append_events(tmp_path, [event(8, "model_status", **resumed)])
            await app.poll_records()
            await pilot.pause()
            assert "retrying" not in str(app.query_one("#status", Static).render())
            assert str(app.query_one("#players", DataTable).get_row("Rome")[1]) == "Running"
            assert "idle timeout" in log_text(app) and "resumed after 315s" in log_text(app)

    asyncio.run(check())


def test_dashboard_distinguishes_negotiation_suspension_from_model_stall(tmp_path):
    run, _ = fixture(tmp_path)
    run["inFlight"].update(
        negotiationWait={"state": "waiting"},
        modelStatus={"state": "waiting", "message": "No model progress", "elapsedSeconds": 301},
    )
    put_state(tmp_path, "run", run)

    async def check():
        app = match_watch.MatchWatch(tmp_path, refresh=60)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert "No model progress" not in str(app.query_one("#status", Static).render())
            assert str(app.query_one("#players", DataTable).get_row("Rome")[1]) == "Negotiating (model suspended)"
            run["inFlight"].pop("negotiationWait")
            run["inFlight"].pop("modelStatus")
            put_state(tmp_path, "run", run)
            await app.poll_records()
            await pilot.pause()
            assert str(app.query_one("#players", DataTable).get_row("Rome")[1]) == "Running"

    asyncio.run(check())


def test_dashboard_shows_quota_retry_countdown_and_clears_on_recovery(tmp_path, monkeypatch):
    from civ_agent.tls import write_private_json

    run, _ = fixture(tmp_path)
    run["status"] = "paused"
    run["inFlight"].update(agentPhase="FAILED", modelStatus={"state": "failed", "reason": "usageLimitExceeded"})
    put_state(tmp_path, "run", run)
    now = [1000]
    monkeypatch.setattr(match_watch.time, "time", lambda: now[0])
    write_private_json(
        tmp_path / "container-status.json",
        {
            "status": "retrying",
            "recovery": {"reason": "usageLimitExceeded", "civilization": "Rome", "attempt": 6, "nextRetryAt": 1300},
        },
    )

    async def check():
        app = match_watch.MatchWatch(tmp_path, refresh=60)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            banner = str(app.query_one("#status", Static).render())
            assert "Quota exhausted" in banner and "5m 00s" in banner and "Attempt 6" in banner
            assert str(app.query_one("#players", DataTable).get_row("Rome")[1]) == "Quota exhausted"
            now[0] += 120
            await app.poll_records()
            await pilot.pause()
            assert "3m 00s" in str(app.query_one("#status", Static).render())
            run["status"] = "running"
            run["inFlight"].update(agentPhase="RUNNING", modelStatus=None)
            put_state(tmp_path, "run", run)
            write_private_json(tmp_path / "container-status.json", {"status": "running"})
            await app.poll_records()
            await pilot.pause()
            assert "Quota exhausted" not in str(app.query_one("#status", Static).render())
            assert str(app.query_one("#players", DataTable).get_row("Rome")[1]) == "Running"

    asyncio.run(check())


def test_dashboard_navigation_live_refresh_freeze_and_quit(tmp_path):
    run, _ = fixture(tmp_path)
    run["pendingFinalizations"] = [{"civilization": "Greece", "gameTurn": 1, "activationId": "job1"}]
    put_state(tmp_path, "run", run)
    before = path_for(tmp_path).read_bytes()

    async def check():
        app = match_watch.MatchWatch(tmp_path, refresh=60)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            players = app.query_one("#players", DataTable)
            assert players.row_count == 3
            assert str(players.get_row("Rome")[0]) == "Rome · gpt-6-luna"
            assert str(players.get_row("Greece")[0]) == "Greece · anthropic/claude-sonnet-4.5"
            assert str(players.get_row("Egypt")[0]) == "Egypt"
            assert str(players.get_row("Rome")[-1]) == "3"  # Command breakdown is not counted twice.
            assert str(players.get_row("Egypt")[1]) == "Built-in AI"
            assert str(players.get_row("Greece")[1]) == "Bookkeeping"
            assert "Repeated startup context" not in log_text(app)
            assert "Stale move" in log_text(app)
            players.focus()
            await pilot.press("enter")
            assert app.civilization == "Rome"
            assert "Greece is expanding" not in log_text(app)
            await pilot.press("v")
            assert "Repeated startup context" in log_text(app)
            await pilot.press("v", "space")
            assert app.frozen
            put_state(tmp_path, "run", {"epoch": "g1", "status": "paused", "reason": "Operator paused"})
            await app.poll_records()
            assert app.snapshot.run["status"] == "running"
            await pilot.press("space")
            assert app.snapshot.run["status"] == "paused"
            tabs = app.query_one("#main-tabs", TabbedContent)
            assert [
                pane.id for pane in tabs.query("TabPane") if pane.parent is tabs.get_pane("activity-tab").parent
            ] == ["activity-tab", "diplomacy-tab", "strategy-tab", "economy-tab", "military-tab"]
            assert not app.query("#turns") and not app.query("#calls")
            await pilot.press("l", "a")
            assert app.civilization is None
            log = app.query_one("#activity", RichLog)
            log.scroll_end(animate=False)
            await pilot.pause()
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            assert log.size.height >= 4 and log.is_vertical_scroll_end
            log.scroll_home(animate=False)
            await pilot.pause()
            append_events(tmp_path, [event(7, "assistant_message", itemId="last", text="Latest action")])
            await app.poll_records()
            await pilot.pause()
            assert log.scroll_y == 0  # Refresh does not pull the reader away from older text.
            await pilot.press("q")
        assert app.return_code == 0

    asyncio.run(check())
    assert path_for(tmp_path).read_bytes() != before
    assert not list(tmp_path.rglob("*.jsonl")) and not list(tmp_path.rglob("*.md"))


def test_dashboard_shows_selected_effort_and_pending_without_substituting_default(tmp_path):
    run, _ = fixture(tmp_path)
    run["sessions"]["Rome"]["configuredSettings"] = {
        "provider": "openai",
        "model": "gpt-6-luna",
        "reasoningEffort": "medium",
    }
    run["inFlight"]["effectiveSettings"] = {"provider": "openai", "model": "gpt-6-luna", "reasoningEffort": "low"}
    put_state(tmp_path, "run", run)

    async def check():
        app = match_watch.MatchWatch(tmp_path, refresh=60)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            row = app.query_one("#players", DataTable).get_row("Rome")
            assert str(row[2]) == "low (default medium)"
            assert "Effort low (default medium)" in str(app.query_one("#status", Static).render())
            run["inFlight"].pop("effectiveSettings")
            run["inFlight"]["agentPhase"] = "STARTING"
            put_state(tmp_path, "run", run)
            await app.poll_records()
            await pilot.pause()
            assert str(app.query_one("#players", DataTable).get_row("Rome")[2]) == "pending (default medium)"

    asyncio.run(check())


def test_empty_and_completed_matches(tmp_path):
    async def check():
        app = match_watch.MatchWatch(tmp_path, refresh=60)
        async with app.run_test(size=(80, 24)) as pilot:
            assert app.snapshot.players == []
            put_state(
                tmp_path,
                "result",
                {
                    "epoch": "g1",
                    "status": "completed",
                    "result": {"winner": "Egypt", "victoryType": "Time"},
                    "scores": [{"civilization": "Egypt", "score": 999, "controller": "BUILT_IN_AI"}],
                },
            )
            await app.poll_records()
            assert str(app.query_one("#players", DataTable).get_row("Egypt")[4]) == "999"
            assert "Winner: Egypt" in str(app.query_one("#status", Static).render())
            await pilot.press("q")

    asyncio.run(check())


def test_batch_dashboard_switches_games(tmp_path):
    batch = tmp_path / "batch"
    first, second = batch / "games/g0001", batch / "games/g0002"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    fixture(first)
    second_run, _ = fixture(second)
    second_run.update(status="paused", reason="Capacity recovery")
    put_state(second, "run", second_run)
    (batch / "batch.json").write_text(
        json.dumps(
            {
                "format": 2,
                "parallel": 2,
                "runs": [{"id": "g0001", "status": "running"}, {"id": "g0002", "status": "paused"}],
            }
        )
    )

    async def check():
        app = match_watch.MatchWatch(batch, refresh=60)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.game_id == "g0001"
            assert "g0001 running" in str(app.query_one("#game-switcher", Static).render())
            await pilot.press("right_square_bracket")
            await pilot.pause()
            assert app.game_id == "g0002" and app.snapshot.run["status"] == "paused"
            assert "[2/2] g0002" in app.sub_title
            await pilot.press("right_square_bracket")
            await pilot.pause()
            assert app.game_id == "g0001"
            await pilot.press("left_square_bracket")
            await pilot.pause()
            assert app.game_id == "g0002"

    asyncio.run(check())


def test_watch_cli_dispatch_and_nonterminal_error(tmp_path, monkeypatch, capsys):
    run = Mock(return_value=7)
    monkeypatch.setattr(match_watch, "run", run)
    assert cli.main(["match", "watch", "--state-dir", str(tmp_path), "--verbose"]) == 7
    assert run.call_args.args[0].state_dir == tmp_path and run.call_args.args[0].verbose
    monkeypatch.undo()
    monkeypatch.setattr(match_watch.sys.stdin, "isatty", lambda: False)
    assert match_watch.run(match_watch.parser().parse_args(["--state-dir", str(tmp_path)])) == 2
    assert "interactive terminal" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_batch_manifest_rejects_duplicate_or_unsafe_game_ids(tmp_path):
    manifest = tmp_path / "batch.json"
    manifest.write_text(json.dumps({"format": 2, "runs": [{"id": "g0001"}, {"id": "g0001"}]}))
    with pytest.raises(ValueError, match="Invalid game ID"):
        match_watch.game_directories(tmp_path)
    manifest.write_text(json.dumps({"format": 2, "runs": [{"id": "../other"}]}))
    with pytest.raises(ValueError, match="Invalid game ID"):
        match_watch.game_directories(tmp_path)


def test_local_dashboard_can_view_a_container_game_without_starting_containers(tmp_path, monkeypatch):
    manifest = tmp_path / "container.json"
    manifest.write_text(json.dumps({"images": {"runtime": "sha256:pinned"}}))
    original = manifest.read_bytes()
    monkeypatch.setattr(container_support, "IN_CONTAINER", False)
    mounts = Mock(return_value=[])
    monkeypatch.setattr(container_support, "watch_mounts", mounts)
    launch = Mock(return_value=7)
    monkeypatch.setattr(subprocess, "call", launch)
    viewer = Mock()
    monkeypatch.setattr(match_watch, "MatchWatch", viewer)
    monkeypatch.setattr(match_watch.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(match_watch.sys.stdout, "isatty", lambda: True)
    arguments = ["match", "watch", "--state-dir", str(tmp_path), "--verbose"]

    assert cli.main(arguments) == 7
    assert launch.call_args.args[0][-3:] == ["sha256:pinned", "watch", "--verbose"]
    viewer.assert_not_called()
    launch.reset_mock()
    mounts.reset_mock()

    assert cli.main([*arguments, "--local"]) == 0
    viewer.assert_called_once_with(tmp_path.resolve(), verbose=True)
    viewer.return_value.run.assert_called_once_with()
    launch.assert_not_called()
    mounts.assert_not_called()
    assert manifest.read_bytes() == original
    assert list(tmp_path.iterdir()) == [manifest]

    batch = tmp_path / "batch"
    batch.mkdir()
    (batch / "batch.json").write_text(json.dumps({"format": 2, "runs": [{"id": "g0001"}]}))
    (batch / "container.json").write_text(json.dumps({"images": None}))
    viewer.reset_mock()

    assert cli.main(["match", "watch", "--state-dir", str(batch)]) == 0
    viewer.assert_called_once_with(batch.resolve(), verbose=False)
    viewer.return_value.run.assert_called_once_with()
    launch.assert_not_called()
    mounts.assert_not_called()
