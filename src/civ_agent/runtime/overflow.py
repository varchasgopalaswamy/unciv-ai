"""Private, owner-bound overflow payloads retained alongside SDK histories."""

import asyncio
import base64
import json
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from civ_agent.orchestration.contracts import canonical, digest
from civ_agent.orchestration.store import JournalConflict
from civ_agent.orchestration.transcripts import scope_of
from civ_agent.tls import TLSConfigError, private_directory, regular_file, write_private_json


@dataclass(frozen=True)
class ScopedOverflowStore:
    root: Path
    owner: dict

    @classmethod
    def for_owner(cls, root: Path, deps):
        scope = scope_of(deps)
        owner = {
            "scope": scope.model_dump(mode="json"),
            "role": getattr(deps, "purpose", getattr(deps, "phase", "report")),
            "job": getattr(deps, "job", scope.activation),
            "attempt": getattr(deps, "attempt_offset", 0),
            "model_step_offset": getattr(deps, "model_step_offset", 0),
        }
        return cls(root, owner)

    @property
    def directory(self):
        return self.root / digest(self.owner)

    def _write(self, key: str, data: bytes) -> str:
        handle = "spill_" + digest([self.owner, key])
        private_directory(self.root)
        directory = private_directory(self.directory)
        path = directory / (handle + ".json")
        body = {
            "version": 1,
            "owner": self.owner,
            "handle": handle,
            "sha256": sha256(data).hexdigest(),
            "data": base64.b64encode(data).decode("ascii"),
        }
        if path.exists() or path.is_symlink():
            regular_file(path, private=True)
            if json.loads(path.read_bytes()) != body:
                raise JournalConflict("OVERFLOW_WRITE_CONFLICT")
        write_private_json(path, body)
        return handle

    async def write(self, key: str, data: bytes) -> str:
        try:
            return await asyncio.to_thread(self._write, key, data)
        except (JournalConflict, TLSConfigError, KeyError, UnicodeError, json.JSONDecodeError) as error:
            from civ_agent.runtime.code_mode import _active_fence

            primary = error if isinstance(error, JournalConflict) else JournalConflict("OVERFLOW_STORE_INTEGRITY")
            fence = _active_fence.get()
            if fence is not None and fence.primary is None:
                # Harness deliberately catches spill-store failures. Retain
                # integrity failures outside that catch so it cannot downgrade
                # a corrupt retained history to ordinary unavailability.
                fence.primary = primary
            raise primary from None

    def _read(self, handle: str) -> bytes:
        if re.fullmatch(r"spill_[a-f0-9]{64}", handle) is None:
            raise FileNotFoundError("OVERFLOW_HANDLE_UNAVAILABLE")
        private_directory(self.root)
        private_directory(self.directory)
        path = self.directory / (handle + ".json")
        if not path.exists() and not path.is_symlink():
            raise FileNotFoundError("OVERFLOW_HANDLE_UNAVAILABLE")
        regular_file(path, private=True)
        body = json.loads(path.read_bytes())
        if body.get("version") != 1 or body.get("owner") != self.owner or body.get("handle") != handle:
            raise JournalConflict("OVERFLOW_OWNER_CONFLICT")
        data = base64.b64decode(body["data"], validate=True)
        if sha256(data).hexdigest() != body["sha256"]:
            raise JournalConflict("OVERFLOW_CONTENT_CONFLICT")
        return data

    async def read(self, handle: str) -> bytes:
        try:
            return await asyncio.to_thread(self._read, handle)
        except (TLSConfigError, KeyError, UnicodeError, json.JSONDecodeError):
            raise JournalConflict("OVERFLOW_STORE_INTEGRITY") from None


def export_spills(root: Path, destination: Path, scope, *, role: str | None = None):
    """Validate every selected envelope; never copy arbitrary paths from a history."""
    if not root.exists():
        return []
    private_directory(root)
    exported = []
    for directory in sorted(root.iterdir()):
        private_directory(directory)
        for source in sorted(directory.glob("spill_*.json")):
            regular_file(source, private=True)
            body = json.loads(source.read_bytes())
            owner = body["owner"]
            if owner["scope"] != scope.model_dump(mode="json") or role is not None and owner["role"] != role:
                continue
            store = ScopedOverflowStore(root, owner)
            if directory != store.directory:
                raise JournalConflict("OVERFLOW_DIRECTORY_CONFLICT")
            store._read(body["handle"])
            target = private_directory(destination) / source.name
            write_private_json(target, body)
            exported.append({"handle": body["handle"], "path": str(target), "owner": canonical(owner)})
    return exported
