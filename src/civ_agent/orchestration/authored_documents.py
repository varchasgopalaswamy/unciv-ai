"""Read-only native notebook documents for synchronous game-domain assessments."""

from copy import deepcopy
from pathlib import Path

from anyio.from_thread import start_blocking_portal
from pydantic_ai_harness.memory import SqliteMemoryStore

from civ_agent.orchestration.authored_memory import (
    MAX_DOCUMENT_CHARS,
    MAX_DOCUMENTS,
    AuthoredMetadata,
    authored_prefix,
    decode_document,
)
from civ_agent.orchestration.contracts import Scope
from civ_agent.orchestration.store import JournalConflict


class AuthoredDocuments:
    def __init__(self, database: Path):
        self.store = SqliteMemoryStore(database=database)
        self.context = start_blocking_portal()
        self.portal = self.context.__enter__()

    def close(self):
        self.context.__exit__(None, None, None)

    def read(self, scope: Scope, turn: int | None = None):
        return deepcopy(self.portal.call(self._read, scope, turn))

    async def _read(self, scope, turn):
        prefix = authored_prefix(scope)
        paths = await self.store.list_paths(prefix, limit=MAX_DOCUMENTS + 1)
        if len(paths) > MAX_DOCUMENTS:
            raise JournalConflict("AUTHORED_DOCUMENT_LIMIT")
        records = {}
        for path in paths:
            file = await self.store.read(path, max_chars=MAX_DOCUMENT_CHARS)
            if file is None:
                continue
            metadata = AuthoredMetadata.model_validate_strings(dict(file.metadata or {}))
            if (metadata.game, metadata.player, metadata.epoch) != (scope.game, scope.player, scope.epoch):
                raise JournalConflict("AUTHORED_METADATA_SCOPE_CONFLICT")
            record = decode_document(path, file)
            expiry = record["body"].get("expiresTurn")
            authored = record["body"].get("basis", {}).get("turn")
            if type(turn) is int and (
                type(expiry) is int
                and expiry <= turn
                or record["category"] == "handoff"
                and type(authored) is int
                and authored + 1 < turn
            ):
                record["body"].update(state="retired", retirementReason="expired")
            records[path.removeprefix(prefix)] = record
        return records
