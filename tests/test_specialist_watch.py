"""Specialist tabs read the roles' existing operator records without starting agents."""

import asyncio
import hashlib
import json

from textual.widgets import DataTable, RichLog, Static, TabbedContent

from civ_agent import watch_state
from civ_agent.agent_history import HistoryStore
from civ_agent.match_watch import MatchWatch
from civ_agent.monitoring import MonitorStore, path_for
from civ_agent.tls import write_private_json
from civ_agent.turn_archive import civilization_directory_name
from civ_agent.watch_state import WatchReader
from tests.test_match_watch import fixture, put_state


def specialist(root, civilization="Rome", role="economy", turn=2, *, epoch="g1", attempt=1, phase="RUNNING"):
    activation = f"{epoch}/{civilization}/{turn}/{role}/{attempt}"
    base = root / "agent-services" / civilization_directory_name(civilization)
    record = {
        "activationId": activation,
        "civilization": civilization,
        "purpose": role,
        "epoch": epoch,
        "gameTurn": turn,
        "phase": phase,
        "settings": {"provider": "openai", "model": f"{role}-model", "reasoningEffort": "high"},
        "usage": {"input_tokens": 200, "cached_input_tokens": 100, "output_tokens": 30, "reasoning_output_tokens": 10},
    }
    record_path = base / "activations" / (hashlib.sha256(activation.encode()).hexdigest() + ".json")
    write_private_json(record_path, record)
    history = HistoryStore(base / "history", "service-secret")
    history.begin(record, None, "player-secret")
    history.startup_context(activation, "Repeated specialist startup")
    log = history.logs[activation]
    log.append("assistant_delta", {"itemId": "message", "text": f"{civilization} {role} "})
    log.append("assistant_message", {"itemId": "message", "text": f"{civilization} {role} output player-secret"})
    job = {
        "epoch": epoch,
        "civilization": civilization,
        "purpose": role,
        "gameTurn": turn,
        "claimedAtMs": turn * 100 + attempt,
        "telemetry": {"phase": phase, "elapsedMs": 1000, "usageComplete": phase == "COMPLETED", **record["settings"]},
        "usage": record["usage"],
    }
    MonitorStore(path_for(root)).put("jobs", activation, {"jobId": activation, "job": job}, epoch=epoch)
    return record, record_path, log


def view(snapshot, civilization="Rome", role="economy"):
    return next(v for v in snapshot.specialists if v.civilization == civilization and v.role == role)


def log_text(app, role):
    return "\n".join(line.text for line in app.query_one(f"#{role}-activity", RichLog).lines)


def test_specialists_read_separate_jobs_models_and_redacted_history_without_writes(tmp_path):
    fixture(tmp_path)
    _, _, log = specialist(tmp_path)
    specialist(tmp_path, role="military")
    specialist(tmp_path, "Greece")
    # Use the exact same model item ID in every role to exercise independent decoders.
    log.append("action", {"itemId": "buy", "description": "Purchased a Library"})
    log.append("action", {"itemId": "buy", "description": "Duplicate purchase"})
    paths = [path for path in tmp_path.rglob("*") if path.is_file()]
    before = {path: path.read_bytes() for path in paths}
    reader = WatchReader(tmp_path)
    snapshot = reader.read()
    economy, military = view(snapshot), view(snapshot, role="military")
    assert economy.job is not None and military.job is not None
    assert economy.job["telemetry"]["model"] == "economy-model"
    assert military.job["telemetry"]["model"] == "military-model"
    assert economy.status == "Running T2" and economy.error is None
    assert [e.text for e in economy.activity if e.label == "Assistant"] == ["Rome economy output [REDACTED]"]
    assert [e.text for e in military.activity if e.label == "Assistant"] == ["Rome military output [REDACTED]"]
    assert [e.text for e in economy.activity if e.label == "Action"] == ["Purchased a Library"]
    assert all("Greece" not in e.text and "military" not in e.text for e in economy.activity)
    assert not any("economy output" in e.text or "military output" in e.text for e in snapshot.activity)
    assert reader.read().specialists == snapshot.specialists
    assert before == {path: path.read_bytes() for path in paths}


def test_economy_schedule_shows_sleeping_due_turn_and_wake_reasons(tmp_path):
    fixture(tmp_path)
    specialist(tmp_path, phase="COMPLETED")
    path = tmp_path / "agent-services/Rome/economy-status.json"
    write_private_json(
        path,
        {"epoch": "g1", "gameTurn": 3, "phase": "sleeping", "lastReviewTurn": 2, "nextReviewTurn": 7, "reasons": []},
    )
    reader = WatchReader(tmp_path)
    economy = view(reader.read())
    assert economy.status == "Sleeping · review in 4 turns"
    assert economy.job["gameTurn"] == 2  # Real usage/history is retained; no synthetic model job.
    write_private_json(
        path,
        {"epoch": "g1", "gameTurn": 3, "phase": "running", "nextReviewTurn": 7, "reasons": ["Population grew: Rome"]},
    )
    economy = view(reader.read())
    assert economy.status == "Starting T3"
    assert economy.schedule["reasons"] == ["Population grew: Rome"]
    # A rewind or an obsolete local file must never label another timeline.
    write_private_json(path, {"epoch": "abandoned", "gameTurn": 8, "phase": "sleeping", "nextReviewTurn": 9})
    assert view(reader.read()).schedule is None


def test_economy_without_a_model_job_can_sleep_until_first_city(tmp_path):
    fixture(tmp_path)
    write_private_json(
        tmp_path / "agent-services/Rome/economy-status.json",
        {"epoch": "g1", "gameTurn": 0, "phase": "sleeping", "nextReviewTurn": None, "reasons": []},
    )
    economy = view(WatchReader(tmp_path).read())
    assert economy.job is None
    assert economy.status == "Sleeping · waiting for first city"


def test_specialist_updates_retries_and_partial_appends_keep_stream_boundaries(tmp_path):
    fixture(tmp_path)
    record, path, log = specialist(tmp_path)
    reader = WatchReader(tmp_path)
    original = view(reader.read())
    record.update(roleState="finished")
    write_private_json(path, record)
    assert view(reader.read()).status == "Role finished T2"
    record.update(roleState="waiting_negotiation")
    write_private_json(path, record)
    assert view(reader.read()).status == "Waiting negotiation T2"
    # An unfinished JSONL append must be retried, without dropping or repeating events.
    sequence = log.sequence
    event = {
        "sequence": sequence,
        "time": "",
        "kind": "assistant_message",
        "data": {"itemId": "new", "text": "New allocation"},
    }
    raw = json.dumps(event).encode()
    with log.path.open("ab") as stream:
        stream.write(raw[:20])
    assert view(reader.read()).activity == original.activity
    with log.path.open("ab") as stream:
        stream.write(raw[20:] + b"\n")
    updated = view(reader.read())
    assert updated.activity[-1].text == "New allocation"
    assert sum(e.text == "New allocation" for e in updated.activity) == 1
    # A malformed committed event retains the last readable display for this job.
    with log.path.open("ab") as stream:
        stream.write(json.dumps({"sequence": sequence + 1, "kind": "assistant_message", "data": None}).encode() + b"\n")
    malformed = view(reader.read())
    assert malformed.error and malformed.activity == updated.activity
    record.update(epoch="wrong-timeline")
    write_private_json(path, record)
    failed = view(reader.read())
    assert failed.error and failed.activity == updated.activity
    # A new attempt has its own history, even when item IDs are reused.
    specialist(tmp_path, attempt=2, phase="COMPLETED")
    retried = view(reader.read())
    assert retried.status == "Completed T2" and retried.error is None
    assert all(e.text != "New allocation" for e in retried.activity)


def test_specialists_clear_old_timeline_and_report_unavailable_local_history(tmp_path):
    run, _ = fixture(tmp_path)
    specialist(tmp_path)
    specialist(tmp_path, role="military")
    reader = WatchReader(tmp_path)
    assert view(reader.read()).activity
    put_state(tmp_path, "run", {**run, "epoch": "g2"})
    restored = reader.read()
    assert all(v.job is None and not v.activity for v in restored.specialists)
    record, path, log = specialist(tmp_path, epoch="g2")
    path.unlink()
    log.path.unlink()
    remote = view(reader.read())
    assert remote.job is not None and remote.job["telemetry"]["model"] == "economy-model"
    assert remote.error and not remote.activity
    assert view(reader.read(), role="military").status == "No job yet"


def test_specialist_history_batches_decode_chunked_events_and_recover_after_truncation(tmp_path, monkeypatch):
    fixture(tmp_path)
    _, _, log = specialist(tmp_path)
    log.append("assistant_message", {"itemId": "long", "text": "Long output " * 7000})
    log.append("assistant_message", {"itemId": "final", "text": "Last decision"})
    monkeypatch.setattr(watch_state, "MAX_EVENT_BATCH", 2)
    reader = WatchReader(tmp_path)
    first = view(reader.read())
    assert len(first.activity) == 2
    latest = first
    for _ in range(log.sequence // 2 + 1):
        latest = view(reader.read())
    assert latest.activity[-1].text == "Last decision"
    assert sum(e.text.startswith("Long output") for e in latest.activity) == 1
    assert all(len(e.text) <= watch_state.MAX_MESSAGE_CHARS + 100 for e in latest.activity)
    # A service may recover a crashed final append. Rebuild this stream's decoder
    # when its file shrinks instead of retaining text from discarded appends.
    log.path.write_bytes(b"")
    empty = view(reader.read())
    assert not empty.activity and not empty.error
    replacement = {
        "sequence": 0,
        "time": "",
        "kind": "assistant_message",
        "data": {"itemId": "replacement", "text": "Recovered output"},
    }
    log.path.write_text(json.dumps(replacement) + "\n")
    assert view(reader.read()).activity[-1].text == "Recovered output"


def test_specialist_tabs_reset_selection_and_history_when_switching_batch_games(tmp_path):
    first, second = tmp_path / "games/first", tmp_path / "games/second"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    fixture(first)
    fixture(second)
    specialist(first)
    _, _, second_log = specialist(second)
    second_log.append("assistant_message", {"itemId": "different-game", "text": "Second game economy"})
    (tmp_path / "batch.json").write_text(json.dumps({"format": 2, "runs": [{"id": "first"}, {"id": "second"}]}))

    async def check():
        app = MatchWatch(tmp_path, refresh=60)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.press("e")
            await pilot.pause()
            assert "Second game economy" not in log_text(app, "economy")
            await pilot.press("right_square_bracket")
            await pilot.pause()
            assert "Second game economy" in log_text(app, "economy")
            await pilot.press("left_square_bracket")
            await pilot.pause()
            assert "Second game economy" not in log_text(app, "economy")

    asyncio.run(check())


def test_specialist_tabs_filter_refresh_freeze_follow_and_fit_small_terminals(tmp_path):
    fixture(tmp_path)
    record, path, log = specialist(tmp_path)
    specialist(tmp_path, "Greece")
    specialist(tmp_path, role="military")
    for index in range(50):
        log.append("assistant_message", {"itemId": f"line{index}", "text": f"Allocation {index}"})

    async def check():
        app = MatchWatch(tmp_path, refresh=60)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("e")
            await pilot.pause()
            assert app.query_one("#main-tabs", TabbedContent).active == "economy-tab"
            table = app.query_one("#economy-agents", DataTable)
            assert table.row_count == 2
            assert str(table.get_row("Rome")[0]) == "Rome · economy-model"
            assert "Input 200 · cached 100" in str(app.query_one("#economy-status", Static).render())
            assert "Rome economy output" in log_text(app, "economy")
            assert "Repeated specialist startup" not in log_text(app, "economy")
            assert not app.query_one("#metrics", Static).display
            activity = app.query_one("#economy-activity", RichLog)
            assert activity.size.height >= 4 and activity.is_vertical_scroll_end
            activity.scroll_home(animate=False)
            await pilot.pause()
            log.append("assistant_message", {"itemId": "next", "text": "New economic decision"})
            await app.poll_records()
            await pilot.pause()
            assert activity.scroll_y == 0
            await pilot.press("f", "f")
            await pilot.pause()
            assert activity.is_vertical_scroll_end
            await pilot.press("space")
            log.append("assistant_message", {"itemId": "frozen", "text": "Frozen economic decision"})
            await app.poll_records()
            assert "Frozen economic decision" not in log_text(app, "economy")
            await pilot.press("space", "v")
            await pilot.pause()
            assert "Frozen economic decision" in log_text(app, "economy")
            assert "Repeated specialist startup" in log_text(app, "economy")
            table.focus()
            table.move_cursor(row=1)
            await pilot.press("enter")
            await pilot.pause()
            assert "Greece economy output" in log_text(app, "economy")
            assert "Rome economy output" not in log_text(app, "economy")
            players = app.query_one("#players", DataTable)
            players.focus()
            players.move_cursor(row=0)
            await pilot.press("enter", "m")
            await pilot.pause()
            assert app.query_one("#military-agents", DataTable).row_count == 1
            assert "Rome military output" in log_text(app, "military")
            record.update(modelStatus={"state": "retrying", "message": "Connection interrupted; retrying."})
            write_private_json(path, record)
            await app.poll_records()
            await pilot.press("e")
            await pilot.pause()
            assert "retrying" in str(app.query_one("#economy-status", Static).render())
            assert str(table.get_row("Rome")[1]) == "Model retrying T2"
            await pilot.press("a", "m")
            await pilot.pause()
            assert app.query_one("#military-agents", DataTable).row_count == 2
            await pilot.press("q")

    asyncio.run(check())
