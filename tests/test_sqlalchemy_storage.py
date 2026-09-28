"""Verify current-format durability, checkpoints and SQL behavior independently."""

import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError

from civ_agent import memory_schema as tables
from civ_agent.civilization_state import CivilizationState
from civ_agent.memory_store import encode
from civ_agent.telemetry import JobAccounting

FIXTURES = Path(__file__).parent / "fixtures"
ANCESTORS = """WITH RECURSIVE reachable(key,version) AS (
    SELECT key,version FROM heads UNION
    SELECT r.key,r.parent FROM records r JOIN reachable a USING(key,version) WHERE r.parent IS NOT NULL
) """


def seed_memory(directory):
    initial = CivilizationState(directory, "Rome")
    initial.engine.dispose()
    path = initial.path
    command = {"kind": "END_TURN", "requestId": "request-old"}
    receipt = {"kind": "END_TURN", "done": True}
    decision = {"command": command, "receipt": receipt, "outcome": "confirmed", "intent": {"reason": "Hold position"}}
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DELETE FROM meta")
        db.executemany(
            "INSERT INTO meta VALUES (?,?)",
            [
                (key, encode(value))
                for key, value in {
                    "format": tables.FORMAT_VERSION,
                    "civilization": "Rome",
                    "epoch": "old-epoch",
                    "storeId": "existing-store",
                    "gameTurn": 4,
                }.items()
            ],
        )
        db.execute("INSERT INTO handles VALUES (41)")
        for version, units in [(1, ["Greek warrior"]), (2, [])]:
            db.execute(
                "INSERT INTO records VALUES (?,?,?,?,?,?)",
                (
                    "observation:border",
                    version,
                    "observation",
                    encode({"turn": 4, "units": units}),
                    version - 1 if version > 1 else None,
                    "previous",
                ),
            )
        db.execute(
            "INSERT INTO records VALUES (?,?,?,?,?,?)", ("dec1", 1, "decision", encode(decision), None, "previous")
        )
        db.executemany("INSERT INTO heads VALUES (?,?)", [("observation:border", 2), ("dec1", 1)])
        snapshot = {"epoch": "old-epoch", "purpose": "gameplay", "records": {}, "events": [], "cursor": 0}
        db.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
            ("previous", "old-epoch", "gameplay", encode(snapshot), "{}", "COMPLETED", 4),
        )
        db.execute("INSERT INTO live_jobs VALUES ('previous')")
        db.execute(
            "INSERT INTO outbox VALUES (?,?,?,?,?,?,?)",
            ("request-old", "previous", "player_command", encode(command), "{}", encode(receipt), "dec1"),
        )
        db.execute("INSERT INTO outgoing VALUES ('request-old')")
        db.execute(
            "INSERT INTO calls VALUES (?,?,?,?,?)",
            ("previous", "end", "player_command", encode(command), encode(receipt)),
        )
    return path


def independent_checkpoint(path, target):
    """Build a current-format checkpoint with independent sqlite3 queries."""
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        saved: dict = {
            "civilization": "Rome",
            "storeId": "existing-store",
            "meta": [list(row) for row in db.execute("SELECT * FROM meta ORDER BY key")],
            "heads": [list(row) for row in db.execute("SELECT * FROM heads ORDER BY key")],
            "inbox": [row[0] for row in db.execute("SELECT id FROM inbox ORDER BY id")],
            "outgoing": [row[0] for row in db.execute("SELECT id FROM outgoing ORDER BY id")],
            "jobs": [
                dict(row)
                for row in db.execute("SELECT j.id,j.status FROM jobs j JOIN live_jobs USING(id) ORDER BY j.id")
            ],
        }
        saved.update(reviews=[], service=None)
        digest = hashlib.sha256()
        for table, references, query in (
            ("records", saved["heads"], "SELECT * FROM records WHERE key=? AND version=?"),
            ("events", [(id,) for id in saved["inbox"]], "SELECT * FROM events WHERE id=?"),
            (
                "outbox",
                [(id,) for id in saved["outgoing"]],
                "SELECT o.*,j.epoch,j.purpose,j.game_turn FROM outbox o JOIN jobs j ON j.id=o.job WHERE o.id=?",
            ),
        ):
            for reference in references:
                digest.update((encode([table, dict(db.execute(query, reference).fetchone())]) + "\n").encode())
        saved["historySha256"] = digest.hexdigest()
    target.write_text(encode(saved))
    return saved, hashlib.sha256(target.read_bytes()).hexdigest()


def schema_signature(path):
    with closing(sqlite3.connect(path)) as db:
        names = [
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        ]
        return {
            name: {
                "columns": [
                    tuple(
                        value.strip("'") if index == 4 and isinstance(value, str) else value
                        for index, value in enumerate(row)
                    )
                    for row in db.execute(f'PRAGMA table_info("{name}")')
                ],
                "foreignKeys": db.execute(f'PRAGMA foreign_key_list("{name}")').fetchall(),
            }
            for name in names
        }


def test_memory_checkpoint_restart_and_revision_branch(tmp_path):
    path = seed_memory(tmp_path / "sqlite #?")
    old_schema = schema_signature(path)
    checkpoint = tmp_path / "old-checkpoint.json"
    saved, checksum = independent_checkpoint(path, checkpoint)
    memory = CivilizationState(path.parent, "Rome")
    assert schema_signature(path) == old_schema
    assert memory.verify(checkpoint, checksum) == saved
    new_checkpoint = tmp_path / "new-checkpoint.json"
    memory.backup(new_checkpoint)
    assert json.loads(new_checkpoint.read_text()) == saved
    assert memory.summary(4)["actions"][0]["action"] == "End turn"
    memory.export(tmp_path / "views")
    assert json.loads((tmp_path / "views/turn_4/summary.json").read_text())["actions"] == memory.summary(4)["actions"]

    memory.start_job("future", "gameplay", {}, game_turn=5)
    assert memory.observe("border", {"turn": 5, "units": ["ABANDONED_FUTURE"]}, "future") == 3
    change = {"op": "create", "content": {"type": "note", "text": "Do not reuse this ID"}}
    assert memory.patch("future", [change])["changes"][0]["id"] == "mem42"
    memory.finish_job("future", True)
    memory.restore(checkpoint, checksum, "new-epoch")
    memory.pause(False)
    memory.start_job("branch", "gameplay", {}, game_turn=5)
    assert memory.observe("border", {"turn": 5, "units": ["French scout"]}, "branch") == 4
    page = memory.search_memory("branch", {"id": "observation:border", "history": True})
    assert len(page["records"]) == 3
    assert "ABANDONED_FUTURE" not in encode(page)
    assert memory.patch("branch", [change])["changes"][0]["id"] == "mem43"
    with closing(sqlite3.connect(path)) as db:
        versions = db.execute(
            ANCESTORS + "SELECT version FROM reachable WHERE key='observation:border' ORDER BY version"
        ).fetchall()
    assert versions == [(1,), (2,), (4,)]
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("old_format", [2, 3, 4, 5, 6])
def test_old_memory_format_is_rejected_without_migration(tmp_path, old_format):
    path = seed_memory(tmp_path / "old-format")
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE meta SET value=? WHERE key='format'", (str(old_format),))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="fresh match"):
        CivilizationState(path.parent, "Rome")
    assert path.read_bytes() == before


def test_restart_rebuilds_missing_disposable_exports(tmp_path):
    path = seed_memory(tmp_path / "state")
    memory = CivilizationState(path.parent, "Rome")
    memory.export(tmp_path / "views")
    assert (tmp_path / "views/turn_4/receipts.jsonl").is_file()


def test_connections_keep_immediate_transactions_durability_and_rollback(tmp_path):
    memory = CivilizationState(tmp_path / "state", "Rome")
    with pytest.raises(IntegrityError):
        with memory.transaction() as db:
            assert db.exec_driver_sql("PRAGMA synchronous").scalar_one() == 2
            assert db.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            memory._set(db, "rolledBack", True)
            db.execute(insert(tables.inbox).values(id=999))
    assert db.closed
    with memory.transaction() as db:
        assert memory._meta(db, "rolledBack") is None
        memory._set(db, "serial", "old")
    attempted, acquired = threading.Event(), threading.Event()

    def second_writer():
        attempted.set()
        with memory.transaction() as other:
            acquired.set()
            assert other.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            return memory._meta(other, "serial")

    with ThreadPoolExecutor(max_workers=1) as executor:
        with memory.transaction() as db:
            memory._set(db, "serial", "new")
            result = executor.submit(second_writer)
            assert attempted.wait(1)
            assert not acquired.wait(0.1)
            with closing(sqlite3.connect(memory.path)) as reader:
                assert json.loads(reader.execute("SELECT value FROM meta WHERE key='serial'").fetchone()[0]) == "old"
        assert result.result(timeout=2) == "new"


def test_existing_accounting_recovers_spending_and_acknowledgements(tmp_path):
    path = tmp_path / "accounting.sqlite3"
    report = {
        "sequence": 3,
        "phase": "RUNNING",
        "startedAtMs": 1,
        "endedAtMs": None,
        "elapsedMs": 2,
        "negotiationWaitMs": 0,
        "usageReported": True,
        "usageComplete": False,
        "model": "gpt-5.6-luna",
        "reasoningEffort": "medium",
        "apiCalls": {},
    }
    usage = {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 30, "reasoning_output_tokens": 10}
    payload = {"epoch": "old-epoch", "claimed": True, "usage": usage, "telemetry": report}
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript((FIXTURES / "accounting_schema.sql").read_text())
        db.execute("INSERT INTO jobs VALUES (?,?,0)", ("old-job", json.dumps(payload)))
        db.execute(
            "INSERT INTO calls(id,job,keys,started) VALUES (?,?,?,?)",
            ("old-call", "old-job", '["tool:player_command"]', 1),
        )
    store = JobAccounting(tmp_path)
    pending = store.pending()[0]
    assert pending["usage"] == usage
    assert pending["telemetry"]["phase"] == "FAILED" and not pending["telemetry"]["usageComplete"]
    assert pending["telemetry"]["apiCalls"]["tool:player_command"]["incomplete"] == 1
    store.acknowledge("old-job", report["sequence"])
    assert store.pending() == [pending]
    store.acknowledge("old-job", pending["telemetry"]["sequence"])
    assert JobAccounting(tmp_path).pending() == []
