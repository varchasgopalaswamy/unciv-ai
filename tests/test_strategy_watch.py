"""Native review reports and SDK progress remain scoped, dated operator views."""

import asyncio
import json

import pytest
from sqlalchemy import insert
from textual.widgets import DataTable, RichLog, Static, TabbedContent

from civ_agent import memory_schema as tables
from civ_agent.match_watch import MatchWatch
from civ_agent.orchestration.contracts import Reference, ReviewRequest, Scope, TurnTicket, digest
from civ_agent.orchestration.job_contracts import JobTelemetry, NativeJob, host_job_id
from civ_agent.strategy_view import StrategyView, report_text
from civ_agent.watch_state import WatchReader
from tests.native_watch_fixtures import fixture, history, load, memory, persistent_files, publish, task
from tests.test_strategic_review import assessment


def review(root, civilization="Rome", turn=1, *, completed=True, text="Dated native report"):
    source, _ = load(root)
    index = ("Rome", "Greece").index(civilization)
    ticket_turn = turn + 1 if turn else 0
    role = task(root, civilization, turn=ticket_turn)
    scope = Scope(
        game="game", player=role.scope.player, epoch=source.authority.epoch, activation=f"review-{civilization}-{turn}"
    )
    request = ReviewRequest(
        scope=scope,
        selection=source.seats[index].session.get_selection("review"),
        inputs=Reference(sha256=digest("frozen private input")),
    )
    state = memory(root, civilization)
    with state.transaction() as db:
        raw = state._meta(db, f"nativeTurn:ticket-{civilization}-{ticket_turn}")
        ticket = TurnTicket.model_validate(raw["ticket"])
        state._set(
            db,
            "nativeReviewGate:" + ticket.scope.activation,
            {"request": request.model_dump(mode="json"), "state": "READY" if completed else "REVIEWING"},
        )
        db.execute(
            insert(tables.reviews).values(
                job=scope.activation,
                turn=turn,
                status="COMPLETED" if completed else "PENDING",
                attempts=1,
                inputs=json.dumps({"privateInput": "NEVER READ SNAPSHOT"}),
                settings="{}",
                timeout=60,
                advice=json.dumps({**assessment(), "assessment": text, "_evidence": "NEVER READ EVIDENCE"})
                if completed
                else None,
            )
        )
    state.engine.dispose()
    main = NativeJob(scope=ticket.scope, job=ticket.scope.activation, purpose="gameplay")
    native = NativeJob(scope=scope, job=scope.activation, purpose="review", parent=main)
    telemetry = JobTelemetry(
        sequence=1,
        phase="COMPLETED" if completed else "RUNNING",
        startedAtMs=1000,
        endedAtMs=2000 if completed else None,
        elapsedMs=1000,
        usageReported=True,
        usageComplete=completed,
        model=request.selection.model,
        provider=request.selection.provider,
        reasoningEffort=request.selection.reasoning_effort,
    )
    job = {
        "epoch": scope.epoch,
        "civilization": civilization,
        "purpose": "review",
        "gameTurn": ticket_turn,
        "claimedAtMs": 1000,
        "telemetry": telemetry.model_dump(mode="json"),
        "usage": {
            "input_tokens": 8000,
            "cached_input_tokens": 4000,
            "output_tokens": 300,
            "reasoning_output_tokens": 50,
        },
    }
    publish(
        root,
        "jobs",
        host_job_id(native),
        {"schemaVersion": 1, "matchId": "engine-game-uuid", "jobId": host_job_id(native), "job": job},
        civilization=civilization,
        epoch=scope.epoch,
    )
    return request


@pytest.mark.parametrize("preferred", [None, 1, 2])
def test_report_displays_native_plans_outlooks_and_nonbinding_initiatives(preferred):
    report = assessment()
    report["review"]["preferredPlan"] = preferred
    report["review"]["plans"][0]["name"] += "\x1b[2J"
    text = report_text(StrategyView("Rome", report=report)).plain
    assert "\x1b" not in text and ("No preferred plan" in text) == (preferred is None)
    assert all(plan["objective"] in text for plan in report["review"]["plans"])
    assert report["review"]["diplomacy"]["initiatives"][0]["nextStep"] in text


def test_completed_reports_and_exact_host_usage_without_snapshot_or_evidence_reads(tmp_path):
    fixture(tmp_path)
    review(tmp_path)
    review(tmp_path, "Greece", text="Greek native report")
    before = persistent_files(tmp_path)
    reader = WatchReader(tmp_path)
    view = reader.read()
    assert not view.errors
    roman, greek = view.strategies
    assert roman.report is not None and greek.report is not None and roman.job is not None
    assert roman.report["assessment"] == "Dated native report" and greek.report["assessment"] == "Greek native report"
    assert roman.job["usage"]["cached_input_tokens"] == 4000
    assert roman.delivered_turn is None  # Existence is not a delivered-read receipt.
    assert "NEVER READ" not in str(view.strategies)
    assert reader.read() == view
    assert before == persistent_files(tmp_path)


def test_pending_review_preserves_dated_report_and_uses_actual_sdk_trace(tmp_path):
    fixture(tmp_path)
    review(tmp_path, turn=0, text="Previous report")
    request = review(tmp_path, turn=1, completed=False)
    asyncio.run(history(tmp_path, request, "New native comparison"))
    view = WatchReader(tmp_path).read().strategies[0]
    assert view.report is not None and view.progress is not None
    assert view.report["assessment"] == "Previous report" and view.status == "Reviewing T1"
    assert "New native comparison" in str(view.trace)
    assert view.progress["draftCharacters"] == len("New native comparison")
    assert "NEVER READ" not in str(view)


def test_native_strategy_tab_keeps_report_and_committed_sdk_trace_separate(tmp_path):
    fixture(tmp_path)
    request = review(tmp_path)
    asyncio.run(history(tmp_path, request, "Review SDK output"))

    async def run():
        app = MatchWatch(tmp_path, refresh=60)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("s")
            await pilot.pause()
            assert app.query_one("#strategies", DataTable).row_count == 2
            assert "Dated native report" in "\n".join(line.text for line in app.query_one("#strategy", RichLog).lines)
            await pilot.click(app.query_one("#strategy-content", TabbedContent).get_tab("strategy-trace-tab"))
            await pilot.pause()
            assert "Review SDK output" in "\n".join(
                line.text for line in app.query_one("#strategy-trace", RichLog).lines
            )
            assert "Dated native report" not in "\n".join(
                line.text for line in app.query_one("#strategy-trace", RichLog).lines
            )
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            assert app.query_one("#strategy-trace", RichLog).size.height >= 3
            assert "Completed" in str(app.query_one("#strategy-status", Static).render())

    asyncio.run(run())
