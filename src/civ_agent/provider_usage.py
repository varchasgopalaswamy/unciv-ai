"""Recover provider usage counters without retaining provider conversation content."""

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from civ_agent.protocol import JsonObject
from civ_agent.telemetry import TOKEN_KEYS

SAFE_THREAD = re.compile(r"[A-Za-z0-9_-]{1,128}")
USAGE_RECORD = re.compile(r'"type"\s*:\s*"token_usage_record"')


class ProviderUsageReader:
    """Read only token-usage records from one closed Codex session trace."""

    def __init__(self, sessions_root: Path):
        self.sessions_root = sessions_root

    def _session(self, thread_id: str) -> Path | None:
        if SAFE_THREAD.fullmatch(thread_id) is None or not self.sessions_root.is_dir():
            return None
        try:
            identity = UUID(thread_id)
            if identity.version != 7 or str(identity) != thread_id.lower():
                return None
            # Codex UUIDv7 thread IDs carry their creation time. Search the UTC
            # date and its neighbors because session folders may use local time.
            created = datetime.fromtimestamp(int(identity.hex[:12], 16) / 1000, UTC).date()
            directories = [
                self.sessions_root / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
                for day in (created + timedelta(days=offset) for offset in (-1, 0, 1))
            ]
            candidates = [
                path
                for directory in directories
                if directory.is_dir()
                for path in directory.glob(f"*-{thread_id}.jsonl")
            ]
        except (OSError, ValueError):
            return None
        return candidates[0] if len(candidates) == 1 and not candidates[0].is_symlink() else None

    def read(self, thread_id: str | None) -> JsonObject | None:
        if not isinstance(thread_id, str):
            return None
        path = self._session(thread_id)
        if path is None:
            return None
        totals = dict.fromkeys(TOKEN_KEYS, 0)
        responses: dict[str, tuple[int, ...]] = {}
        try:
            with path.open() as stream:
                for line in stream:
                    # JSON string contents escape their quotes, so this filter
                    # avoids even decoding ordinary conversation-content rows.
                    if USAGE_RECORD.search(line) is None:
                        continue
                    row = json.loads(line)
                    if row.get("type") != "token_usage_record":
                        continue
                    payload = row.get("payload")
                    if not isinstance(payload, dict) or payload.get("thread_id") != thread_id:
                        return None
                    response, usage = payload.get("response_id"), payload.get("usage")
                    if not isinstance(response, str) or not isinstance(usage, dict):
                        return None
                    values = tuple(usage.get(key) for key in TOKEN_KEYS)
                    if any(type(value) is not int or value < 0 for value in values):
                        return None
                    if response in responses:
                        if responses[response] != values:
                            return None
                        continue
                    responses[response] = values
                    for key, value in zip(TOKEN_KEYS, values, strict=True):
                        totals[key] += value
        except (OSError, ValueError, TypeError):
            return None
        if not responses:
            return None
        return {"usage": totals, "modelRequests": len(responses)}


def reconcile_provider_usage(record: JsonObject, reader: ProviderUsageReader) -> bool:
    """Merge an authoritative closed trace into a local lifecycle record."""
    recovered = reader.read(record.get("threadId"))
    if recovered is None:
        return False
    usage = record.setdefault("usage", {})
    for key in TOKEN_KEYS:
        usage[key] = max(usage.get(key, 0), recovered["usage"][key])
    # Response IDs make this count exact; replace any live notification estimate.
    record["modelRequests"] = recovered["modelRequests"]
    record["usageReported"] = True
    record["providerUsageReconciled"] = True
    return True
