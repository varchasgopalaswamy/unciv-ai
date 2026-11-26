import os
from pathlib import Path

import pytest

from civ_agent.orchestration.contracts import Scope
from civ_agent.orchestration.store import JournalConflict, WorkflowStore


@pytest.fixture
def store(tmp_path):
    journal = WorkflowStore(tmp_path / "player", player="p0")
    journal.bind_epoch("game", "e1")
    yield journal
    journal.close()


@pytest.fixture
def scope():
    return Scope(game="game", player="p0", epoch="e1", activation="a1")


def test_private_artifact_is_bound_to_player_activation_and_epoch(store, scope):
    reference = store.put(scope, {"private": "data"})
    assert store.get(scope, reference) == {"private": "data"}
    other = scope.model_copy(update={"activation": "a2"})
    with pytest.raises(JournalConflict, match="ARTIFACT_MISSING"):
        store.get(other, reference)
    with pytest.raises(JournalConflict, match="PLAYER_CONFLICT"):
        store.get(scope.model_copy(update={"player": "p1"}), reference)
    store.bind_epoch("game", "e2", previous="e1")
    with pytest.raises(JournalConflict, match="EPOCH_CONFLICT"):
        store.get(scope, reference)


def test_uncertain_reservation_does_not_authorize_a_second_effect(store, scope):
    request = {"command": "found_city"}
    assert store.reserve(scope, "call1", request) == (True, None)
    assert store.reserve(scope, "call1", request) == (False, None)
    with pytest.raises(JournalConflict, match="REQUEST_CHANGED"):
        store.reserve(scope, "call1", {"command": "move"})
    receipt = store.complete(scope, "call1", request, {"city": "c1"})
    assert store.reserve(scope, "call1", request) == (False, receipt)
    assert store.complete(scope, "call1", request, {"city": "c1"}) == receipt
    with pytest.raises(JournalConflict, match="RECEIPT_CHANGED"):
        store.complete(scope, "call1", request, {"city": "c2"})


def test_receipt_and_notification_commit_together_and_survive_reopen(store, scope):
    store.reserve(scope, "command", {})
    store.complete(scope, "command", {}, {"applied": True})
    assert store.engine.url.database is not None
    reopened = WorkflowStore(Path(store.engine.url.database).parent, player="p0")
    try:
        events = reopened.pending_events(scope)
        assert len(events) == 1
        receipt = reopened.reserve(scope, "command", {})[1]
        assert receipt is not None
        assert reopened.get(scope, receipt) == {"applied": True}
        reopened.acknowledge(scope, events[0]["id"])
        assert store.pending_events(scope) == []
    finally:
        reopened.close()


def test_outbox_rolls_back_with_domain_transaction(store, scope):
    with pytest.raises(RuntimeError):
        with store.transaction(scope) as db:
            store.enqueue(db, scope, "received", {"event": "message"})
            raise RuntimeError("domain commit failed")
    assert store.pending_events(scope) == []
    with store.transaction(scope) as db:
        store.enqueue(db, scope, "received", {"event": "message"})
    with store.transaction(scope) as db:
        store.enqueue(db, scope, "received", {"event": "message"})
    assert len(store.pending_events(scope)) == 1
    with pytest.raises(JournalConflict, match="EVENT_CHANGED"):
        with store.transaction(scope) as db:
            store.enqueue(db, scope, "received", {"event": "other"})


def test_database_is_private_under_permissive_umask(tmp_path):
    previous = os.umask(0)
    try:
        store = WorkflowStore(tmp_path / "private", player="p0")
        store.bind_epoch("g", "e")
        assert (tmp_path / "private" / "workflow.sqlite3").stat().st_mode & 0o777 == 0o600
        store.close()
    finally:
        os.umask(previous)


def test_player_store_cannot_be_reopened_for_another_player(store):
    assert store.engine.url.database is not None
    with pytest.raises(JournalConflict, match="STORE_INCOMPATIBLE"):
        WorkflowStore(Path(store.engine.url.database).parent, player="p1")


def test_generation_allowance_is_reserved_before_io_and_never_refunded(store, scope):
    store.reserve(scope, "generation1", {}, generation_limit=2)
    assert store.reserve(scope, "generation1", {}, generation_limit=2) == (False, None)
    store.reserve(scope, "generation2", {}, generation_limit=2)
    with pytest.raises(JournalConflict, match="MODEL_REQUEST_LIMIT"):
        store.reserve(scope, "generation3", {}, generation_limit=2)
