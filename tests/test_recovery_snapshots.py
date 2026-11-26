"""Native public stores provide restore mechanics; admission stays fenced."""

import asyncio
import hashlib
import json
import sqlite3
from contextlib import asynccontextmanager

import pytest
from pydantic import ValidationError
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai_harness.memory import MemoryConflictError, MemoryOperation, SqliteMemoryStore
from pydantic_ai_harness.planning import PlanItem, PlanStore, SqlitePlanStore, TaskStatus

from civ_agent.orchestration.authored_memory import AuthoredMetadata, authored_prefix, decode_document, document_kind
from civ_agent.orchestration.contracts import GatewayJob, PlayerRole, Scope, digest
from civ_agent.orchestration.player_plans import plan_session
from civ_agent.orchestration.recovery_snapshots import (
    NativeRecoverySnapshot,
    backup_sqlite,
    export_native_snapshot,
    install_native_snapshot,
)
from civ_agent.orchestration.store import JournalConflict, WorkflowStore
from civ_agent.orchestration.transcripts import backend
from tests.step_history import save_history


class RestoreGate:
    """Stand-in for the native root's durable restoring/ready/admitted state."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.phase = "restoring"
        self.held = False

    @asynccontextmanager
    async def permit(self):
        async with self.lock:
            if self.phase != "restoring":
                raise JournalConflict("RESTORE_ALREADY_PUBLISHED")
            self.held = True
            try:
                yield
            except BaseException:
                raise  # A failed install remains restoring and may retry.
            else:
                self.phase = "ready"
            finally:
                self.held = False

    async def admit(self):
        async with self.lock:
            if self.phase != "ready":
                raise JournalConflict("RESTORE_INCOMPLETE")
            self.phase = "admitted"


def scopes():
    old = Scope(game="game", player="p0", epoch="e1", activation="original")
    return old, old.model_copy(update={"epoch": "e2", "activation": "restored"})


def plan(database, scope, purpose: PlayerRole = "gameplay"):
    identity = GatewayJob(scope=scope, job="checkpoint", purpose=purpose)
    return SqlitePlanStore(str(database), session=plan_session(identity))


async def source(tmp_path):
    old, target = scopes()
    memory = SqliteMemoryStore(database=tmp_path / "memory.sqlite3")
    files = {
        "MEMORY.md": "# Private notes\n\nSee strategy.md for the adopted strategy.\n",
        "note-origin.md": "The saved position, not the abandoned future.\n",
        "belief-offer.md": "The offer may be renewed. This is uncertain player-authored text.\n",
        "contribution-" + digest("communication-job")[:16] + "-offer.md": "A suggestion, never an adopted strategy.\n",
        "strategy.md": json.dumps({"type": "strategy", "assessment": "Grow safely"}),
        "city-city1.md": json.dumps(
            {"type": "city_plan", "city": "city1", "reasons": {"construction": "Retain the current queue"}}
        ),
    }
    for name, content in files.items():
        await memory.write_with_metadata(
            authored_prefix(old) + name,
            content,
            expected_version=None,
            operation=MemoryOperation(id=digest(["original", name]), fingerprint=digest(content)),
            metadata=AuthoredMetadata(
                game=old.game,
                player=old.player,
                epoch=old.epoch,
                job="communication-job" if name.startswith("contribution-") else "old-job",
                purpose="communication" if name.startswith("contribution-") else "gameplay",
                kind=document_kind(name),
                createdTurn=4,
                updatedTurn=4,
            ).strings(),
        )
    plans_database = tmp_path / "plans.sqlite3"
    old_plans: dict[PlayerRole, PlanStore] = {"gameplay": plan(plans_database, old)}
    await old_plans["gameplay"].set_items(
        [
            PlanItem(id="inspect", content="Inspect the permitted frontier", status=TaskStatus.in_progress),
            PlanItem(id="consider", content="Consider the next turn", depends_on=["inspect"]),
        ]
    )
    saved = await export_native_snapshot("boundary-1", old, memory, old_plans)
    return old, target, memory, plans_database, saved


def test_sqlite_online_backup_includes_committed_wal_and_excludes_uncommitted_changes(tmp_path):
    original, destination = tmp_path / "live.sqlite3", tmp_path / "backup/state.sqlite3"
    with sqlite3.connect(original) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE state (value TEXT)")
        writer.execute("INSERT INTO state VALUES ('committed')")
        writer.commit()
        assert original.with_name(original.name + "-wal").stat().st_size > 0
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO state VALUES ('uncommitted')")
        saved = backup_sqlite(original, destination)
        with sqlite3.connect(destination) as restored:
            assert restored.execute("SELECT value FROM state").fetchall() == [("committed",)]
            assert restored.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert saved.sha256 == hashlib.sha256(destination.read_bytes()).hexdigest()
        assert saved.bytes == destination.stat().st_size and saved.filename == "state.sqlite3"
        assert destination.stat().st_mode & 0o777 == 0o600
        with pytest.raises(ValueError, match="PATH_CONFLICT"):
            backup_sqlite(original, destination)
        writer.rollback()
    assert not list(destination.parent.glob(".sqlite-backup-*"))


def test_explicit_rewind_installs_saved_native_notes_and_typed_tasks_into_the_new_epoch(tmp_path):
    async def run():
        old, target, memory, plans_database, saved = await source(tmp_path)
        saved = NativeRecoverySnapshot.model_validate_json(saved.model_dump_json())
        original = await memory.read(authored_prefix(old) + "note-origin.md", max_chars=8000)
        assert original is not None and original.metadata is not None
        await memory.write_with_metadata(
            authored_prefix(old) + "note-origin.md",
            "Discarded future.\n",
            expected_version=original.version,
            metadata=original.metadata,
            operation=MemoryOperation(id="future", fingerprint=digest("future")),
        )
        await plan(plans_database, old).set_items([PlanItem(id="future", content="Discarded future")])
        target_plans: dict[PlayerRole, PlanStore] = {"gameplay": plan(plans_database, target)}
        gate = RestoreGate()
        await install_native_snapshot(saved, target, memory, target_plans, permit=gate.permit)
        restored = await memory.read(authored_prefix(target) + "note-origin.md", max_chars=8000)
        assert restored is not None and "saved position" in restored.content
        assert restored.content == original.content
        assert restored.operation_id != original.operation_id
        assert restored.metadata == {**dict(original.metadata), "epoch": "e2", "originEpoch": "e1"}
        assert (
            decode_document(authored_prefix(target) + "note-origin.md", restored)["body"]["basis"]["job"] == "old-job"
        )
        index = await memory.read(authored_prefix(target) + "MEMORY.md", max_chars=8000)
        assert index is not None and index.content.startswith("# Private notes\n")
        strategy = await memory.read(authored_prefix(target) + "strategy.md", max_chars=8000)
        assert strategy is not None and decode_document("strategy.md", strategy)["category"] == "strategy"
        city = await memory.read(authored_prefix(target) + "city-city1.md", max_chars=8000)
        assert city is not None and decode_document("city-city1.md", city)["body"]["city"] == "city1"
        contribution_name = "contribution-" + digest("communication-job")[:16] + "-offer.md"
        contribution = await memory.read(authored_prefix(target) + contribution_name, max_chars=8000)
        assert (
            contribution is not None and decode_document(contribution_name, contribution)["category"] == "contribution"
        )
        tasks = await target_plans["gameplay"].get_items()
        assert [item.id for item in tasks] == ["inspect", "consider"]
        assert tasks[0].status == TaskStatus.in_progress and tasks[1].depends_on == ["inspect"]
        abandoned = await memory.read(authored_prefix(old) + "note-origin.md", max_chars=8000)
        assert abandoned is not None and "Discarded future" in abandoned.content
        assert gate.phase == "ready"

    asyncio.run(run())


@pytest.mark.parametrize(
    "damage",
    [
        "source_player",
        "document_scope",
        "filename",
        "plan_ids",
        "target_owner",
        "same_epoch",
        "missing_role",
        "typed_strategy",
        "typed_city",
        "metadata_kind",
    ],
)
def test_all_snapshot_scope_and_identity_validation_precedes_every_mutation(tmp_path, damage):
    async def run():
        _, target, memory, plans_database, saved = await source(tmp_path)
        wire = saved.model_dump(mode="json")
        plans: dict[PlayerRole, PlanStore] = {"gameplay": plan(plans_database, target)}
        if damage == "source_player":
            wire["source"]["player"] = "foreign"
        elif damage == "document_scope":
            wire["documents"][-1]["metadata"]["epoch"] = "foreign"
        elif damage == "filename":
            wire["documents"][-1]["name"] = "../note.md"
        elif damage == "plan_ids":
            wire["plans"]["gameplay"][-1].pop("id")
        elif damage == "target_owner":
            target = target.model_copy(update={"player": "foreign"})
        elif damage == "same_epoch":
            target = target.model_copy(update={"epoch": "e1"})
        elif damage == "typed_strategy":
            next(document for document in wire["documents"] if document["name"] == "strategy.md")["content"] = (
                "# Plain text is not a typed strategy"
            )
        elif damage == "typed_city":
            next(document for document in wire["documents"] if document["name"] == "city-city1.md")["content"] = (
                json.dumps({"type": "city_plan", "city": "city2", "reasons": {}})
            )
        elif damage == "metadata_kind":
            next(document for document in wire["documents"] if document["name"] == "strategy.md")["metadata"][
                "kind"
            ] = "note"
        else:
            plans = {}
        gate = RestoreGate()
        with pytest.raises((ValidationError, JournalConflict)):
            snapshot = NativeRecoverySnapshot.model_validate_json(json.dumps(wire))
            await install_native_snapshot(snapshot, target, memory, plans, permit=gate.permit)
        assert not await memory.list_paths(
            authored_prefix(target) if damage != "same_epoch" else "game/p0/e2/notes/", limit=10
        )
        assert not await plan(plans_database, scopes()[1]).get_items()
        assert gate.phase == "restoring" and not gate.held

    asyncio.run(run())


def test_partial_restore_replays_native_memory_receipts_and_does_not_admit_a_new_root_early(tmp_path):
    async def run():
        _, target, memory, plans_database, saved = await source(tmp_path)
        gate = RestoreGate()
        entered, release = asyncio.Event(), asyncio.Event()
        native_plan = plan(plans_database, target)

        class Plan(SqlitePlanStore):
            failed = False

            async def get_items(self):
                return await native_plan.get_items()

            async def set_items(self, items):
                assert gate.held and gate.phase == "restoring"
                if not self.failed:
                    self.failed = True
                    raise OSError("interrupted installation")
                entered.set()
                await release.wait()
                await native_plan.set_items(items)

        proxy = Plan(
            str(plans_database), session=plan_session(GatewayJob(scope=target, job="checkpoint", purpose="gameplay"))
        )
        with pytest.raises(OSError, match="interrupted"):
            await install_native_snapshot(saved, target, memory, {"gameplay": proxy}, permit=gate.permit)
        assert gate.phase == "restoring"
        with pytest.raises(JournalConflict, match="INCOMPLETE"):
            await gate.admit()
        with sqlite3.connect(tmp_path / "memory.sqlite3") as db:
            writes_before = db.execute("SELECT count(*) FROM memory_operations").fetchone()[0]
        installing = asyncio.create_task(
            install_native_snapshot(saved, target, memory, {"gameplay": proxy}, permit=gate.permit)
        )
        await asyncio.wait_for(entered.wait(), 2)
        admitting = asyncio.create_task(gate.admit())
        await asyncio.sleep(0)
        assert not admitting.done() and not installing.done()
        release.set()
        await asyncio.wait_for(asyncio.gather(installing, admitting), 2)
        assert gate.phase == "admitted"
        with sqlite3.connect(tmp_path / "memory.sqlite3") as db:
            assert db.execute("SELECT count(*) FROM memory_operations").fetchone()[0] == writes_before
        assert [item.id for item in await native_plan.get_items()] == ["inspect", "consider"]

    asyncio.run(run())


def test_a_late_restore_retry_cannot_overwrite_active_epoch_notes_or_tasks(tmp_path):
    async def run():
        _, target, memory, plans_database, saved = await source(tmp_path)
        gate = RestoreGate()
        plans: dict[PlayerRole, PlanStore] = {"gameplay": plan(plans_database, target)}
        await install_native_snapshot(saved, target, memory, plans, permit=gate.permit)
        await gate.admit()
        path = authored_prefix(target) + "note-origin.md"
        previous = await memory.read(path, max_chars=8000)
        assert previous is not None
        await memory.write_with_metadata(
            path,
            "Active new decision.\n",
            expected_version=previous.version,
            metadata=previous.metadata,
            operation=MemoryOperation(id="active", fingerprint=digest("active")),
        )
        await plans["gameplay"].set_items([PlanItem(id="active", content="Active new plan")])
        with pytest.raises(JournalConflict, match="ALREADY_PUBLISHED"):
            await install_native_snapshot(saved, target, memory, plans, permit=gate.permit)
        active = await memory.read(path, max_chars=8000)
        assert active is not None and "Active new decision" in active.content
        assert [item.id for item in await plans["gameplay"].get_items()] == ["active"]
        # Native CAS also protects a fresh-epoch collision before any plan write.
        with pytest.raises(MemoryConflictError):
            await install_native_snapshot(
                saved.model_copy(update={"checkpoint_id": "other-boundary"}),
                target,
                memory,
                plans,
                permit=RestoreGate().permit,
            )

    asyncio.run(run())


def test_rewind_install_never_replaces_upstream_histories_or_paid_generation_attempts(tmp_path):
    async def run():
        old, target, memory, plans_database, saved = await source(tmp_path)
        store = WorkflowStore(tmp_path / "player-store", player="p0")
        store.bind_epoch(old.game, old.epoch)
        steps = backend(store, create=True)
        transcript = await save_history(
            store, old, [ModelResponse(parts=[TextPart("Abandoned history, retained privately")])]
        )
        with store.engine.begin() as db:
            db.exec_driver_sql(
                "INSERT INTO generation_attempts VALUES('paid-attempt', ?)", (json.dumps(old.model_dump()),)
            )
        run = await steps.get_run(run_id=transcript.run_id)
        history = await steps.list_snapshots(run_id=transcript.run_id)
        try:
            await install_native_snapshot(
                saved, target, memory, {"gameplay": plan(plans_database, target)}, permit=RestoreGate().permit
            )
            store.bind_epoch(target.game, target.epoch, previous=old.epoch)
            with store.engine.begin() as db:
                assert db.exec_driver_sql("SELECT id FROM generation_attempts").all() == [("paid-attempt",)]
            assert await steps.get_run(run_id=transcript.run_id) == run
            assert await steps.list_snapshots(run_id=transcript.run_id) == history
            with pytest.raises(JournalConflict, match="EPOCH_CONFLICT"):
                store.put(old, {"obsolete": "late old activity"})
        finally:
            store.close()

    asyncio.run(run())


def test_repeated_explicit_rewind_preserves_original_authorship_provenance(tmp_path):
    async def run():
        _, target, memory, plans_database, saved = await source(tmp_path)
        plans: dict[PlayerRole, PlanStore] = {"gameplay": plan(plans_database, target)}
        await install_native_snapshot(saved, target, memory, plans, permit=RestoreGate().permit)
        next_boundary = await export_native_snapshot("boundary-2", target, memory, plans)
        next_epoch = target.model_copy(update={"epoch": "e3"})
        await install_native_snapshot(
            next_boundary,
            next_epoch,
            memory,
            {"gameplay": plan(plans_database, next_epoch)},
            permit=RestoreGate().permit,
        )
        restored = await memory.read(authored_prefix(next_epoch) + "note-origin.md", max_chars=8000)
        assert restored is not None and restored.metadata is not None
        assert restored.metadata["epoch"] == "e3" and restored.metadata["originEpoch"] == "e1"
        assert restored.metadata["job"] == "old-job" and restored.metadata["createdTurn"] == "4"

    asyncio.run(run())


def test_snapshot_export_rejects_a_foreign_listing_before_reading_its_content(tmp_path):
    async def run():
        class Memory(SqliteMemoryStore):
            async def list_paths(self, prefix="", *, limit):
                return ["game/another-player/e1/notes/note-private.md"]

            async def read(self, path, *, max_chars):
                raise AssertionError("A foreign listing must never grant a read")

        with pytest.raises(JournalConflict, match="SCOPE_CONFLICT"):
            await export_native_snapshot("boundary", scopes()[0], Memory(database=tmp_path / "unused.sqlite3"), {})

    asyncio.run(run())


def test_native_markdown_index_can_be_cleared_and_json_like_notes_remain_plain_text(tmp_path):
    async def run():
        old, target, memory, plans_database, _ = await source(tmp_path)
        for name, content in (
            ("MEMORY.md", ""),
            ("note-origin.md", '{"type":"strategy","assessment":"Only a quoted claim"}'),
        ):
            path = authored_prefix(old) + name
            previous = await memory.read(path, max_chars=8000)
            assert previous is not None
            await memory.write_with_metadata(
                path,
                content,
                expected_version=previous.version,
                metadata=previous.metadata,
                operation=MemoryOperation(id=digest(["native-edit", name]), fingerprint=digest(content)),
            )
        saved = await export_native_snapshot("cleared-index", old, memory, {"gameplay": plan(plans_database, old)})
        await install_native_snapshot(
            saved, target, memory, {"gameplay": plan(plans_database, target)}, permit=RestoreGate().permit
        )
        index = await memory.read(authored_prefix(target) + "MEMORY.md", max_chars=8000)
        note = await memory.read(authored_prefix(target) + "note-origin.md", max_chars=8000)
        assert index is not None and index.content == ""
        assert note is not None and decode_document("note-origin.md", note)["category"] == "note"
        assert note.content == '{"type":"strategy","assessment":"Only a quoted claim"}'

    asyncio.run(run())
