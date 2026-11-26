"""Native-store snapshots; orchestration owns quiescence and epoch publication."""

import hashlib
import os
import re
import sqlite3
import tempfile
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager, closing
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator
from pydantic_ai_harness.memory import MemoryFile, MemoryOperation, MemoryStore, MetadataMemoryStore
from pydantic_ai_harness.planning import PlanItem, PlanStore

from civ_agent.orchestration.authored_memory import (
    MAX_DOCUMENT_CHARS,
    MAX_DOCUMENTS,
    AuthoredMetadata,
    authored_prefix,
    decode_document,
)
from civ_agent.orchestration.contracts import Digest, OpaqueId, PlayerRole, Scope, digest
from civ_agent.orchestration.store import JournalConflict
from civ_agent.schemas import BoundaryModel, NonnegativeInt
from civ_agent.tls import private_directory


class SavedMemoryDocument(BoundaryModel):
    name: Annotated[str, Field(min_length=1, max_length=80)]
    content: Annotated[str, Field(max_length=MAX_DOCUMENT_CHARS)]
    source_version: Annotated[str, Field(min_length=1)]
    metadata: AuthoredMetadata

    @field_validator("name")
    @classmethod
    def filename(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.md", value) is None or ".." in value:
            raise ValueError("RECOVERY_DOCUMENT_PATH_CONFLICT")
        return value


class NativeRecoverySnapshot(BoundaryModel):
    version: Literal[1] = 1
    checkpoint_id: OpaqueId
    source: Scope
    documents: Annotated[list[SavedMemoryDocument], Field(max_length=MAX_DOCUMENTS)]
    plans: dict[PlayerRole, list[PlanItem]]

    @field_validator("plans", mode="before")
    @classmethod
    def persisted_task_ids(cls, value):
        # Native PlanItem intentionally creates IDs for new model-written tasks.
        # A checkpoint must retain its existing IDs rather than generate new ones.
        if isinstance(value, dict):
            for items in value.values():
                if not isinstance(items, list):
                    continue
                ids = [
                    item.get("id") if isinstance(item, dict) else item.id if isinstance(item, PlanItem) else None
                    for item in items
                ]
                if any(not isinstance(id, str) or not id for id in ids) or len(set(ids)) != len(ids):
                    raise ValueError("RECOVERY_PLAN_ID_CONFLICT")
        return value

    @model_validator(mode="after")
    def source_binding(self) -> Self:
        if len({document.name for document in self.documents}) != len(self.documents):
            raise ValueError("RECOVERY_DOCUMENT_DUPLICATE")
        for document in self.documents:
            metadata = document.metadata
            if (metadata.game, metadata.player, metadata.epoch) != (
                self.source.game,
                self.source.player,
                self.source.epoch,
            ):
                raise ValueError("RECOVERY_DOCUMENT_SCOPE_CONFLICT")
            decode_document(
                # The shared policy validates typed strategy/city documents;
                # generic native Markdown remains exact untrusted text.
                authored_prefix(self.source) + document.name,
                MemoryFile(
                    content=document.content,
                    version=document.source_version,
                    operation_id=None,
                    truncated=False,
                    metadata=metadata.strings(),
                ),
            )
        return self


async def export_native_snapshot(
    checkpoint_id: str,
    source: Scope,
    memory: MemoryStore,
    plans: Mapping[PlayerRole, PlanStore],
) -> NativeRecoverySnapshot:
    """Read under the caller's settled checkpoint barrier; no store is mutated."""
    prefix = authored_prefix(source)
    paths = await memory.list_paths(prefix, limit=MAX_DOCUMENTS + 1)
    if len(paths) > MAX_DOCUMENTS:
        raise JournalConflict("AUTHORED_DOCUMENT_LIMIT")
    documents = []
    for path in paths:
        if not path.startswith(prefix):
            raise JournalConflict("RECOVERY_DOCUMENT_SCOPE_CONFLICT")
        file = await memory.read(path, max_chars=MAX_DOCUMENT_CHARS)
        if file is None or file.truncated or file.metadata is None:
            raise JournalConflict("RECOVERY_DOCUMENT_CHANGED")
        documents.append(
            SavedMemoryDocument(
                name=path.removeprefix(prefix),
                content=file.content,
                source_version=file.version,
                metadata=AuthoredMetadata.model_validate_strings(dict(file.metadata)),
            )
        )
    tasks = {role: await store.get_items() for role, store in plans.items()}
    return NativeRecoverySnapshot(checkpoint_id=checkpoint_id, source=source, documents=documents, plans=tasks)


async def install_native_snapshot(
    snapshot: NativeRecoverySnapshot,
    target: Scope,
    memory: MetadataMemoryStore,
    plans: Mapping[PlayerRole, PlanStore],
    *,
    permit: Callable[[], AbstractAsyncContextManager[None]],
) -> None:
    """Install into a fresh epoch while a trusted restore permit is held.

    The permit must fence admission for the entire context, allow retries only
    while this restore remains unfinished, and reject entry once its new root
    has been admitted. Native PlanStore has no replay receipt; this admission
    fence is necessary even when all document writes can replay their receipts.
    The trusted caller supplies PlanStores resolved for target and their roles;
    the native PlanStore protocol does not expose backend tenant identifiers.
    WorkflowStore, StepPersistence, budgets and old mutation receipts stay intact.
    """
    # Revalidate the external persistence boundary, including mutable nested
    # native PlanItems, before the first mutation or acquisition of the permit.
    snapshot = NativeRecoverySnapshot.model_validate_json(snapshot.model_dump_json())
    target = Scope.model_validate_json(target.model_dump_json())
    if (
        (snapshot.source.game, snapshot.source.player) != (target.game, target.player)
        or snapshot.source.epoch == target.epoch
        or set(snapshot.plans) != set(plans)
    ):
        raise JournalConflict("RECOVERY_TARGET_SCOPE_CONFLICT")
    prefix = authored_prefix(target)
    async with permit():
        for document in snapshot.documents:
            metadata = document.metadata.model_copy(
                update={"epoch": target.epoch, "originEpoch": document.metadata.originEpoch or snapshot.source.epoch}
            ).strings()
            await memory.write_with_metadata(
                prefix + document.name,
                document.content,
                expected_version=None,
                metadata=metadata,
                operation=MemoryOperation(
                    id=digest(["restore-authored", snapshot.checkpoint_id, prefix, document.name]),
                    fingerprint=digest([document.content, document.source_version, metadata]),
                ),
            )
        for role, items in snapshot.plans.items():
            await plans[role].set_items(items)


class SQLiteBackup(BoundaryModel):
    version: Literal[1] = 1
    filename: str
    sha256: Digest
    bytes: NonnegativeInt


def backup_sqlite(source: Path, destination: Path) -> SQLiteBackup:
    """Publish a standard SQLite online backup, including committed WAL data."""
    if source.is_symlink() or not source.is_file() or destination.exists() or destination.is_symlink():
        raise ValueError("RECOVERY_BACKUP_PATH_CONFLICT")
    private_directory(destination.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=".sqlite-backup-", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(temporary)
    try:
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as reader:
            with closing(sqlite3.connect(temporary)) as writer:
                reader.backup(writer)
                if writer.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise ValueError("RECOVERY_BACKUP_INVALID")
        with temporary.open("rb") as saved:
            os.fsync(saved.fileno())
        # Publish without replacing an earlier manifest's immutable component.
        os.link(temporary, destination)
        directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return SQLiteBackup(
            filename=destination.name,
            sha256=hashlib.sha256(destination.read_bytes()).hexdigest(),
            bytes=destination.stat().st_size,
        )
    finally:
        temporary.unlink(missing_ok=True)
