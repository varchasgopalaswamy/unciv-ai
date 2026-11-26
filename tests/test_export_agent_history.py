"""Operator exports use native SDK stores without contacting game/model services."""

import asyncio
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    BinaryContent,
    ModelMessagesTypeAdapter,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserContent,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness import StepPersistence
from pydantic_ai_harness.step_persistence import RunRecord, SqliteStepStore

from civ_agent.commands import history as exporter
from civ_agent.orchestration.contracts import PrepareRole, Scope, canonical
from civ_agent.orchestration.history_exports import ExportError, export_native_history
from civ_agent.orchestration.managed_epochs import publish_managed_epoch
from civ_agent.orchestration.recovery_snapshots import backup_sqlite
from civ_agent.orchestration.store import JournalConflict, WorkflowStore
from civ_agent.orchestration.transcripts import backend
from civ_agent.player_paths import ManagedPlayerPaths
from civ_agent.runtime.agents import LocalAgentRunner, LocalAgentServices
from civ_agent.tls import TLSConfigError, private_directory, write_private, write_private_json
from tests.archive_fixtures import request as requested_game
from tests.test_managed_epochs import Gate, ready, replacement
from tests.test_native_player_stores import production_stores


def layout(tmp_path):
    root = private_directory(tmp_path / "game")
    private_directory(root / "native-runtime")
    source = requested_game().model_copy(update={"seats": [requested_game().seats[0]]})
    source = source.model_copy(update={"authority": source.authority.model_copy(update={"epoch": "g1"})})
    write_private_json(root / "native-runtime/game.json", source.model_dump(mode="json"))
    paths = ManagedPlayerPaths(root, 0, "Rome")
    paths.prepare()
    return root, source, paths


async def record_sdk(
    paths, *, epoch="g1", text="PRIVATE_ROME_MODEL_INPUT", run_id=None, prompt: list[UserContent] | None = None
):
    journal = WorkflowStore(paths.store, player=paths.player)
    try:
        with journal.engine.connect() as db:
            existing = db.exec_driver_sql("SELECT epoch FROM epochs WHERE game = 'game'").scalar()
        journal.bind_epoch("game", epoch, previous=existing)
    finally:
        journal.close()
    path = paths.store / "agent-steps.sqlite3"
    if not path.exists():
        write_private(path, b"")
    steps = SqliteStepStore(database=path)
    scope = Scope(game="game", player=paths.player, epoch=epoch, activation="source-role")
    capability = StepPersistence(store=steps, metadata={"scope": canonical(scope.model_dump(mode="json"))})
    agent = Agent(
        FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart("PRIVATE_ROME_MODEL_OUTPUT")])),
        name="player_role_v1",
        capabilities=[capability],
    )
    result = await agent.run(prompt if prompt is not None else text, run_id=run_id)
    runs = await steps.list_runs()
    return steps, runs[-1], result


def source_bytes(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_actual_production_role_exports_native_messages_notes_and_role_plans_without_backend_calls(
    tmp_path, monkeypatch
):
    async def run():
        async with production_stores(private_directory(tmp_path / "role")) as p:
            calls = []

            def factory(request):
                def model(messages, info):
                    index = len(calls)
                    calls.append(messages)
                    name, arguments = [
                        ("write_plan", {"items": [{"content": "PRIVATE_NATIVE_ROLE_TASK", "status": "pending"}]}),
                        ("write_memory", {"content": "# PRIVATE_NATIVE_ROLE_NOTE"}),
                        ("finish_role", {"summary": "Private role work completed"}),
                    ][index]
                    return ModelResponse(parts=[ToolCallPart(name, arguments, "export-" + str(index))])

                return FunctionModel(model)

            request = await p.root.prepare_role(PrepareRole(ticket=p.ticket, purpose="gameplay"))
            assert request is not None
            runner = LocalAgentRunner(
                LocalAgentServices(invoke=p.gateway, step_store=backend(p.store, create=True), gateway=p.gateway)
            )
            result = await runner.run_role(
                request,
                startup=p.store.get(request.scope, request.startup),
                catalogue=p.store.get(request.scope, request.catalogue),
                model=factory(request),
            )
            assert result.completion and len(calls) == 3
            root, _, paths = layout(tmp_path)
            for source, destination in (
                (p.store.root / "workflow.sqlite3", paths.store / "workflow.sqlite3"),
                (p.store.root / "agent-steps.sqlite3", paths.store / "agent-steps.sqlite3"),
                (
                    tmp_path / "role/authored.sqlite3",
                    paths.store / "authored.sqlite3",
                ),
            ):
                backup_sqlite(source, destination)
            write_private_json(root / "operator-secret.json", {"token": "SECRET_NEVER_MODEL_INPUT"})
            native = backend(p.store)
            run_record = (await native.list_runs())[0]
            snapshot = await native.latest_snapshot(run_id=run_record.run_id, include_interrupted=True)
            assert snapshot is not None
            before = source_bytes(root)
            blocked = AsyncMock(side_effect=AssertionError("Exporter must not call a backend"))
            monkeypatch.setattr(p.host, "tool", blocked)
            monkeypatch.setattr(p.gateway, "post", blocked)
            monkeypatch.setattr(p.clients, "model", blocked)
            entries = await export_native_history(root, tmp_path / "exports", "Rome", notebooks=True)
            assert len(entries) == 1 and entries[0]["runId"] == run_record.run_id
            messages = ModelMessagesTypeAdapter.validate_json(Path(entries[0]["history"]).read_bytes())
            assert ModelMessagesTypeAdapter.dump_python(messages, mode="json") == ModelMessagesTypeAdapter.dump_python(
                snapshot.messages, mode="json"
            )
            exported = next((tmp_path / "exports").glob("export-*"))
            files = source_bytes(exported)
            assert all(path.stat().st_mode & 0o777 == 0o600 for path in exported.rglob("*") if path.is_file())
            assert any(
                name.endswith("notes/MEMORY.md") and b"PRIVATE_NATIVE_ROLE_NOTE" in raw for name, raw in files.items()
            )
            plan = next(raw for name, raw in files.items() if name.endswith("plans/gameplay.json"))
            assert json.loads(plan)[0]["content"] == "PRIVATE_NATIVE_ROLE_TASK"
            assert {Path(name).name for name in files if "/plans/" in name} == {
                role + ".json" for role in ("gameplay", "economy", "military", "communication")
            }
            assert all(b"SECRET_NEVER_MODEL_INPUT" not in raw for raw in files.values())
            assert source_bytes(root) == before and not blocked.await_count
            index = json.loads((exported / "index.json").read_text())
            assert index["epoch"] == index["currentEpoch"] == "g1" and set(index["notebooks"]) == {"p0"}
            effects = json.loads(Path(entries[0]["toolEffects"]).read_text())
            by_name = {effect["tool_name"]: effect for effect in effects}
            assert {"write_plan", "write_memory"} <= by_name.keys()
            assert all(effect["run_id"] == run_record.run_id for effect in effects)
            assert by_name["write_memory"]["status"] == by_name["write_plan"]["status"] == "completed"

    asyncio.run(run())


def test_player_selection_never_reads_or_exports_another_players_private_store(tmp_path):
    async def run():
        root, source, rome = layout(tmp_path)
        source = source.model_copy(update={"seats": requested_game().seats})
        write_private_json(root / "native-runtime/game.json", source.model_dump(mode="json"))
        greece = ManagedPlayerPaths(root, 1, "Greece")
        greece.prepare()
        await record_sdk(rome)
        await record_sdk(greece, text="PRIVATE_GREECE_MODEL_INPUT")
        # An inaccessible unselected database must not affect the Rome export.
        before = source_bytes(root)
        (greece.store / "agent-steps.sqlite3").chmod(0o000)
        try:
            entries = await export_native_history(root, tmp_path / "rome-only", "Rome")
            assert {entry["player"] for entry in entries} == {"p0"}
            assert all(
                b"PRIVATE_GREECE_MODEL_INPUT" not in raw for raw in source_bytes(tmp_path / "rome-only").values()
            )
            with pytest.raises((TLSConfigError, sqlite3.Error)):
                await export_native_history(root, tmp_path / "all")
        finally:
            (greece.store / "agent-steps.sqlite3").chmod(0o600)
        assert source_bytes(root) == before

    asyncio.run(run())


def test_native_media_and_message_timestamps_roundtrip_in_the_sdk_format(tmp_path):
    async def run():
        root, _, paths = layout(tmp_path)
        media = BinaryContent(data=b"\x00\xff\x80" * 30000, media_type="image/png")
        steps, run, _ = await record_sdk(paths, prompt=["PRIVATE_MULTIMODAL_INPUT", media])
        snapshot = await steps.latest_snapshot(run_id=run.run_id)
        assert snapshot is not None
        before = source_bytes(root)
        exported = await export_native_history(root, tmp_path / "export")
        parsed = ModelMessagesTypeAdapter.validate_json(Path(exported[0]["history"]).read_bytes())
        assert ModelMessagesTypeAdapter.dump_python(parsed, mode="json") == ModelMessagesTypeAdapter.dump_python(
            snapshot.messages, mode="json"
        )
        binaries = [
            item
            for message in parsed
            for part in message.parts
            if isinstance(part, UserPromptPart) and isinstance(part.content, list)
            for item in part.content
            if isinstance(item, BinaryContent)
        ]
        assert [item.data for item in binaries] == [media.data]
        assert source_bytes(root) == before

    asyncio.run(run())


def test_current_epoch_default_and_explicit_verified_historical_selection(tmp_path):
    async def run():
        root, source, paths = layout(tmp_path)
        _, old, _ = await record_sdk(paths, run_id="opaque/SDK:run-id")
        target = replacement(source, epoch="g2")
        await publish_managed_epoch(root, target, permit=Gate(ready(root, target)))
        _, current, _ = await record_sdk(paths, epoch="g2", text="PRIVATE_CURRENT_EPOCH")
        before = source_bytes(root)
        entries = await export_native_history(root, tmp_path / "current")
        assert [entry["runId"] for entry in entries] == [current.run_id]
        entries = await export_native_history(root, tmp_path / "historical", epoch="g1")
        assert [entry["runId"] for entry in entries] == [old.run_id]
        assert "PRIVATE_CURRENT_EPOCH" not in Path(entries[0]["history"]).read_text()
        assert source_bytes(root) == before
        with pytest.raises(JournalConflict, match="RETAINED_SOURCE"):
            await export_native_history(root, tmp_path / "unknown", epoch="unpublished")
        with pytest.raises(ExportError, match="No native player"):
            await export_native_history(root, tmp_path / "unknown-player", "Greece")

    asyncio.run(run())


@pytest.mark.parametrize("fault", ["owner", "scope", "snapshot", "publication", "permissions", "symlink"])
def test_export_rejects_invalid_private_ownership_publication_and_native_bindings(tmp_path, fault):
    async def run():
        root, _, paths = layout(tmp_path)
        steps, run, _ = await record_sdk(paths)
        if fault == "owner":
            journal = WorkflowStore(paths.store, player="p0")
            with journal.engine.begin() as db:
                db.exec_driver_sql("UPDATE ownership SET player = 'p9'")
            journal.close()
        elif fault == "scope":
            await steps.register_run(
                RunRecord(
                    run_id="foreign",
                    agent_name="player_role_v1",
                    metadata={
                        "scope": canonical(
                            Scope(game="game", player="p9", epoch="g1", activation="foreign").model_dump(mode="json")
                        )
                    },
                )
            )
        elif fault == "snapshot":
            snapshot = await steps.latest_snapshot(run_id=run.run_id)
            assert snapshot is not None
            await steps.save_snapshot(replace(snapshot, agent_name="foreign-agent", idempotency_key="foreign-snapshot"))
        elif fault == "publication":
            write_private_json(root / "native-runtime/current-epoch.json", {"epoch": "g9"})
        elif fault == "permissions":
            (paths.store / "agent-steps.sqlite3").chmod(0o644)
        else:
            original = paths.store / "agent-steps.sqlite3"
            moved = original.with_name("actual.sqlite3")
            original.rename(moved)
            original.symlink_to(moved)
        with pytest.raises((ExportError, JournalConflict, TLSConfigError, ValueError)):
            await export_native_history(root, tmp_path / "export")
        assert not list((tmp_path / "export").glob("export-*"))

    asyncio.run(run())


def test_cli_exports_native_files_and_reports_no_history_without_legacy_fallback(tmp_path, capsys):
    root, _, paths = layout(tmp_path)
    asyncio.run(record_sdk(paths))
    options = exporter.parser().parse_args(["Rome", "--state-dir", str(root), "--output-dir", str(tmp_path / "export")])
    assert exporter.run(options) == 0
    assert "Rome g1 player_role_v1" in capsys.readouterr().out
    write_private_json(root / "native-runtime/game.json", {"legacy": "not a native publication"})
    assert exporter.run(options) == 2
    captured = capsys.readouterr()
    assert "Cannot export native history" in captured.err and "legacy" not in captured.err
