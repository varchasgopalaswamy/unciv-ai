"""A rewind carries notification disposition, never old actor ownership or speech."""

import hashlib
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, update

from civ_agent import memory_schema as domain
from civ_agent.civilization_state import CivilizationState, StateConflict
from civ_agent.orchestration.communication_store import CommunicationStore, communication_scope, notifications
from civ_agent.orchestration.contracts import EventNotice, canonical
from civ_agent.orchestration.store import JournalConflict, WorkflowStore, artifact_scopes


def event(identifier, *, wake=True, kind="message", text="A stated intention, not an engine fact"):
    return {
        "id": identifier,
        "kind": kind,
        "wake": wake,
        "body": {
            "message": f"m{identifier}",
            "author": "Greece",
            "participants": ["Rome", "Greece"],
            "public": False,
            "text": text,
            "mode": "NATIVE_AI" if kind == "trade" else "HUMAN",
            "turn": 0,
        },
    }


@pytest.fixture
def native(tmp_path):
    memory = CivilizationState(tmp_path / "domain", "Rome")
    memory.bind("e1")
    with memory.transaction() as db:
        memory._set(db, "nativeGatewayOwner", {"version": 1, "game": "game", "player": "p0"})
    artifacts = WorkflowStore(tmp_path / "artifacts", player="p0")
    artifacts.bind_epoch("game", "e1")
    store = CommunicationStore(memory, artifacts, communication_scope("game", "p0", "e1"))
    try:
        yield SimpleNamespace(memory=memory, artifacts=artifacts, store=store, path=tmp_path / "checkpoint.json")
    finally:
        memory.engine.dispose()
        artifacts.close()


def captured(native):
    memory, store = native.memory, native.store
    store.ingest([event(i) for i in range(1, 5)] + [event(5, wake=False), event(6, kind="trade")], 6)
    notices = store.pending()
    store.signaled(notices[1])
    for index, job in ((2, "pending-job"), (3, "completed-job")):
        store.signaled(notices[index])
        store.assign([notices[index]], job)
        memory.start_job(job, "communication", {}, snapshot=store.snapshot([notices[index]]))
    memory.finish_job("completed-job", True)
    store.finish([notices[3]], "completed-job")
    memory.pause(True)
    return memory.backup(native.path), notices


def rows(memory):
    with memory.transaction() as db:
        return [dict(row) for row in db.execute(select(notifications).order_by(notifications.c.source)).mappings()]


def rewrite(path, saved):
    path.write_text(json.dumps(saved))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_restore_rebases_exact_baseline_dispositions_without_replaying_events(native):
    checksum, original = captured(native)
    memory, artifacts = native.memory, native.artifacts
    baseline = rows(memory)
    with memory.transaction() as db:
        inbox = list(db.scalars(select(domain.inbox.c.id).order_by(domain.inbox.c.id)))
        records = db.scalar(select(func.count()).select_from(domain.records))
    memory.pause(False)
    memory.finish_job("pending-job", True)
    native.store.finish([original[2]], "pending-job")
    native.store.signaled(original[0])
    native.store.ingest([event(7, text="Discarded future private message")], 7)
    with memory.transaction() as db:
        future_records = db.scalar(select(func.count()).select_from(domain.records))
    artifacts.bind_epoch("game", "e2", previous="e1")
    memory.restore(native.path, checksum, "e2", notification_artifacts=artifacts)
    restored = CommunicationStore(memory, artifacts, communication_scope("game", "p0", "e2"))
    pending = restored.pending()
    assert [notice.native_id for notice in pending] == [1, 2, 3]
    assert all(notice.id not in {old.id for old in original} for notice in pending)
    assert [artifacts.get(restored.scope, notice.content) for notice in pending] == [event(i) for i in (1, 2, 3)]
    current = rows(memory)
    assert [row["source"] for row in current] == [row["source"] for row in baseline]
    for index in range(3):
        assert (current[index]["job"], current[index]["signaled"], current[index]["handled"]) == (None, 0, 0)
    for index in range(3, 6):
        assert {key: current[index][key] for key in ("job", "signaled", "handled")} == {
            key: baseline[index][key] for key in ("job", "signaled", "handled")
        }
    assert memory.cursor() == 6 and memory.is_paused()
    snapshot = restored.snapshot(pending)
    assert [item["id"] for item in snapshot["events"]] == [1, 2, 3]
    assert snapshot["communicationEventIds"] == [1, 2, 3]
    assert all(record["category"] == "statement" for record in snapshot["records"].values())
    assert "Discarded future" not in json.dumps(snapshot)
    with memory.transaction() as db:
        assert list(db.scalars(select(domain.inbox.c.id).order_by(domain.inbox.c.id))) == inbox
        assert db.scalar(select(func.count()).select_from(domain.events)) == 7
        assert db.scalar(select(func.count()).select_from(domain.records)) == future_records == records + 1
    with pytest.raises(JournalConflict, match="EPOCH_CONFLICT"):
        artifacts.get(original[0].scope, original[0].content)
    with pytest.raises(JournalConflict, match="EPOCH_CONFLICT"):
        native.store.signaled(original[0])
    memory.restore(native.path, checksum, "e2", notification_artifacts=artifacts)
    assert rows(memory) == current
    with artifacts.engine.begin() as db:
        assert db.scalar(select(func.count()).select_from(artifact_scopes)) == 13  # seven original, six target bindings


def test_restored_origin_survives_second_epoch_checkpoint_and_new_events(native):
    checksum, _ = captured(native)
    native.artifacts.bind_epoch("game", "e2", previous="e1")
    native.memory.restore(native.path, checksum, "e2", notification_artifacts=native.artifacts)
    target = CommunicationStore(native.memory, native.artifacts, communication_scope("game", "p0", "e2"))
    native.memory.pause(False)
    target.ingest([event(7)], 7)
    native.memory.pause(True)
    second = native.path.with_name("second.json")
    checksum = native.memory.backup(second)
    native.artifacts.bind_epoch("game", "e3", previous="e2")
    native.memory.restore(second, checksum, "e3", notification_artifacts=native.artifacts)
    third = CommunicationStore(native.memory, native.artifacts, communication_scope("game", "p0", "e3"))
    assert [row["source"] for row in rows(native.memory)] == [f"e1:{i}" for i in range(1, 7)] + ["e2:7"]
    assert [item["id"] for item in third.snapshot(third.pending())["events"]] == [1, 2, 3, 7]
    assert native.memory.cursor() == 7


def test_new_native_event_number_cannot_reveal_same_number_from_discarded_future(native):
    checksum, _ = captured(native)
    native.memory.pause(False)
    native.store.ingest([event(7, text="Discarded future private text")], 7)
    native.artifacts.bind_epoch("game", "e2", previous="e1")
    native.memory.restore(native.path, checksum, "e2", notification_artifacts=native.artifacts)
    target = CommunicationStore(native.memory, native.artifacts, communication_scope("game", "p0", "e2"))
    native.memory.pause(False)
    target.ingest([event(7, text="New timeline stated intention")], 7)
    notice = next(notice for notice in target.pending() if notice.native_id == 7)
    snapshot = target.snapshot([notice])
    assert snapshot["events"] == [event(7, text="New timeline stated intention")]
    assert "Discarded future" not in json.dumps(snapshot)
    assert native.artifacts.get(target.scope, notice.content) == event(7, text="New timeline stated intention")
    with native.memory.transaction() as db:
        sources = list(db.scalars(select(domain.events.c.source).join(domain.inbox).order_by(domain.events.c.id)))
        assert sources == [f"e1:{i}" for i in range(1, 7)] + ["e2:7"]


@pytest.mark.parametrize("failure", ["missing_store", "unbound_epoch", "foreign_player", "same_epoch"])
def test_restore_requires_scoped_artifact_installation_before_any_domain_mutation(native, tmp_path, failure):
    checksum, _ = captured(native)
    before = rows(native.memory)
    artifacts = native.artifacts
    epoch = "e2"
    other = None
    if failure == "missing_store":
        artifacts = None
    elif failure == "foreign_player":
        other = WorkflowStore(tmp_path / "foreign", player="p1")
        other.bind_epoch("game", "e2")
        artifacts = other
    elif failure == "same_epoch":
        epoch = "e1"
    try:
        with pytest.raises((StateConflict, JournalConflict)):
            native.memory.restore(native.path, checksum, epoch, notification_artifacts=artifacts)
        assert rows(native.memory) == before
        with native.memory.transaction() as db:
            assert native.memory._meta(db, "epoch") == "e1"
        assert native.memory.cursor() == 6
    finally:
        if other is not None:
            other.close()


@pytest.mark.parametrize("damage", ["scope", "id", "content", "source", "notify", "missing", "duplicate", "raw_flag"])
def test_snapshot_projection_tampering_is_rejected_against_original_inbox(native, damage):
    checksum, _ = captured(native)
    saved = native.memory.verify(native.path, checksum)
    entries = saved["nativeNotifications"]["rows"]
    entry = entries[0]
    if damage == "scope":
        entry["notice"]["scope"]["player"] = "p1"
    elif damage == "id":
        entry["notice"]["id"] = "0" * 64
    elif damage == "content":
        entry["notice"]["content"]["sha256"] = "0" * 64
    elif damage == "source":
        entry["source"] = "e1:999"
    elif damage == "notify":
        entry["notify"] = False
    elif damage == "missing":
        entries.pop(0)
    elif damage == "duplicate":
        entries.append(entry.copy())
    else:
        entry["signaled"] = 1
    checksum = rewrite(native.path, saved)
    before = rows(native.memory)
    with pytest.raises(StateConflict, match="CHECKPOINT_NOTIFICATIONS_"):
        native.memory.verify(native.path, checksum)
    assert rows(native.memory) == before


def test_legacy_process_snapshot_works_and_missing_native_dispositions_fail_closed(native, tmp_path):
    checksum, _ = captured(native)
    saved = native.memory.verify(native.path, checksum)
    saved.pop("nativeNotifications")
    checksum = rewrite(native.path, saved)
    with pytest.raises(StateConflict, match="CHECKPOINT_NOTIFICATION_PROJECTION_REQUIRED"):
        native.memory.restore(native.path, checksum, "e2", notification_artifacts=native.artifacts)
    legacy = CivilizationState(tmp_path / "legacy", "Rome")
    try:
        legacy.bind("e1")
        legacy.ingest("e1", [event(1)], 1)
        path = tmp_path / "legacy.json"
        checksum = legacy.backup(path)
        assert "nativeNotifications" not in legacy.verify(path, checksum)
        legacy.restore(path, checksum, "e2")
        assert legacy.cursor() == 1 and legacy.snapshot("communication")["events"] == [event(1)]
    finally:
        legacy.engine.dispose()


def test_empty_legacy_native_prefix_does_not_infer_state_from_future_rows(native):
    checksum = native.memory.backup(native.path)
    saved = native.memory.verify(native.path, checksum)
    saved.pop("nativeNotifications")
    checksum = rewrite(native.path, saved)
    native.store.ingest([event(1)], 1)
    native.memory.restore(native.path, checksum, "e2")
    assert rows(native.memory) == [] and native.memory.cursor() == 0


@pytest.mark.parametrize("selection", [["e1:1", "e1:99"], ["../x:1"], ["e1:0"]])
def test_explicit_source_selection_requires_current_owner_notification_binding(native, selection):
    native.store.ingest([event(1)], 1)
    with pytest.raises(StateConflict, match="COMMUNICATION_EVENT_SELECTION_INVALID"):
        native.memory.snapshot("communication", event_sources=selection)
    with pytest.raises(StateConflict, match="COMMUNICATION_EVENT_SELECTION_INVALID"):
        native.memory.snapshot("gameplay", event_sources=["e1:1"])


def test_future_mutable_rows_do_not_change_saved_projection_verification(native):
    checksum, _ = captured(native)
    with native.memory.transaction() as db:
        db.execute(update(notifications).values(handled=1, signaled=1, job="future-job"))
    saved = native.memory.verify(native.path, checksum)
    assert saved["nativeNotifications"]["rows"][0]["handled"] is False


def test_foreign_scope_source_is_not_an_explicit_selection_capability(native):
    native.store.ingest([event(1)], 1)
    row = rows(native.memory)[0]
    with native.memory.transaction() as db:
        notice = EventNotice.model_validate_json(row["notice"])
        scope = notice.scope.model_copy(update={"epoch": "old"})
        db.execute(update(notifications).values(scope=canonical(scope.model_dump())))
    with pytest.raises(StateConflict, match="COMMUNICATION_EVENT_SELECTION_INVALID"):
        native.memory.snapshot("communication", event_sources=["e1:1"])


def test_interrupted_artifact_projection_keeps_domain_unchanged_and_retry_is_idempotent(native, monkeypatch):
    checksum, _ = captured(native)
    before = rows(native.memory)
    native.artifacts.bind_epoch("game", "e2", previous="e1")
    original = native.artifacts.put
    calls = 0

    def failing(scope, value):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("Synthetic interruption before domain install")
        return original(scope, value)

    monkeypatch.setattr(native.artifacts, "put", failing)
    with pytest.raises(OSError, match="Synthetic interruption"):
        native.memory.restore(native.path, checksum, "e2", notification_artifacts=native.artifacts)
    assert rows(native.memory) == before
    with native.memory.transaction() as db:
        assert native.memory._meta(db, "epoch") == "e1"
    monkeypatch.setattr(native.artifacts, "put", original)
    native.memory.restore(native.path, checksum, "e2", notification_artifacts=native.artifacts)
    target = CommunicationStore(native.memory, native.artifacts, communication_scope("game", "p0", "e2"))
    assert [notice.native_id for notice in target.pending()] == [1, 2, 3]
    with native.artifacts.engine.begin() as db:
        assert db.scalar(select(func.count()).select_from(artifact_scopes)) == 12
