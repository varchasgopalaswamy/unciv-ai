import asyncio
import json
from unittest.mock import Mock

from textual.widgets import DataTable, RichLog, Static, TabbedContent

from civ_agent import cli, match_watch
from civ_agent.protocol import JsonObject
from civ_agent.watch_state import WatchReader


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n")


def write_lines(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(value) + "\n" for value in values))


def event(sequence, kind, **data):
    return {"sequence": sequence, "time": f"2026-09-21T12:00:{sequence:02d}+00:00", "kind": kind, "data": data}


def fixture(root) -> tuple[JsonObject, list[JsonObject]]:
    usage = {"input_tokens": 2000, "cached_input_tokens": 1500, "output_tokens": 100, "reasoning_output_tokens": 20}
    records = []
    for handoff, (name, turn, phase) in enumerate(
        (("Rome", 1, "ended"), ("Greece", 1, "ended"), ("Rome", 2, "active"))
    ):
        job = f"job{handoff}"
        directory = root / f"runner/history/{name}/turn_{turn}/attempt_{job}"
        write_json(
            directory / "source.json", {"epoch": "g1", "civilization": name, "gameTurn": turn, "activationId": job}
        )
        write_lines(
            directory / "events.jsonl",
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
        "sessions": {"Rome": {}, "Greece": {}},
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
    write_json(root / "runner/run.json", run)
    write_lines(root / "telemetry/turns.jsonl", records)
    write_json(
        root / "telemetry/game-result.json",
        {
            "epoch": "g1",
            "status": "running",
            "usage": {**usage, "complete": False},
            "scores": [
                {"civilization": "Rome", "score": 42, "controller": "AGENT"},
                {"civilization": "Greece", "score": 38, "controller": "AGENT"},
                {"civilization": "Egypt", "score": 50, "controller": "BUILT_IN_AI"},
            ],
            "players": [{"civilization": "Rome", "usage": usage, "hostCalls": records[-1]["hostCalls"]}],
            "hostCalls": records[-1]["hostCalls"],
        },
    )
    return run, records


def test_reader_preserves_accounting_deduplicates_messages_and_never_writes(tmp_path):
    fixture(tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
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
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_partial_append_corruption_and_atomic_replacement(tmp_path):
    fixture(tmp_path)
    reader = WatchReader(tmp_path)
    reader.read()
    path = tmp_path / "runner/history/Rome/turn_2/attempt_job2/events.jsonl"
    original = path.read_bytes()
    new = json.dumps(event(7, "assistant_message", itemId="b", text="New decision")).encode()
    path.write_bytes(original + new[:20])
    assert not any(e.text == "New decision" for e in reader.read().activity)
    path.write_bytes(original + new + b"\n")
    updated = reader.read()
    assert any(e.text == "New decision" for e in updated.activity)
    path.write_bytes(b"not json\n")
    corrupt = reader.read()
    assert corrupt.errors and corrupt.activity == updated.activity
    replacement = path.with_suffix(".tmp")
    replacement.write_bytes(original)
    replacement.replace(path)
    repaired = reader.read()
    assert not repaired.errors and not any(e.text == "New decision" for e in repaired.activity)


def test_old_epoch_and_other_players_history_are_not_mixed(tmp_path):
    run, records = fixture(tmp_path)
    reader = WatchReader(tmp_path)
    past = reader.read(("Rome", 1))
    assert all(e.civilization == "Rome" and e.turn == 1 for e in past.activity)
    write_json(tmp_path / "runner/run.json", {**run, "epoch": "g2"})
    view = reader.read()
    assert view.result == {} and view.turns == () and view.activity == ()
    assert len(view.errors) >= 1
    write_json(tmp_path / "runner/run.json", run)
    path = tmp_path / "runner/history/Rome/turn_2/attempt_job2/source.json"
    write_json(path, {"epoch": "g1", "civilization": "Greece", "gameTurn": 2, "activationId": "job2"})
    view = reader.read(("Rome", 2))
    assert view.activity == () and view.errors


def test_chunked_startup_is_hidden_and_control_sequences_are_inert(tmp_path):
    fixture(tmp_path)
    path = tmp_path / "runner/history/Rome/turn_2/attempt_job2/events.jsonl"
    payload = json.dumps({"source": "service_startup", "text": "Private repeated preload"})
    write_lines(
        path,
        [
            event(0, "developer_message", encoding="json", chunkIndex=0, chunkCount=2, text=payload[:20]),
            event(1, "developer_message", encoding="json", chunkIndex=1, chunkCount=2, text=payload[20:]),
            event(2, "assistant_message", itemId="visible", text="[bold]literal[/bold]\x1b[2J safe"),
        ],
    )
    view = WatchReader(tmp_path).read(("Rome", 2))
    assert view.activity[0].verbose and view.activity[0].text == "Private repeated preload"
    assert view.activity[1].text == "[bold]literal[/bold] safe"


def log_text(app):
    return "\n".join(line.text for line in app.query_one("#activity", RichLog).lines)


def test_dashboard_model_warning_clears_on_recovery_and_stays_in_history(tmp_path):
    run, _ = fixture(tmp_path)
    notice = {
        "state": "retrying",
        "message": "Model connection idle timeout; retrying (1/5).",
        "elapsedSeconds": 301,
    }
    run["inFlight"]["modelStatus"] = notice
    write_json(tmp_path / "runner/run.json", run)
    path = tmp_path / "runner/history/Rome/turn_2/attempt_job2/events.jsonl"
    with path.open("a") as stream:
        stream.write(json.dumps(event(7, "model_status", **notice)) + "\n")

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
            write_json(tmp_path / "runner/run.json", run)
            with path.open("a") as stream:
                stream.write(json.dumps(event(8, "model_status", **resumed)) + "\n")
            await app.poll_records()
            await pilot.pause()
            assert "retrying" not in str(app.query_one("#status", Static).render())
            assert str(app.query_one("#players", DataTable).get_row("Rome")[1]) == "Running"
            assert "idle timeout" in log_text(app) and "resumed after 315s" in log_text(app)

    asyncio.run(check())


def test_dashboard_navigation_live_refresh_freeze_and_quit(tmp_path):
    run, _ = fixture(tmp_path)
    run["pendingFinalizations"] = [{"civilization": "Greece", "gameTurn": 1, "activationId": "job1"}]
    write_json(tmp_path / "runner/run.json", run)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    async def check():
        app = match_watch.MatchWatch(tmp_path, refresh=60)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            players = app.query_one("#players", DataTable)
            assert players.row_count == 3
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
            write_json(tmp_path / "runner/run.json", {"epoch": "g1", "status": "paused", "reason": "Operator paused"})
            await app.poll_records()
            assert app.snapshot.run["status"] == "running"
            await pilot.press("space")
            assert app.snapshot.run["status"] == "paused"
            app.query_one(TabbedContent).active = "turns-tab"
            table = app.query_one("#turns", DataTable)
            table.focus()
            table.move_cursor(row=1)
            await pilot.press("enter")
            assert app.selected == ("Rome", 1)
            assert all(e.turn == 1 for e in app.snapshot.activity)
            assert "Turn 1 · Rome" in str(app.query_one("#metrics", Static).render())
            assert "Usage complete" in str(app.query_one("#metrics", Static).render())
            assert "Memory edits 3 accepted" in str(app.query_one("#metrics", Static).render())
            assert "SOURCE_NOT_DELIVERED: 1" in str(app.query_one("#metrics", Static).render())
            await pilot.press("l", "a")
            assert app.selected is None and app.civilization is None
            log = app.query_one("#activity", RichLog)
            log.scroll_end(animate=False)
            await pilot.pause()
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            assert log.size.height >= 4 and log.is_vertical_scroll_end
            log.scroll_home(animate=False)
            await pilot.pause()
            path = tmp_path / "runner/history/Rome/turn_2/attempt_job2/events.jsonl"
            with path.open("a") as stream:
                stream.write(json.dumps(event(7, "assistant_message", itemId="last", text="Latest action")) + "\n")
            await app.poll_records()
            await pilot.pause()
            assert log.scroll_y == 0  # Refresh does not pull the reader away from older text.
            await pilot.press("q")
        assert app.return_code == 0

    asyncio.run(check())
    # Only the test's explicit pause and appended event changed saved records.
    after = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert {p for p in before if before[p] != after[p]} == {
        tmp_path / "runner/run.json",
        tmp_path / "runner/history/Rome/turn_2/attempt_job2/events.jsonl",
    }
    assert set(before) == set(after)


def test_empty_and_completed_matches(tmp_path):
    async def check():
        app = match_watch.MatchWatch(tmp_path, refresh=60)
        async with app.run_test(size=(80, 24)) as pilot:
            assert app.snapshot.players == []
            write_json(
                tmp_path / "telemetry/game-result.json",
                {
                    "epoch": "g1",
                    "status": "completed",
                    "result": {"winner": "Egypt", "victoryType": "Time"},
                    "scores": [{"civilization": "Egypt", "score": 999, "controller": "BUILT_IN_AI"}],
                },
            )
            await app.poll_records()
            assert str(app.query_one("#players", DataTable).get_row("Egypt")[3]) == "999"
            assert "Winner: Egypt" in str(app.query_one("#status", Static).render())
            await pilot.press("q")

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
