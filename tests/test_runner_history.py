"""Transactional history identity, restart cursors and operator privacy."""

import contextlib
import io
import json

import pytest

from civ_agent.monitoring import MonitorStore, get_record, read_database, records
from civ_agent.runner_history import EventHistory, HistoryError
from civ_agent.tls import TLSConfigError


def history(path, **changes):
    options = dict(epoch="match", civilization="Rome", activation="job", game_turn=0)
    return EventHistory(path, **{**options, **changes})


def test_distinct_players_epochs_and_attempts_preserve_history(tmp_path):
    path = tmp_path / "monitoring.sqlite3"
    first = history(path)
    first.append([dict(sequence=0, kind="assistant_message", data=dict(text="Settled Rome."))])
    for changes in (dict(activation="retry"), dict(civilization="Greece"), dict(epoch="restored")):
        assert history(path, **changes).cursor == 0
    with pytest.raises(HistoryError):
        history(path, game_turn=1)  # Same activation cannot silently change its turn.
    assert history(path).events == first.events
    assert not list(tmp_path.rglob("*.md")) and not list(tmp_path.rglob("*.jsonl"))


def test_restart_deduplicates_and_a_gap_cannot_commit_half_a_page(tmp_path):
    path = tmp_path / "monitoring.sqlite3"
    first = history(path)
    event = dict(sequence=0, kind="assistant_message", data=dict(itemId="m", text="Settled Rome."))
    first.append([event])
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        resumed = history(path)
        resumed.append([event])
    assert output.getvalue() == "" and resumed.cursor == 1
    with pytest.raises(HistoryError):
        resumed.append([{**event, "sequence": 1}, {**event, "sequence": 3}])
    assert history(path).cursor == 1
    assert resumed.markdown().count("Settled Rome.") == 1


def test_unusual_names_are_values_and_do_not_create_paths(tmp_path):
    path = tmp_path / "monitoring.sqlite3"
    unusual = history(path, civilization="../Rome", activation="../../job")
    literal = history(path, civilization="%2E%2E%2FRome", activation="../../job")
    assert unusual.key != literal.key
    assert {p.name for p in tmp_path.iterdir()} <= {
        "monitoring.sqlite3",
        "monitoring.sqlite3-wal",
        "monitoring.sqlite3-shm",
    }


def test_sanitized_events_are_durable_and_permissions_are_private(tmp_path):
    path = tmp_path / "monitoring.sqlite3"
    log = history(path, secrets=("player-token",))
    log.append(
        [dict(sequence=0, kind="tool_result", data=dict(receipt="accepted player-token", raw_reasoning="hidden"))]
    )
    text = json.dumps(history(path).events)
    assert "accepted" in text and "[REDACTED]" in text
    assert "player-token" not in text and "hidden" not in text
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("value", [None, -1, True, 0.5, "0"])
def test_invalid_turn_does_not_create_a_database(tmp_path, value):
    path = tmp_path / "monitoring.sqlite3"
    with pytest.raises(HistoryError):
        history(path, game_turn=value)
    assert not path.exists()


def test_database_symlink_is_rejected(tmp_path):
    target = tmp_path / "private"
    target.write_text("DO NOT CHANGE")
    target.chmod(0o600)
    link = tmp_path / "monitoring.sqlite3"
    link.symlink_to(target)
    with pytest.raises(TLSConfigError):
        history(link)
    assert target.read_text() == "DO NOT CHANGE"


def test_reader_uses_committed_snapshot_without_blocking_writer(tmp_path):
    path = tmp_path / "monitoring.sqlite3"
    store = MonitorStore(path)
    store.put("state", "run", {"status": "running"})
    with read_database(path) as reader:
        assert get_record(reader, "state", "run") == {"status": "running"}
        store.put("state", "run", {"status": "paused"})
        assert get_record(reader, "state", "run") == {"status": "running"}
    assert store.get("state", "run") == {"status": "paused"}
    from sqlalchemy.exc import OperationalError

    with read_database(path) as reader, pytest.raises(OperationalError):
        reader.execute(records.delete())
