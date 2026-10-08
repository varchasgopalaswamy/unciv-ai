"""The operator can read completed advice while independent reviews continue."""

import asyncio
import time

import pytest
from sqlalchemy import select
from textual.widgets import DataTable, RichLog, Static, TabbedContent

from civ_agent import memory_schema as tables
from civ_agent.civilization_state import CivilizationState
from civ_agent.match_watch import MatchWatch
from civ_agent.monitoring import MonitorStore, path_for
from civ_agent.review_store import ReviewStore
from civ_agent.strategy_view import StrategyReader, StrategyView, report_text
from civ_agent.telemetry import JobAccounting
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
    store.prepare(
        job, {"turn": turn, "observation": {"turn": turn}, "privateInput": "Do not display frozen inputs"}, {}, 60
    )
    store.attempt(job, 3)
    memory.finish_job(job, True)
    return job


def complete(memory, job, text):
    ReviewStore(memory).complete(job, {**assessment(), "assessment": text})


def test_report_displays_diplomatic_initiatives_and_empty_choice():
    report = assessment()
    diplomacy = report["review"]["diplomacy"]
    initiative = diplomacy["initiatives"][0]
    initiative["players"] = ["France\x1b[2J"]
    text = report_text(StrategyView("Rome", report=report)).plain
    assert "Diplomatic initiative · France" in text and "\x1b" not in text
    assert all(
        initiative[field] in text for field in ("objective", "incentives", "approach", "nextStep", "reconsiderWhen")
    )
    diplomacy["initiatives"] = []
    text = report_text(StrategyView("Rome", report=report)).plain
    assert diplomacy["assessment"] in text and "No initiative proposed." in text


@pytest.mark.parametrize("preferred", [None, 1, 2])
def test_report_displays_all_plans_outlooks_and_reconsideration(preferred):
    report = assessment()
    review = report["review"]
    review["preferredPlan"] = preferred
    review["plans"][0]["name"] += "\x1b[2J"
    text = report_text(StrategyView("Rome", report=report)).plain
    assert "\x1b" not in text
    assert ("No preferred plan" in text) == (preferred is None)
    for number, plan in enumerate(review["plans"], 1):
        assert f"Plan {number} · " in text
        assert all(plan[field] in text for field in ("objective", "investment", "relativeOutcome", "risks"))
    assert review["reconsiderWhen"] in text
    assert review["outcomeAssessment"] in text
    for plan in review["plans"]:
        assert all(item in text for item in plan["expectedObservations"] + plan["firstActions"])
    assert text.index("Recommendation") < text.index("Investment") < text.index("Diplomacy")


def telemetry(root, civilization, turn, phase="COMPLETED", *, epoch="g1", attempt=1):
    job = {
        "civilization": civilization,
        "purpose": "review",
        "epoch": epoch,
        "gameTurn": turn,
        "claimedAtMs": attempt,
        "telemetry": {"phase": phase, "elapsedMs": 1200, "model": "test-model", "reasoningEffort": "max"}
        if phase
        else None,
        "usage": {"input_tokens": 8000, "cached_input_tokens": 4000, "output_tokens": 300},
    }
    key = f"{epoch}/{civilization}/{turn}/{attempt}"
    MonitorStore(path_for(root)).put("jobs", key, {"jobId": key, "job": job}, epoch=epoch)
    return key


def publish_progress(root, civilization, id, progress, *, epoch="g1", trace=None):
    ledger = JobAccounting(root / "agent-services" / civilization / "telemetry")
    ledger.start(id, epoch, True)
    ledger.sample({"activationId": id, "phase": "RUNNING", "reviewProgress": progress, "reviewTrace": trace})
    return ledger


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


def test_progress_uses_matching_attempt_and_epoch_without_reading_transcripts(tmp_path, memories):
    fixture(tmp_path)
    memory = memories("Rome")
    prepare(memory, 5)
    first = telemetry(tmp_path, "Rome", 5, "FAILED")
    job = telemetry(tmp_path, "Rome", 5, "RUNNING", attempt=2)
    progress = {"stage": "Drafting", "toolCalls": 8, "draftCharacters": 320, "lastActivityAtMs": 1000}
    trace = {"entries": [{"label": "Assistant", "text": "Current draft", "time": "now"}], "truncated": False}
    old = publish_progress(tmp_path, "Rome", first, {**progress, "draftCharacters": 999}, trace={"entries": []})
    ledger = publish_progress(tmp_path, "Rome", job, progress, trace=trace)
    reader = WatchReader(tmp_path)
    try:
        view = reader.read().strategies[0]
        assert view.status == "Drafting T5" and view.progress == progress and view.trace == trace
        ledger.sample(
            {
                "activationId": job,
                "phase": "RUNNING",
                "reviewProgress": {
                    **progress,
                    "modelStatus": {"state": "retrying", "message": "Connection interrupted; retrying."},
                },
            }
        )
        assert reader.read().strategies[0].status == "Retrying T5"
        # A different timeline's accounting does not leak into this report.
        alien = telemetry(tmp_path, "Rome", 5, "RUNNING", attempt=3)
        other = publish_progress(tmp_path, "Rome", alien, progress, epoch="g2", trace=trace)
        other.engine.dispose()
        wrong_epoch = reader.read().strategies[0]
        assert wrong_epoch.progress is None and wrong_epoch.trace is None
    finally:
        old.engine.dispose()
        ledger.engine.dispose()


def test_opening_report_is_visible_before_gameplay_and_delivered_on_turn_zero(tmp_path, memories):
    run, _ = fixture(tmp_path)
    put_state(tmp_path, "run", {**run, "sessions": {}})
    memory = memories("Rome")
    store = ReviewStore(memory)
    store.prepare("opening", {"turn": 0, "observation": {"turn": 0}}, {}, 60)
    telemetry(tmp_path, "Rome", 0, "RUNNING")
    reader = WatchReader(tmp_path)
    pending = reader.read().strategies[0]
    assert pending.error is None and pending.status == "Reviewing T0"
    assert pending.report is None and pending.delivered_turn is None
    complete(memory, "opening", "Explore before deciding where to expand.")
    telemetry(tmp_path, "Rome", 0)
    completed = reader.read().strategies[0]
    assert completed.report is not None and completed.report["turn"] == 0
    assert completed.status == "Completed" and completed.delivered_turn is None
    memory.start_job("first", "gameplay", {}, 0)
    assert reader.read().strategies[0].delivered_turn == 0


def test_civ_waiting_for_first_turn_is_in_strategy_roster(tmp_path, memories):
    fixture(tmp_path)
    store = MonitorStore(path_for(tmp_path))
    result = store.get("state", "result")
    assert result is not None
    result["scores"].append({"civilization": "France", "controller": "AGENT", "score": 0})
    store.put("state", "result", result)
    memory = memories("France")
    ReviewStore(memory).prepare("opening", {"turn": 0, "observation": {"turn": 0}}, {}, 60)
    telemetry(tmp_path, "France", 0, "RUNNING")
    view = WatchReader(tmp_path).read()
    assert [s.civilization for s in view.strategies] == ["Rome", "Greece", "France"]
    assert view.strategies[2].status == "Reviewing T0"


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
    job = telemetry(tmp_path, "Greece", 5, "RUNNING")
    ledger = publish_progress(
        tmp_path,
        "Greece",
        job,
        {"stage": "Drafting", "toolCalls": 8, "draftCharacters": 320, "lastActivityAtMs": int(time.time() * 1000)},
    )
    ledger.engine.dispose()

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
            assert "Expected observations" in text and "Prior outcomes" in text
            assert "Diplomatic initiative" in text and "Their incentives" in text and "Reconsider when" in text
            assert "Plan 1 · " in text and "Investment" in text and "Relative outcome" in text
            assert (
                text.index("Recommendation")
                < text.index("Investment")
                < text.index("Relative outcome")
                < text.index("Prior outcomes")
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
            assert app.query_one("#strategy-content", TabbedContent).active == "strategy-trace-tab"
            await pilot.click(app.query_one("#strategy-content", TabbedContent).get_tab("strategy-report-tab"))
            await pilot.pause()
            assert app.strategy_civilization == "Greece" and log.scroll_y == 0
            assert "first review is pending" in "\n".join(line.text for line in log.lines)
            progress = str(app.query_one("#strategy-status", Static).render())
            assert "Drafting T5" in progress and "8 tool calls" in progress and "320 report characters" in progress
            assert "last activity" in progress and "test-model · max reasoning" in progress
            assert log.size.height >= 3
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


@pytest.mark.parametrize("size", [(120, 40), (80, 24)])
def test_live_review_trace_streams_follows_and_keeps_report_separate(tmp_path, memories, size):
    fixture(tmp_path)
    memory = memories("Rome")
    complete(memory, prepare(memory, 5), "Completed advice stays readable")
    prepare(memory, 10)
    job = telemetry(tmp_path, "Rome", 10, "RUNNING")
    progress = {
        "stage": "Drafting",
        "toolCalls": 2,
        "draftCharacters": 100,
        "lastActivityAtMs": int(time.time() * 1000),
    }
    trace = {
        "entries": [
            {"label": "Reasoning summary", "text": "[bold]Compare expansion[/bold]\x1b[2J", "time": "now"},
            {"label": "Tool", "text": "research_details · completed", "time": "now"},
            {"label": "Assistant", "text": "\n".join(f"Draft line {n}" for n in range(50)), "time": "now"},
        ],
        "truncated": False,
    }
    ledger = publish_progress(tmp_path, "Rome", job, progress, trace=trace)

    def more(text):
        trace["entries"][-1]["text"] += "\n" + text
        ledger.sample({"activationId": job, "phase": "RUNNING", "reviewProgress": progress, "reviewTrace": trace})

    async def check():
        app = MatchWatch(tmp_path, refresh=60)
        async with app.run_test(size=size) as pilot:
            await pilot.press("s")
            await pilot.pause()
            tabs = app.query_one("#strategy-content", TabbedContent)
            assert tabs.active == "strategy-trace-tab"
            log = app.query_one("#strategy-trace", RichLog)
            content = "\n".join(line.text for line in log.lines)
            assert "Reasoning summary" in content and "research_details" in content
            assert "[bold]Compare expansion[/bold]" in content and "\x1b" not in content
            assert "Completed advice stays readable" not in content
            assert log.size.height >= 3 and log.is_vertical_scroll_end
            more("Streamed update")
            await app.poll_records()
            await pilot.pause()
            assert "Streamed update" in "\n".join(line.text for line in log.lines)
            assert log.is_vertical_scroll_end
            log.scroll_to(y=5, animate=False)
            await pilot.pause()
            offset = log.scroll_y
            more("Keep the reading position")
            await app.poll_records()
            await pilot.pause()
            assert log.scroll_y == offset
            await pilot.press("space")
            more("Arrived while frozen")
            await app.poll_records()
            assert "Arrived while frozen" not in "\n".join(line.text for line in log.lines)
            await pilot.press("space")
            await pilot.pause()
            assert "Arrived while frozen" in "\n".join(line.text for line in log.lines)
            await pilot.click(tabs.get_tab("strategy-report-tab"))
            await pilot.pause()
            report = app.query_one("#strategy", RichLog)
            assert report.size.width > 1
            assert "Completed advice stays readable" in "\n".join(line.text for line in report.lines)
            complete(memory, "turn10", "New completed advice")
            telemetry(tmp_path, "Rome", 10)
            await app.poll_records()
            await pilot.pause()
            assert tabs.active == "strategy-report-tab"
            assert "New completed advice" in "\n".join(line.text for line in report.lines)
            await pilot.click(tabs.get_tab("strategy-trace-tab"))
            await pilot.pause()
            assert "Streamed update" in "\n".join(line.text for line in log.lines)
            await pilot.press("q")

    try:
        asyncio.run(check())
    finally:
        ledger.engine.dispose()
