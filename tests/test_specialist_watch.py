"""Specialist tabs consume native host reports and isolated SDK histories."""

import asyncio

from textual.widgets import DataTable, RichLog, Static

from civ_agent.match_watch import MatchWatch
from civ_agent.watch_state import WatchReader
from tests.native_watch_fixtures import fixture, history, memory, persistent_files, task


def view(snapshot, civilization="Rome", role="economy"):
    return next(item for item in snapshot.specialists if item.civilization == civilization and item.role == role)


def test_specialists_keep_exact_role_usage_models_and_sdk_content_separate(tmp_path):
    fixture(tmp_path)
    for name, role in (("Rome", "economy"), ("Rome", "military"), ("Greece", "economy")):
        prepared = task(tmp_path, name, role)
        asyncio.run(history(tmp_path, prepared, f"{name} {role} output"))
    before = persistent_files(tmp_path)
    reader = WatchReader(tmp_path)
    snapshot = reader.read()
    economy, military = view(snapshot), view(snapshot, role="military")
    assert not snapshot.errors and economy.status == "Running T2"
    assert economy.job["usage"]["cached_input_tokens"] == 100
    assert [entry.text for entry in economy.activity if entry.label == "Assistant"] == ["Rome economy output"]
    assert [entry.text for entry in military.activity if entry.label == "Assistant"] == ["Rome military output"]
    assert not any("economy output" in entry.text or "military output" in entry.text for entry in snapshot.activity)
    assert reader.read() == snapshot
    assert before == persistent_files(tmp_path)


def test_economy_schedule_comes_from_current_native_turn_summary_without_synthetic_jobs(tmp_path):
    fixture(tmp_path)
    task(tmp_path, turn=3, schedule={"phase": "sleeping", "nextReviewTurn": 7, "reasons": []})
    economy = view(WatchReader(tmp_path).read())
    assert economy.job is None and economy.status == "Sleeping · review in 4 turns"
    assert economy.schedule["epoch"] == "g1"
    task(tmp_path, role="economy", turn=3, phase="COMPLETED")
    assert view(WatchReader(tmp_path).read()).job["telemetry"]["usageComplete"]


def test_wrong_notebook_owner_cannot_supply_another_player_activity(tmp_path):
    fixture(tmp_path)
    prepared = task(tmp_path, role="economy")
    asyncio.run(history(tmp_path, prepared, "Private Economy"))
    state = memory(tmp_path)
    with state.transaction() as db:
        state._set(db, "nativeGatewayOwner", {"version": 1, "game": "game", "player": "p1"})
    state.engine.dispose()
    snapshot = WatchReader(tmp_path).read()
    assert snapshot.errors and "Private Economy" not in str(snapshot)


def test_specialist_tabs_filter_freeze_and_fit_small_terminal(tmp_path):
    fixture(tmp_path)
    for name, role in (("Rome", "economy"), ("Greece", "economy"), ("Rome", "military")):
        asyncio.run(history(tmp_path, task(tmp_path, name, role), f"{name} {role} output"))

    async def run():
        app = MatchWatch(tmp_path, refresh=60)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.press("e")
            await pilot.pause()
            assert app.query_one("#economy-agents", DataTable).row_count == 2
            text = "\n".join(line.text for line in app.query_one("#economy-activity", RichLog).lines)
            assert "Rome economy output" in text and "Greece economy output" not in text
            assert "cached 100" in str(app.query_one("#economy-status", Static).render())
            await pilot.press("m")
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            assert app.query_one("#military-activity", RichLog).size.height >= 3
            assert app.snapshot.specialists

    asyncio.run(run())
