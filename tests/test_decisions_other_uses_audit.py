import sqlite3
from pathlib import Path

import pytest

from scripts.audit_decisions_other_uses import request_summary, snapshot


def source_database(path, value):
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE audit_value (value TEXT)")
        db.execute("INSERT INTO audit_value VALUES (?)", (value,))


def test_snapshot_refuses_stale_preserved_wal_without_opening_or_changing_it(tmp_path):
    source, old = tmp_path / "current.sqlite3", tmp_path / "old.sqlite3"
    source_database(source, "current source")
    preserved = tmp_path / "preserved.sqlite3"
    with sqlite3.connect(old) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE audit_value (value TEXT)")
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.execute("INSERT INTO audit_value VALUES ('stale old WAL')")
        writer.commit()
        stale_wal = old.with_name(old.name + "-wal").read_bytes()
        assert stale_wal
        preserved.write_bytes(old.read_bytes())
        sidecar = preserved.with_name(preserved.name + "-wal")
        sidecar.write_bytes(stale_wal)
        before = {path: path.read_bytes() for path in (source, preserved, sidecar)}
        with pytest.raises(ValueError, match="fresh audit output directory"):
            with snapshot(source, preserve=preserved):
                pytest.fail("An existing preserved database and WAL must be rejected")
        assert {path: path.read_bytes() for path in before} == before
        assert not preserved.with_name(preserved.name + "-shm").exists()


def test_orphan_preserved_wal_is_rejected_before_creating_database(tmp_path):
    source, preserved = tmp_path / "current.sqlite3", tmp_path / "preserved.sqlite3"
    source_database(source, "current source")
    preserved.with_name(preserved.name + "-wal").write_bytes(b"old unrelated WAL")
    with pytest.raises(ValueError, match="already exists"):
        with snapshot(source, preserve=preserved):
            pytest.fail("Orphan sidecars must be rejected too")
    assert not preserved.exists()


def test_sqlite_reads_fresh_temporary_snapshot_and_preserves_only_captured_files(tmp_path):
    source, preserved = tmp_path / "current.sqlite3", tmp_path / "preserved.sqlite3"
    source_database(source, "current source")
    original = source.read_bytes()
    with snapshot(source, preserve=preserved) as (db, metadata):
        assert db.execute("SELECT value FROM audit_value").fetchone()[0] == "current source"
        opened = Path(db.execute("PRAGMA database_list").fetchone()[2])
        assert opened != preserved and opened.parent != preserved.parent
        assert metadata["artifact"] == str(source)
    assert preserved.read_bytes() == original == source.read_bytes()
    assert not preserved.with_name(preserved.name + "-wal").exists()
    assert not preserved.with_name(preserved.name + "-shm").exists()


def test_current_exact_question_denominators_and_raw_rejections_remain_separate_from_historical_bounds():
    weak = {"name": "retrieval_include:0", "type": "choice", "choice": "include", "confidence": 0.2}
    accepted = {"name": "retrieval_priority:0", "type": "choice", "choice": "normal", "confidence": 0.99}
    rows = [
        {
            "fallback": "low_confidence",
            "answers": [accepted],
            "rawAnswers": [weak, accepted],
            "questionNames": ["retrieval_include:0", "retrieval_priority:0"],
            "questionResults": [
                {"name": "retrieval_include:0", "status": "low_confidence"},
                {"name": "retrieval_priority:0", "status": "accepted"},
            ],
        },
        {
            "fallback": None,
            "answers": [{"name": "preservation:0", "type": "choice", "choice": "consider", "confidence": 1}],
        },
    ]
    summary = request_summary(rows)
    assert summary["acceptedByTask"]["retrieval"]["answers"] == 1
    assert summary["exactSubmittedQuestions"] == 2 and summary["minimumSubmittedQuestions"] == 3
    assert summary["requestsWithExactQuestionNames"] == summary["requestsWithoutExactQuestionNames"] == 1
    assert summary["knownRequestsWithAllAcceptedAnswers"] == 0 and summary["knownRequestsWithPartialAnswers"] == 1
    diagnostics = summary["questionDiagnostics"]
    assert diagnostics["statuses"] == {"low_confidence": 1, "accepted": 1}
    assert diagnostics["byTask"]["retrieval"]["submittedQuestions"] == 2
    assert diagnostics["byQuestionType"]["retrieval_include"]["retainedRawConfidence"]["median"] == 0.2
    assert diagnostics["byQuestionType"]["preservation"]["submittedQuestions"] == 0


def test_retained_names_without_status_are_unreported_not_an_invented_refusal():
    summary = request_summary([{"fallback": "timeout", "answers": [], "questionNames": ["early_review"]}])
    assert summary["exactSubmittedQuestions"] == 1
    assert summary["questionDiagnostics"]["statuses"] == {"unreported": 1}
