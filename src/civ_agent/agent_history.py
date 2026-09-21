"""Durable operator-visible Codex events. Raw model reasoning is never collected."""

import datetime
import hashlib
import json
import os
import re
import stat
from collections.abc import Iterable
from pathlib import Path

from civ_agent.mcp_results import canonical_tool_result
from civ_agent.protocol import EventPage
from civ_agent.tls import private_directory, regular_file, write_private

MAX_EVENT_BYTES = 64 * 1024
MAX_PAGE_BYTES = 256 * 1024
MAX_PENDING_BYTES = 1024 * 1024
HISTORY_METHODS = frozenset(
    {
        "item/started",
        "item/completed",
        "item/agentMessage/delta",
        "item/reasoning/summaryTextDelta",
        "item/commandExecution/outputDelta",
        "item/mcpToolCall/progress",
        "error",
        "turn/completed",
    }
)
SECRET_KEYS = re.compile(r"token|authorization|password|secret|api.?key", re.I)
ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")


def plain_text(value):
    value = ANSI.sub("", value)
    return "".join(c for c in value if c in "\n\t" or ord(c) >= 32 and ord(c) != 127)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def visible_item(method, params):
    """An allowlist of user-visible fields; never serialize a raw ThreadItem."""
    item = params.get("item", {})
    item_type = item.get("type")
    item_id = item.get("id")
    completed = method == "item/completed"
    if item_type == "agentMessage" and completed:
        return "assistant_message", {"itemId": item_id, "text": item.get("text", ""), "phase": item.get("phase")}
    if item_type == "plan" and completed:
        return "assistant_message", {"itemId": item_id, "text": item.get("text", ""), "phase": "plan"}
    if item_type == "reasoning" and completed:
        # `content` holds raw reasoning. Only the separately exposed summary is permitted.
        summary = item.get("summary", [])
        text = "\n\n".join(value for value in summary if isinstance(value, str)) if isinstance(summary, list) else ""
        return ("reasoning_summary", {"itemId": item_id, "text": text}) if text else None
    if item_type == "mcpToolCall":
        if completed:
            return "tool_result", {
                "itemId": item_id,
                "type": item_type,
                "status": item.get("status"),
                "result": canonical_tool_result(item.get("result")),
                "error": item.get("error"),
            }
        return "tool_call", {
            "itemId": item_id,
            "type": item_type,
            "server": item.get("server"),
            "tool": item.get("tool"),
            "arguments": item.get("arguments"),
        }
    if item_type == "commandExecution":
        if completed:
            return "tool_result", {
                "itemId": item_id,
                "type": item_type,
                "status": item.get("status"),
                "output": item.get("aggregatedOutput"),
                "exitCode": item.get("exitCode"),
            }
        return "tool_call", {"itemId": item_id, "type": item_type, "command": item.get("command")}
    if item_type == "dynamicToolCall":
        if completed:
            return "tool_result", {
                "itemId": item_id,
                "type": item_type,
                "status": item.get("status"),
                "result": item.get("contentItems"),
                "success": item.get("success"),
            }
        return "tool_call", {
            "itemId": item_id,
            "type": item_type,
            "tool": item.get("tool"),
            "arguments": item.get("arguments"),
        }
    if item_type == "functionCallOutput" and completed:
        return "tool_result", {
            "itemId": item_id,
            "type": item_type,
            "tool": item.get("name"),
            "output": item.get("output"),
        }
    if item_type == "fileChange":
        return ("tool_result" if completed else "tool_call"), {
            "itemId": item_id,
            "type": item_type,
            "status": item.get("status"),
            "changes": item.get("changes"),
        }
    return None


class HistoryLog:
    def __init__(self, directory: Path, secrets: Iterable[str] = (), *, auto_render: bool = True) -> None:
        self.directory = private_directory(directory)
        self.path = self.directory / "events.jsonl"
        self.markdown = self.directory / "history.md"
        self.secrets = set(secret for secret in secrets if secret)
        self.sequence = 0
        self.auto_render = auto_render
        self.rendered_sequence = -1
        self.last_lifecycle = None
        self.turn_id = None
        self.pending = []
        self.pending_bytes = 0
        self.streams = {}
        self.items = set()
        if self.path.exists():
            regular_file(self.path, private=True)
            with self.path.open("rb") as stream:
                offset = 0
                for line in stream:
                    if not line.endswith(b"\n"):
                        # A crash can leave only the final append incomplete. Never accept
                        # that partial event, and continue with its unconsumed sequence.
                        descriptor = os.open(self.path, os.O_RDWR | os.O_NOFOLLOW)
                        try:
                            os.ftruncate(descriptor, offset)
                            os.fsync(descriptor)
                        finally:
                            os.close(descriptor)
                        break
                    event = json.loads(line)
                    if event["sequence"] != self.sequence:
                        raise ValueError("Non-contiguous history sequence")
                    self.sequence = event["sequence"] + 1
                    if event["kind"] == "lifecycle":
                        self.last_lifecycle = event["data"]
                    offset += len(line)
            if self.auto_render:
                self.render()

    def sanitize(self, value, depth=0):
        if depth > 20:
            return "[Nested value omitted]"
        if isinstance(value, str):
            for secret in sorted(self.secrets, key=len, reverse=True):
                value = value.replace(secret, "[REDACTED]")
            value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9_.~-]+", "Bearer [REDACTED]", value)
            value = re.sub(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])", "[REDACTED]", value)
            return value
        if isinstance(value, list):
            return [self.sanitize(item, depth + 1) for item in value]
        if isinstance(value, dict):
            return {
                self.sanitize(str(key), depth + 1): "[REDACTED]"
                if SECRET_KEYS.search(str(key))
                else self.sanitize(item, depth + 1)
                for key, item in value.items()
            }
        return value if value is None or isinstance(value, (bool, int, float)) else type(value).__name__

    def append(self, kind, data, event_time=None):
        self._append(kind, self.sanitize(data), event_time)

    def _append(self, kind, data, event_time=None, render=True):
        event = {
            "sequence": self.sequence,
            "time": event_time or datetime.datetime.now(datetime.UTC).isoformat(),
            "kind": kind,
            "data": data,
        }
        events = [event]
        if len(encoded(event)) > MAX_EVENT_BYTES:
            serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            chunks = [serialized[index : index + 8192] for index in range(0, len(serialized), 8192)]
            events = []
            for index, chunk in enumerate(chunks):
                metadata = {
                    key: data[key]
                    for key in ("itemId", "turnId", "type", "status")
                    if key in data and len(encoded(data[key])) < 256
                }
                metadata.update({"text": chunk, "chunkIndex": index, "chunkCount": len(chunks), "encoding": "json"})
                events.append({**event, "sequence": self.sequence + index, "data": metadata})
        # Commit all chunks from one accepted event together; each still has a
        # bounded size and its own cursor, but does not need a separate fsync.
        raw = b"".join(encoded(item) + b"\n" for item in events)
        descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "ab") as stream:
            if stat.S_IMODE(os.fstat(stream.fileno()).st_mode) & ~0o600:
                raise ValueError("History file must have owner-only permissions")
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        self.sequence += len(events)
        # The JSONL is authoritative. The Markdown is a readable derivative which is
        # rebuilt at lifecycle boundaries and after completed items, never fed to agents.
        if render and self.auto_render and kind not in {"assistant_delta", "reasoning_summary_delta", "tool_output"}:
            self.render()

    def delta(self, kind, data):
        key = (kind, data.get("itemId"), data.get("summaryIndex"))
        prior = self.streams.get(key, {**data, "text": ""})
        combined = self.sanitize(prior["text"] + data.get("text", ""))
        keep = max((len(secret) for secret in self.secrets), default=1) - 1
        shown, trailing = (combined[:-keep], combined[-keep:]) if keep and len(combined) > keep else ("", combined)
        self.streams[key] = {**data, "text": trailing}
        if shown:
            self.append(kind, {**data, "text": shown})

    def flush(self, item_id=None):
        for key, data in list(self.streams.items()):
            if item_id is not None and data.get("itemId") != item_id:
                continue
            text = data["text"]
            # A interrupted stream may end partway through a credential. Do not persist
            # a nearly complete prefix which would make guessing its final bytes easy.
            for secret in self.secrets:
                for length in range(len(secret) - 1, 7, -1):
                    if text.endswith(secret[:length]):
                        text = text[:-length] + "[REDACTED]"
                        break
            if text:
                self.append(key[0], {**data, "text": text})
            del self.streams[key]

    def accept(self, turn_id, kind, data):
        if self.turn_id is None:
            size = len(encoded(data))
            if self.pending_bytes + size <= MAX_PENDING_BYTES:
                self.pending.append((turn_id, kind, self.sanitize(data)))
                self.pending_bytes += size
            else:
                raise ValueError("Visible event buffer overflow before turn binding")
            return
        if turn_id != self.turn_id:
            return
        data = {**data, "turnId": turn_id}
        if kind in {"assistant_delta", "reasoning_summary_delta", "tool_output"}:
            self.delta(kind, data)
        else:
            identity = (kind, data.get("itemId"))
            if data.get("itemId") and identity in self.items:
                return
            if data.get("itemId"):
                self.items.add(identity)
                self.flush(data["itemId"])
            self.append(kind, data)

    def bind_turn(self, turn_id):
        self.turn_id = turn_id
        pending, self.pending = self.pending, []
        self.pending_bytes = 0
        for candidate, kind, data in pending:
            self.accept(candidate, kind, data)

    def render(self):
        if self.rendered_sequence == self.sequence:
            return
        events = []
        if self.path.exists():
            with self.path.open(encoding="utf-8") as stream:
                events = [json.loads(line) for line in stream]
        reassembled = []
        index = 0
        while index < len(events):
            event = events[index]
            data = event["data"]
            if data.get("encoding") == "json" and data.get("chunkIndex") == 0:
                count = data["chunkCount"]
                chunks = events[index : index + count]
                if len(chunks) == count and all(
                    part["kind"] == event["kind"] and part["data"].get("chunkIndex") == offset
                    for offset, part in enumerate(chunks)
                ):
                    event = {**event, "data": json.loads("".join(part["data"]["text"] for part in chunks))}
                    index += count - 1
            reassembled.append(event)
            index += 1
        finalized = {(event["kind"], event["data"].get("itemId")) for event in reassembled}
        parts = ["# Agent history\n\nReasoning entries are the summaries exposed by Codex, not raw model reasoning.\n"]
        for event in reassembled:
            kind, data = event["kind"], event["data"]
            if kind == "assistant_delta" and ("assistant_message", data.get("itemId")) in finalized:
                continue
            if kind == "reasoning_summary_delta" and ("reasoning_summary", data.get("itemId")) in finalized:
                continue
            label = kind.replace("_", " ").capitalize()
            value = data.get("text") if "text" in data else json.dumps(data, ensure_ascii=False, indent=2)
            parts.append(f"\n## {label} · {plain_text(event['time'])}\n\n{plain_text(str(value))}\n")
        write_private(self.markdown, "".join(parts).encode())
        self.rendered_sequence = self.sequence

    def page(self, cursor: int, limit: int) -> EventPage:
        result = []
        size = 128
        next_cursor = cursor
        if self.path.exists():
            regular_file(self.path, private=True)
            with self.path.open("rb") as stream:
                for line in stream:
                    event = json.loads(line)
                    if event["sequence"] < cursor:
                        continue
                    if len(result) >= limit or size + len(line) > MAX_PAGE_BYTES:
                        break
                    result.append(event)
                    size += len(line)
                    next_cursor = event["sequence"] + 1
        return {"events": result, "nextCursor": next_cursor, "hasMore": next_cursor < self.sequence, "available": True}


class HistoryStore:
    def __init__(self, directory, service_token, *, auto_render=True):
        self.directory, self.service_token = Path(directory), service_token
        self.auto_render = auto_render
        self.logs = {}

    def path(self, activation_id):
        return self.directory / hashlib.sha256(activation_id.encode()).hexdigest()

    def begin(self, record, prompt, player_token):
        log = HistoryLog(
            self.path(record["activationId"]), (self.service_token, player_token), auto_render=self.auto_render
        )
        self.logs[record["activationId"]] = log
        if prompt is not None:
            log.append("user_message", {"text": prompt})
        self.lifecycle(record)

    def lifecycle(self, record):
        log = self.logs.get(record["activationId"])
        if log is None:
            directory = self.path(record["activationId"])
            if not (directory / "events.jsonl").exists():
                return
            log = self.logs.setdefault(
                record["activationId"], HistoryLog(directory, (self.service_token,), auto_render=self.auto_render)
            )
        data = {key: record[key] for key in ("phase", "threadId", "error", "aborted") if key in record}
        if data != log.last_lifecycle:
            if data["phase"] in {"FAILED", "INTERRUPTED", "COMPLETED"}:
                log.flush()
            log.append("lifecycle", data)
            log.last_lifecycle = data

    def startup_context(self, activation_id, text, *, role="user"):
        """Retain the exact extra context supplied before the first model request."""
        if role not in {"developer", "user"}:
            raise ValueError("INVALID_PROMPT_ROLE")
        self.logs[activation_id].append(role + "_message", {"text": text, "source": "service_startup"})

    def handoff_response(self, activation_id, call_id, name, arguments, result):
        """Keep a response intentionally withheld to prevent another model request."""
        log = self.logs[activation_id]
        item = {
            "itemId": "handoff:" + call_id,
            "type": "mcpToolCall",
            "source": "service_handoff",
            "deliveredToModel": False,
        }
        log.append("tool_call", {**item, "server": "civ", "tool": name, "arguments": arguments})
        log.append("tool_result", {**item, "result": {"content": [], "structuredContent": result, "isError": False}})

    def action(self, activation_id, call_id, kind, description):
        """Record a native receipt's description for observers, outside model context."""
        self.logs[activation_id].append(
            "action",
            {"itemId": "action:" + call_id, "source": "service_receipt", "kind": kind, "description": description},
        )

    def bind_turn(self, activation_id, turn_id):
        log = self.logs.get(activation_id)
        if log is not None:
            log.bind_turn(turn_id)

    def notify(self, activation_id, thread_id, method, params):
        log = self.logs.get(activation_id)
        if log is None or params.get("threadId") != thread_id or not thread_id:
            return
        result = None
        if method in {"item/started", "item/completed"}:
            result = visible_item(method, params)
        elif method in {
            "item/agentMessage/delta",
            "item/reasoning/summaryTextDelta",
            "item/commandExecution/outputDelta",
            "item/mcpToolCall/progress",
        }:
            kind = {
                "item/agentMessage/delta": "assistant_delta",
                "item/reasoning/summaryTextDelta": "reasoning_summary_delta",
                "item/commandExecution/outputDelta": "tool_output",
                "item/mcpToolCall/progress": "tool_output",
            }[method]
            result = kind, {"itemId": params.get("itemId"), "text": params.get("delta", params.get("message", ""))}
            if kind == "reasoning_summary_delta":
                result[1]["summaryIndex"] = params.get("summaryIndex", 0)
        elif method == "error":
            error = params.get("error", {})
            result = (
                "error",
                {
                    "message": error.get("message", "Codex reported an error"),
                    "willRetry": params.get("willRetry", False),
                },
            )
        elif method == "turn/completed" and isinstance(params.get("turn", {}).get("error"), dict):
            turn = params["turn"]
            result = "error", {"message": turn["error"].get("message", "Codex turn failed"), "willRetry": False}
            params = {**params, "turnId": turn.get("id")}
        if result is not None and isinstance(params.get("turnId"), str):
            log.accept(params["turnId"], *result)

    def page(self, activation_id: str, cursor: int = 0, limit: int = 100) -> EventPage:
        if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid event cursor or limit")
        log = self.logs.get(activation_id)
        if log is None:
            directory = self.path(activation_id)
            if not (directory / "events.jsonl").exists():
                return {"events": [], "nextCursor": cursor, "hasMore": False, "available": False}
            log = self.logs.setdefault(
                activation_id, HistoryLog(directory, (self.service_token,), auto_render=self.auto_render)
            )
        if cursor > log.sequence:
            raise ValueError("Event cursor exceeds history")
        return log.page(cursor, limit)
