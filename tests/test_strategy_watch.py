"""The operator can read completed advice while independent reviews continue."""

import asyncio

import pytest
from sqlalchemy import select
from textual.widgets import DataTable, RichLog, Static, TabbedContent

from civ_agent import memory_schema as tables
from civ_agent.civilization_state import CivilizationState
from civ_agent.match_watch import MatchWatch
from civ_agent.monitoring import MonitorStore, path_for
from civ_agent.review_store import ReviewStore
from civ_agent.strategy_view import StrategyReader
from civ_agent.turn_archive import civilization_directory_name
from civ_agent.watch_state import WatchReader
from tests.test_match_watch import fixture, put_state
from tests.test_strategic_review import assessment


@pytest.fixture
def memories(tmp_path):
    opened = []

    def create(civilization):
        memory = CivilizationState(
            tmp_path / "agent-services" / civilization_directory_name(civilization) / "civilization", civilization
        )
        memory.bind("g1")
        opened.append(memory)
        return memory

    yield create
    for memory in opened:
        memory.engine.dispose()


def prepare(memory, turn):
    job = f"turn{turn}"
    memory.start_job(job, "gameplay", {}, turn)
    store = ReviewStore(memory)
    store.prepare(job, {"turn": turn, "privateInput": "Do not display frozen inputs"}, {}, 60)
    store.confirm(job)
    store.attempt(job, 3)
    memory.finish_job(job, True)
    return job


def complete(memory, job, text):
    ReviewStore(memory).complete(job, {**assessment(), "assessment": text})


def telemetry(root, civilization, turn, phase="COMPLETED", *, epoch="g1", attempt=1):
    job = {
        "civilization": civilization,
        "purpose": "review",
        "epoch": epoch,
        "gameTurn": turn,
        "claimedAtMs": attempt,
        "telemetry": {"phase": phase, "elapsedMs": 1200} if phase else None,
        "usage": {"input_tokens": 8000, "cached_input_tokens": 4000, "output_tokens": 300},
    }
    key = f"{epoch}/{civilization}/{turn}/{attempt}"
    MonitorStore(path_for(root)).put("jobs", key, {"jobId": key, "job": job}, epoch=epoch)


def test_reader_uses_committed_reports_and_tracks_first_preload_without_writes(tmp_path, memories):
    fixture(tmp_path)
    rome, greece = memories("Rome"), memories("Greece")
    complete(rome, prepare(rome, 5), "Rome should expand.")
    complete(greece, prepare(greece, 5), "Greece should defend.")
    telemetry(tmp_path, "Rome", 5)
    telemetry(tmp_path, "Greece", 5)
    rome.start_job("later", "gameplay", {}, 7)
    paths = [path_for(tmp_path), rome.path, greece.path]
    before = {path: path.read_bytes() for path in paths}
    reader = WatchReader(tmp_path)
    view = reader.read()
    roman, greek = view.strategies
    assert not view.errors and roman.error is None and greek.error is None
    assert roman.report is not None and greek.report is not None and roman.job is not None
    assert roman.report["assessment"] == "Rome should expand." and roman.delivered_turn == 7
    assert greek.report["assessment"] == "Greece should defend." and greek.delivered_turn is None
    assert roman.status == "Completed" and roman.job["usage"]["input_tokens"] == 8000
    assert "Do not display frozen inputs" not in str(view.strategies)
    assert reader.read().strategies == view.strategies
    assert before == {path: path.read_bytes() for path in paths}
    # Opening the viewer does not construct CivilizationState or interrupt running work.
    with rome.transaction() as db:
        assert db.scalar(select(tables.jobs.c.status).where(tables.jobs.c.id == "later")) == "RUNNING"


def test_pending_review_keeps_latest_report_and_observes_telemetry_updates(tmp_path, memories):
    fixture(tmp_path)
    rome = memories("Rome")
    complete(rome, prepare(rome, 5), "Previous report")
    prepare(rome, 10)
    reader = WatchReader(tmp_path)
    telemetry(tmp_path, "Rome", 10, None)
    first = reader.read().strategies[0]
    assert first.report is not None
    assert first.report["turn"] == 5 and first.status == "Pending T10"
    telemetry(tmp_path, "Rome", 10, "RUNNING")
    running = reader.read().strategies[0]
    assert running.status == "Reviewing T10" and running.report == first.report
    ReviewStore(rome).failed("turn10", "Model timed out")
    telemetry(tmp_path, "Rome", 10, "FAILED")
    failed = reader.read().strategies[0]
    assert failed.pending is not None
    assert failed.pending["error"] == "Model timed out" and failed.pending["attempts"] == 1
    complete(rome, "turn10", "New report")
    telemetry(tmp_path, "Rome", 10)
    report = reader.read().strategies[0].report
    assert report is not None and report["assessment"] == "New report"


def test_rewind_discards_future_report_delivery_and_finished_attempt(tmp_path, memories):
    run, _ = fixture(tmp_path)
    rome = memories("Rome")
    complete(rome, prepare(rome, 5), "Retained advice")
    prepare(rome, 10)
    checkpoint = tmp_path / "checkpoint.json"
    digest = rome.backup(checkpoint)
    complete(rome, "turn10", "Discarded future advice")
    rome.start_job("turn11", "gameplay", {}, 11)
    telemetry(tmp_path, "Rome", 10)
    reader = WatchReader(tmp_path)
    assert reader.read().strategies[0].delivered_turn == 11
    rome.restore(checkpoint, digest, "g2")
    put_state(tmp_path, "run", {**run, "epoch": "g2"})
    restored = reader.read().strategies[0]
    assert restored.report is not None
    assert restored.report["assessment"] == "Retained advice"
    assert restored.status == "Pending T10" and restored.job is None and restored.delivered_turn == 10
    # Retried work remains attributed to the parent turn's original epoch.
    telemetry(tmp_path, "Rome", 10, "RUNNING", attempt=2)
    assert reader.read().strategies[0].status == "Reviewing T10"


def test_unavailable_storage_never_creates_records_or_crosses_player_and_epoch(tmp_path, memories, monkeypatch):
    reader = StrategyReader(tmp_path)
    assert reader.read(["Rome"], "g1", [])[0].error
    assert not list(tmp_path.iterdir())
    rome = memories("Rome")
    complete(rome, prepare(rome, 5), "Readable report")
    original = reader.read(["Rome"], "g1", [])[0]

    def failed(*args):
        raise OSError("Busy")

    with monkeypatch.context() as patch:
        patch.setattr(reader, "player", failed)
        last = reader.read(["Rome"], "g1", [])[0]
        assert last.error and last.report == original.report
        assert reader.read(["Rome"], "g2", [])[0].report is None
    mismatch = reader.read(["Rome"], "g2", [])[0]
    assert mismatch.error and mismatch.report is None
    with rome.transaction() as db:
        rome._set(db, "civilization", "Greece")
    assert reader.read(["Rome"], "g1", [])[0].report is None


@pytest.mark.parametrize("size", [(120, 40), (80, 24)])
def test_strategy_tab_selection_refresh_freeze_and_reading_position(tmp_path, memories, monkeypatch, size):
    monkeypatch.setenv("NO_COLOR", "1")
    fixture(tmp_path)
    rome, greece = memories("Rome"), memories("Greece")
    complete(rome, prepare(rome, 5), "[bold]Roman advice[/bold]\x1b[2J")
    prepare(greece, 5)
    telemetry(tmp_path, "Greece", 5, "RUNNING")

    async def check():
        app = MatchWatch(tmp_path, refresh=60)
        async with app.run_test(size=size) as pilot:
            await pilot.press("s")
            await pilot.pause()
            assert app.query_one(TabbedContent).active == "strategy-tab"
            table = app.query_one("#strategies", DataTable)
            log = app.query_one("#strategy", RichLog)
            assert table.row_count == 2 and log.size.height >= 4
            assert not app.query_one("#metrics", Static).display
            text = "\n".join(line.text for line in log.lines)
            assert "[bold]Roman advice[/bold]" in text and "\x1b" not in text
            assert "Short-term plan" in text and "Scientific victory" in text
            assert "Objective:" in text and "Investment:" in text and "Payoff:" in text
            assert (
                text.index("Objective:") < text.index("Investment:") < text.index("Payoff:") < text.index("Remaining:")
            )
            assert "Recommendations remain advisory" in str(app.query_one("#strategy-status", Static).render())
            log.scroll_to(y=5, animate=False)
            await pilot.pause()
            offset = log.scroll_y
            await app.poll_records()
            await pilot.pause()
            assert log.scroll_y == offset and offset > 0
            table.move_cursor(row=1)
            await pilot.press("enter")
            await pilot.pause()
            assert app.strategy_civilization == "Greece" and log.scroll_y == 0
            assert "first review is pending" in "\n".join(line.text for line in log.lines)
            await pilot.press("space")
            complete(greece, "turn5", "Greece should protect its Settler.")
            await app.poll_records()
            assert app.snapshot.strategies[1].report is None
            await pilot.press("space")
            await pilot.pause()
            assert "protect its Settler" in "\n".join(line.text for line in log.lines)
            players = app.query_one("#players", DataTable)
            players.focus()
            players.move_cursor(row=0)
            await pilot.press("enter")
            assert table.row_count == 1 and app.strategy_civilization == "Rome"
            await pilot.press("a")
            assert table.row_count == 2
            await pilot.press("q")
        assert app.return_code == 0

    asyncio.run(check())
