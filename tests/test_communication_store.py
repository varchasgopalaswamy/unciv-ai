import json

import pytest
from sqlalchemy import select

from civ_agent import memory_schema as domain
from civ_agent.orchestration.communication_store import CommunicationStore, communication_scope, notifications
from civ_agent.orchestration.store import JournalConflict, WorkflowStore
from tests.domain_fixtures import managed, run_async
from tests.test_gateway_registry import registry


def event(identifier=1, *, text="An unverified statement", wake=True):
    return {
        "id": identifier,
        "kind": "message",
        "wake": wake,
        "body": {
            "message": "m" + str(identifier),
            "author": "Greece",
            "participants": ["Rome", "Greece"],
            "public": False,
            "text": text,
            "turn": 0,
        },
    }


@run_async
async def test_cursor_inbox_and_notifications_commit_together_and_replay_once(tmp_path, monkeypatch):
    async with managed(tmp_path) as m:
        owner = await registry(m)
        artifacts = WorkflowStore(tmp_path / "artifacts", player="p0")
        artifacts.bind_epoch("game", "g1")
        store = CommunicationStore(m.memory, artifacts, communication_scope("game", "p0", "g1"))
        try:
            check = store.check
            monkeypatch.setattr(
                store, "check", lambda _: (_ for _ in ()).throw(OSError("died before notification commit"))
            )
            with pytest.raises(OSError):
                store.ingest([event()], 1)
            assert m.memory.cursor() == 0 and not m.memory.snapshot("communication")["events"]
            monkeypatch.setattr(store, "check", check)
            store.ingest([event()], 1)
            original = store.pending()
            assert len(original) == 1 and m.memory.cursor() == 1
            assert artifacts.get(store.scope, original[0].content) == event()
            store.ingest([event()], 1)
            assert store.pending() == original
            with m.memory.transaction() as db:
                rows = list(db.execute(select(domain.events)).mappings())
                assert len(rows) == 1 and rows[0]["kind"] == "message"
                assert json.loads(rows[0]["body"])["body"]["text"] == "An unverified statement"
            store.signaled(original[0])
            assert not store.pending()
            assert store.assign(original, "job1") == original
            assert store.assign(original, "job1") == original
            with pytest.raises(JournalConflict, match="ALREADY_ASSIGNED"):
                store.assign(original, "job2")
            with pytest.raises(JournalConflict, match="UNFINISHED"):
                store.finish(original, "job1")
        finally:
            await owner.close()
            artifacts.close()


@run_async
async def test_changed_events_and_forged_scope_cannot_advance_or_acknowledge_the_inbox(tmp_path):
    async with managed(tmp_path) as m:
        owner = await registry(m)
        artifacts = WorkflowStore(tmp_path / "artifacts", player="p0")
        artifacts.bind_epoch("game", "g1")
        store = CommunicationStore(m.memory, artifacts, communication_scope("game", "p0", "g1"))
        try:
            store.ingest([event()], 1)
            original = store.pending()[0]
            with pytest.raises(JournalConflict, match="EVENT_CHANGED"):
                store.ingest([event(text="Different authored claim")], 2)
            assert m.memory.cursor() == 1 and store.pending() == [original]
            wrong = original.model_copy(update={"scope": original.scope.model_copy(update={"player": "p1"})})
            with pytest.raises(JournalConflict, match="SCOPE_CONFLICT"):
                store.signaled(wrong)
            changed = original.model_copy(update={"native_id": 2})
            with pytest.raises(JournalConflict, match="NOTICE_CONFLICT"):
                store.assign([changed], "job1")
            assert store.pending() == [original]
        finally:
            await owner.close()
            artifacts.close()


@run_async
async def test_batch_snapshot_does_not_lose_late_events_behind_an_older_handled_cursor(tmp_path):
    async with managed(tmp_path) as m:
        owner = await registry(m)
        artifacts = WorkflowStore(tmp_path / "artifacts", player="p0")
        artifacts.bind_epoch("game", "g1")
        store = CommunicationStore(m.memory, artifacts, communication_scope("game", "p0", "g1"))
        try:
            store.ingest([event(index, wake=index == 60) for index in range(1, 61)], 60)
            notice = store.pending()[0]
            with m.memory.transaction() as db:
                m.memory._set(db, "handled:communication", 60)
            assert not m.memory.snapshot("communication")["events"]
            selected = store.snapshot([notice])
            assert [row["id"] for row in selected["events"]] == [60]
            assert selected["communicationEventIds"] == [60]
            with m.memory.transaction() as db:
                assert (
                    db.execute(select(notifications.c.handled).where(notifications.c.id == notice.id)).scalar_one() == 0
                )
        finally:
            await owner.close()
            artifacts.close()
