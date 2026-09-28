import json
from pathlib import Path

import pytest
from sqlalchemy import select

from civ_agent import memory_schema as tables
from civ_agent.civilization_state import CivilizationState
from civ_agent.commands import history as exporter
from civ_agent.monitoring import MonitorStore, path_for, read_database
from civ_agent.runner_history import EventHistory
from tests.test_structured_memory import create


def test_explicit_export_preserves_sanitized_history_and_leaves_database_unchanged(tmp_path):
    log = EventHistory(path_for(tmp_path), "g1", "Greece", "job", ("secret",), game_turn=2)
    log.append(
        [
            {"sequence": 0, "kind": "assistant_delta", "data": {"itemId": "a", "text": "Build "}},
            {
                "sequence": 1,
                "kind": "assistant_message",
                "data": {"itemId": "a", "text": "Build a farm. secret", "raw_reasoning": "hidden"},
            },
            {"sequence": 2, "kind": "reasoning_summary", "data": {"text": "Checking city actions."}},
        ]
    )
    original = path_for(tmp_path).read_bytes()
    one = exporter.export_history(tmp_path, tmp_path / "exports", "Greece")
    two = exporter.export_history(tmp_path, tmp_path / "exports")
    assert one[0]["history"] != two[0]["history"]
    text = Path(one[0]["history"]).read_text()
    assert text.count("Build a farm.") == 1 and "Checking city actions." in text
    assert "secret" not in text and "hidden" not in text
    assert Path(one[0]["events"]).stat().st_mode & 0o777 == 0o600
    assert path_for(tmp_path).read_bytes() == original
    with pytest.raises(exporter.ExportError):
        exporter.export_history(tmp_path, tmp_path / "exports", "Rome")


def test_diplomacy_export_includes_both_sides_of_selected_players_conversations(tmp_path):
    store = MonitorStore(path_for(tmp_path))
    for name in ("Rome", "Greece"):
        EventHistory(path_for(tmp_path), "g1", name, "job", game_turn=2)
    for key, participants in (
        ("rome-greece", ["Rome", "Greece"]),
        ("greece-france", ["Greece", "France"]),
        ("rome-egypt", ["Rome", "Egypt"]),
    ):
        store.put("diplomacy", key, {"id": key, "participants": participants}, epoch="g1", game_turn=2)
    before = path_for(tmp_path).read_bytes()
    for player, expected in (
        ("Rome", {"rome-greece", "rome-egypt"}),
        ("Greece", {"rome-greece", "greece-france"}),
        (None, {"rome-greece", "greece-france", "rome-egypt"}),
    ):
        output = tmp_path / (player or "all")
        exporter.export_history(tmp_path, output, player)
        report = next(output.glob("export-*/diplomacy.jsonl"))
        assert {json.loads(line)["id"] for line in report.read_text().splitlines()} == expected
        assert report.stat().st_mode & 0o777 == 0o600
    assert path_for(tmp_path).read_bytes() == before


def test_notebook_export_reads_active_jobs_without_changing_the_service(tmp_path):
    memory = CivilizationState(tmp_path / "agent-services/Rome/civilization", "Rome")
    memory.bind("g1")
    memory.start_job("game", "gameplay", {}, game_turn=2)
    create(memory, {"type": "note", "text": "Defend the southern city"})
    EventHistory(path_for(tmp_path), "g1", "Rome", "game", game_turn=2)
    database = memory.root / "memory.sqlite3"
    before = database.read_bytes()
    entries = exporter.export_history(tmp_path, tmp_path / "exports", notebooks=True)
    report = next((tmp_path / "exports").glob("export-*/notebooks/Rome/turn_2/summary.json"))
    summary = json.loads(report.read_text())
    assert any(row["text"] == "Defend the southern city" for row in summary["memoryChanges"])
    assert len(entries) == 1 and database.read_bytes() == before
    with read_database(database) as db:
        assert db.scalar(select(tables.jobs.c.status).where(tables.jobs.c.id == "game")) == "RUNNING"
