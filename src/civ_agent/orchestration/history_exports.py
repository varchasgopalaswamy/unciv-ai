"""Private operator exports of published native SDK histories and authored stores."""

import asyncio
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from pydantic import TypeAdapter
from pydantic_ai.messages import ModelMessagesTypeAdapter, ToolCallPart, ToolReturnPart
from pydantic_ai_harness.memory import MemoryFile, SqliteMemoryStore
from pydantic_ai_harness.planning import PlanItem, SqlitePlanStore
from pydantic_ai_harness.step_persistence import (
    ContinuableSnapshot,
    RunRecord,
    SqliteStepStore,
    StepEvent,
    ToolEffectRecord,
)

from civ_agent.operator_state import (
    is_local,
)
from civ_agent.operator_state import (
    publication_basis as epoch_publication_basis,
)
from civ_agent.operator_state import (
    published_game as published_managed_epoch,
)
from civ_agent.orchestration.authored_memory import MAX_DOCUMENT_CHARS, MAX_DOCUMENTS, authored_prefix, decode_document
from civ_agent.orchestration.contracts import GatewayJob, PlayerRole, Scope, canonical, digest
from civ_agent.orchestration.managed_epochs import EpochPublicationBasis, retained_managed_epoch
from civ_agent.orchestration.player_plans import plan_session
from civ_agent.player_paths import PlayerPaths as ManagedPlayerPaths
from civ_agent.protocol import JsonObject
from civ_agent.runtime.overflow import export_spills
from civ_agent.tls import private_directory, regular_file, write_private, write_private_json

_RUN = TypeAdapter(RunRecord)
_SNAPSHOT = TypeAdapter(ContinuableSnapshot)
_EVENTS = TypeAdapter(list[StepEvent])
_EFFECTS = TypeAdapter(list[ToolEffectRecord])
_FILE = TypeAdapter(MemoryFile)
_PLAN = TypeAdapter(list[PlanItem])
_AGENTS = {"player_role_v1", "strategic_review_v1", "peace_military_v1", "peace_main_v1", "summarizing_compaction"}
_ROLES: tuple[PlayerRole, ...] = ("gameplay", "economy", "military", "communication")


class ExportError(ValueError):
    pass


def copy_native_database(source: Path, destination: Path) -> None:
    """Use SQLite's online backup; SDK schema initialization touches only scratch."""
    regular_file(source, private=True)
    if destination.exists() or destination.is_symlink():
        raise ExportError("Native history copies require a fresh scratch file.")
    mode = "?mode=ro"
    with closing(sqlite3.connect(source.resolve().as_uri() + mode, uri=True)) as reader:
        with closing(sqlite3.connect(destination)) as writer:
            reader.backup(writer)
            if writer.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ExportError("Native history database integrity check failed.")
    destination.chmod(0o600)


def verify_native_store_owner(database: Path, game: str, player: str, epoch: str) -> None:
    with closing(sqlite3.connect(database)) as db:
        if db.execute("SELECT singleton, player, version FROM ownership").fetchall() != [(1, player, 1)]:
            raise ExportError("Native history store belongs to a different player.")
        if db.execute("SELECT game, epoch FROM epochs").fetchall() != [(game, epoch)]:
            raise ExportError("Native history store differs from the published game epoch.")


def native_run_scope(run: RunRecord, game: str, player: str) -> Scope:
    try:
        scope = Scope.model_validate_json(run.metadata["scope"])
    except (KeyError, ValueError):
        raise ExportError("Native SDK run has no valid owning scope.") from None
    if (
        (scope.game, scope.player) != (game, player)
        or run.metadata["scope"] != canonical(scope.model_dump(mode="json"))
        or run.agent_name not in _AGENTS
        or run.metadata.get("agent_name", run.agent_name) != run.agent_name
        and not (run.agent_name == "summarizing_compaction" and run.metadata.get("purpose") == "context_summary")
        or not isinstance(run.run_id, str)
        or not 1 <= len(run.run_id) <= 1024
    ):
        raise ExportError("Native SDK run ownership or agent binding is invalid.")
    return scope


async def _notebooks(paths: ManagedPlayerPaths, scratch: Path, destination: Path, scope: Scope) -> JsonObject:
    combined = paths.store / "authored.sqlite3"
    if combined.exists() or combined.is_symlink():
        memory_path = plan_path = scratch / "authored.sqlite3"
        await asyncio.to_thread(copy_native_database, combined, memory_path)
    else:
        # Passive exports of retained pre-controller stores remain supported.
        memory_path, plan_path = scratch / "memory.sqlite3", scratch / "plans.sqlite3"
        await asyncio.to_thread(copy_native_database, paths.coordinator / "authored-memory.sqlite3", memory_path)
        await asyncio.to_thread(copy_native_database, paths.coordinator / "plans.sqlite3", plan_path)
    memory = SqliteMemoryStore(database=memory_path)
    prefix = authored_prefix(scope)
    found = await memory.list_paths(prefix, limit=MAX_DOCUMENTS + 1)
    if len(found) > MAX_DOCUMENTS:
        raise ExportError("Native authored-document export exceeds its bounded store limit.")
    notes = []
    for path in found:
        file = await memory.read(path, max_chars=MAX_DOCUMENT_CHARS)
        if file is None or not path.startswith(prefix):
            raise ExportError("Native authored document is missing or belongs to a different scope.")
        # Shared trusted metadata/document validation; plain Markdown remains
        # untrusted authored text and is never promoted to a game observation.
        decode_document(path, file)
        metadata = file.metadata or {}
        if (metadata.get("game"), metadata.get("player"), metadata.get("epoch")) != (
            scope.game,
            scope.player,
            scope.epoch,
        ):
            raise ExportError("Native authored document belongs to a different scope.")
        name = path.removeprefix(prefix)
        if Path(name).name != name or not name.endswith(".md"):
            raise ExportError("Native authored document has an invalid filename.")
        target = private_directory(destination / "notes") / name
        write_private(target, file.content.encode())
        write_private(target.with_suffix(".metadata.json"), _FILE.dump_json(file, indent=2))
        notes.append(str(target))
    plans = {}
    for role in _ROLES:
        identity = GatewayJob(scope=scope, job="operator-export", purpose=role)
        items = await SqlitePlanStore(str(plan_path), session=plan_session(identity)).get_items()
        target = private_directory(destination / "plans") / (role + ".json")
        write_private(target, _PLAN.dump_json(items, indent=2))
        plans[role] = str(target)
    return {"notes": notes, "plans": plans}


async def _export_native_history(
    state_dir: Path,
    output_dir: Path,
    civilization: str | None = None,
    *,
    notebooks: bool = False,
    epoch: str | None = None,
) -> list[JsonObject]:
    """Export SDK-owned formats without provider or game API calls.

    Independent committed SQLite backups are observational exports, not a
    coordinated recovery checkpoint. Historical selection is privileged and
    restricted to the current publication's verified ancestor chain.
    """
    state_dir = Path(state_dir)
    basis = epoch_publication_basis(state_dir)
    current = published_managed_epoch(state_dir)
    game, current_epoch = current.authority.game, current.authority.epoch
    selected_epoch = epoch if epoch is not None else current_epoch
    if is_local(state_dir):
        if selected_epoch != current_epoch:
            raise ExportError("The local controller retains attempts within its current epoch.")
        selected = current
    else:
        selected = retained_managed_epoch(state_dir, game, selected_epoch).request
    if selected.seats != current.seats:
        raise ExportError("Native history publication changed its player roster.")
    seats = [(index, seat) for index, seat in enumerate(selected.seats) if civilization in {None, seat.civilization}]
    if not seats:
        raise ExportError("No native player matches this civilization selection.")
    entries, exported_notes = [], {}
    destination = Path(tempfile.mkdtemp(prefix="export-", dir=private_directory(output_dir)))
    try:
        with tempfile.TemporaryDirectory(prefix="native-history-") as scratch_name:
            scratch_root = Path(scratch_name)
            for index, seat in seats:
                paths = ManagedPlayerPaths(state_dir, index, seat.civilization)
                if paths.player != seat.session.player or seat.session.game != game:
                    raise ExportError("Native player paths differ from the published owning session.")
                scratch = private_directory(scratch_root / paths.player)
                await asyncio.to_thread(
                    copy_native_database, paths.store / "workflow.sqlite3", scratch / "workflow.sqlite3"
                )
                await asyncio.to_thread(
                    verify_native_store_owner, scratch / "workflow.sqlite3", game, paths.player, current_epoch
                )
                target = private_directory(destination / ("epoch-" + digest(selected_epoch)) / paths.name)
                source = paths.store / "agent-steps.sqlite3"
                if source.exists() or source.is_symlink():
                    await asyncio.to_thread(copy_native_database, source, scratch / "steps.sqlite3")
                    store = SqliteStepStore(database=scratch / "steps.sqlite3")
                    for run in await store.list_runs():
                        scope = native_run_scope(run, game, paths.player)
                        if scope.epoch != selected_epoch:
                            continue
                        snapshot = await store.latest_snapshot(run_id=run.run_id, include_interrupted=True)
                        events = await store.list_events(run_id=run.run_id)
                        if (
                            any(event.run_id != run.run_id for event in events)
                            or snapshot is not None
                            and (
                                snapshot.run_id,
                                snapshot.agent_name,
                                snapshot.conversation_id,
                            )
                            != (run.run_id, run.agent_name, run.conversation_id)
                        ):
                            raise ExportError("Native SDK snapshot or event run binding is invalid.")
                        # Native run IDs are opaque, not owned path segments.
                        # The index preserves them exactly; bounded hashes keep
                        # even long upstream IDs within filesystem limits.
                        folder = private_directory(target / digest([scope.activation, run.run_id]))
                        write_private(folder / "run.json", _RUN.dump_json(run, indent=2))
                        write_private(folder / "events.json", _EVENTS.dump_json(events, indent=2))
                        effects = {
                            effect.tool_call_id: effect
                            for effect in await store.list_unresolved_tool_effects(run_id=run.run_id)
                        }
                        for event in events:
                            if event.tool_call_id is not None:
                                effect = await store.get_tool_effect(run_id=run.run_id, tool_call_id=event.tool_call_id)
                                if effect is not None:
                                    if effect.tool_call_id != event.tool_call_id:
                                        raise ExportError("Native event tool-effect binding is invalid.")
                                    effects[effect.tool_call_id] = effect
                        history = None
                        if snapshot is not None:
                            write_private(folder / "snapshot.json", _SNAPSHOT.dump_json(snapshot, indent=2))
                            history = folder / "messages.json"
                            write_private(history, ModelMessagesTypeAdapter.dump_json(snapshot.messages, indent=2))
                            for message in snapshot.messages:
                                for part in message.parts:
                                    if isinstance(part, ToolCallPart):
                                        effect = await store.get_tool_effect(
                                            run_id=run.run_id, tool_call_id=part.tool_call_id
                                        )
                                        if effect is not None:
                                            if effect.tool_call_id != part.tool_call_id:
                                                raise ExportError("Native SDK tool-effect call binding is invalid.")
                                            effects[effect.tool_call_id] = effect
                                    elif isinstance(part, ToolReturnPart) and isinstance(part.metadata, dict):
                                        for child_id in part.metadata.get("tool_calls", {}):
                                            effect = await store.get_tool_effect(
                                                run_id=run.run_id, tool_call_id=child_id
                                            )
                                            if effect is not None:
                                                if effect.tool_call_id != child_id:
                                                    raise ExportError("Native nested tool-effect binding is invalid.")
                                                effects[child_id] = effect
                        if any(effect.run_id != run.run_id for effect in effects.values()):
                            raise ExportError("Native SDK tool-effect run binding is invalid.")
                        write_private(
                            folder / "tool-effects.json",
                            _EFFECTS.dump_json([effects[key] for key in sorted(effects)], indent=2),
                        )
                        spills = await asyncio.to_thread(
                            export_spills, paths.store / "tool-output", folder / "tool-output", scope
                        )
                        entries.append(
                            {
                                "civilization": seat.civilization,
                                "player": paths.player,
                                "epoch": scope.epoch,
                                "activationId": scope.activation,
                                "agent": run.agent_name,
                                "runId": run.run_id,
                                "conversationId": run.conversation_id,
                                "startedAt": run.started_at.isoformat(),
                                "snapshotState": snapshot.state if snapshot is not None else None,
                                "history": str(history) if history is not None else None,
                                "run": str(folder / "run.json"),
                                "events": str(folder / "events.json"),
                                "toolEffects": str(folder / "tool-effects.json"),
                                "spills": spills,
                            }
                        )
                if notebooks:
                    scope = Scope(game=game, player=paths.player, epoch=selected_epoch, activation="operator-export")
                    exported_notes[paths.player] = await _notebooks(paths, scratch, target, scope)
            if not entries:
                raise ExportError("No recorded native SDK conversations match this selection.")
            if epoch_publication_basis(state_dir) != basis:
                raise ExportError("Native game publication changed during export; retry the selection.")
            write_private_json(
                destination / "index.json",
                {
                    "game": game,
                    "currentEpoch": current_epoch,
                    "epoch": selected_epoch,
                    "publicationSha256": basis.publication_sha256
                    if isinstance(basis, EpochPublicationBasis)
                    else digest(basis),
                    "conversations": entries,
                    "notebooks": exported_notes,
                    "consistency": "Independent committed SQLite backups; not a recovery checkpoint.",
                    "capture": __import__("json").loads((state_dir / "capture-metadata.json").read_text())
                    if (state_dir / "capture-metadata.json").exists()
                    else None,
                },
            )
            return entries
    except BaseException:
        shutil.rmtree(destination)
        raise


async def export_native_history(
    state_dir: Path,
    output_dir: Path,
    civilization: str | None = None,
    *,
    notebooks: bool = False,
    epoch: str | None = None,
    coordinated: bool = False,
) -> list[JsonObject]:
    from civ_agent.operator_views import passive_root

    with passive_root(state_dir, coordinated=coordinated, civilization=civilization) as captured:
        return await _export_native_history(captured, output_dir, civilization, notebooks=notebooks, epoch=epoch)
