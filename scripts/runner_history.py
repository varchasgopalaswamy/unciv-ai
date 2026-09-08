"""Private, restart-safe mirrors of the agent service's exposed event stream."""

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import unicodedata


KINDS = {
    "user_message", "assistant_delta", "assistant_message", "reasoning_summary_delta", "reasoning_summary",
    "tool_call", "tool_result", "tool_output", "lifecycle", "error",
}
PRIVATE_FIELDS = {"encrypted_content", "encryptedContent", "raw_reasoning", "rawReasoning", "reasoning_text", "reasoningText"}
ANSI = re.compile(r"\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-_])")


class HistoryError(Exception):
    pass


def visible_text(value):
    value = ANSI.sub("", value).replace("\r\n", "\n").replace("\r", "\n")
    return "".join(character for character in value if character in "\n\t" or unicodedata.category(character) not in {"Cc", "Cf", "Cs"})


def clean(value, secrets):
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return visible_text(value)
    if isinstance(value, dict):
        return {visible_text(str(key)): clean(item, secrets) for key, item in value.items() if key not in PRIVATE_FIELDS}
    if isinstance(value, list):
        return [clean(item, secrets) for item in value]
    return value


def atomic_text(path, text):
    descriptor, temporary = tempfile.mkstemp(prefix=".history-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def message_key(event):
    kind, data = event["kind"], event.get("data", {})
    if kind in {"assistant_delta", "assistant_message"}:
        return "Assistant", data.get("itemId", "")
    if kind in {"reasoning_summary_delta", "reasoning_summary"}:
        return "Reasoning summary", data.get("itemId", "")
    if kind == "tool_output":
        return "Tool output", data.get("itemId", "")
    return None


class Chunks:
    def __init__(self, secrets=()):
        self.pending = None
        self.secrets = secrets

    def expand(self, event):
        data = event.get("data", {})
        if data.get("encoding") != "json" or "chunkIndex" not in data:
            return event
        index, count = data.get("chunkIndex"), data.get("chunkCount")
        if type(index) is not int or type(count) is not int or not 0 <= index < count or not isinstance(data.get("text"), str):
            raise HistoryError("Invalid chunked history event.")
        if index == 0:
            self.pending = dict(event=event, fragments=[], count=count)
        if self.pending is None or len(self.pending["fragments"]) != index or self.pending["count"] != count:
            raise HistoryError("Chunked history events are out of order.")
        self.pending["fragments"].append(data["text"])
        if index + 1 != count:
            return None
        combined = dict(self.pending["event"])
        try:
            combined["data"] = clean(json.loads("".join(self.pending["fragments"])), self.secrets)
        except (ValueError, UnicodeError):
            raise HistoryError("Chunked history could not be decoded.") from None
        self.pending = None
        return combined


class Messages:
    def __init__(self):
        self.text = {}
        self.summary_index = {}

    def update(self, event):
        key = message_key(event)
        if key is None:
            return None, None
        value = event.get("data", {}).get("text", "")
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        previous = self.text.get(key, "")
        if event["kind"] in {"assistant_message", "reasoning_summary"}:
            if not value and previous:
                return key, ""
            self.text[key] = value
            return key, value[len(previous):] if value.startswith(previous) else value
        if event["kind"] == "reasoning_summary_delta":
            index = event["data"].get("summaryIndex", 0)
            if key in self.summary_index and self.summary_index[key] != index:
                value = "\n\n" + value
            self.summary_index[key] = index
        self.text[key] = previous + value
        return key, value


class LiveOutput:
    def __init__(self, civilization, secrets=()):
        self.civilization = visible_text(civilization)
        self.messages = Messages()
        self.open_key = None
        self.chunks = Chunks(secrets)

    def finish_line(self):
        if self.open_key is not None:
            print(flush=True)
            self.open_key = None

    def line(self, label, text):
        self.finish_line()
        for line in text.splitlines() or [""]:
            print(f"[{self.civilization}] {label}: {line}", flush=True)

    def consume(self, event, emit=True):
        event = self.chunks.expand(event)
        if event is None:
            return
        key, delta = self.messages.update(event)
        if not emit:
            return
        data, kind = event.get("data", {}), event["kind"]
        if key is not None:
            if delta:
                if self.open_key != key:
                    self.finish_line()
                for segment in delta.splitlines(keepends=True):
                    if self.open_key is None:
                        print(f"[{self.civilization}] {key[0]}: ", end="", flush=True)
                    print(segment, end="", flush=True)
                    self.open_key = None if segment.endswith("\n") else key
            if kind in {"assistant_message", "reasoning_summary"}:
                self.finish_line()
            return
        if kind == "user_message":
            self.line("Instructions", str(data.get("text", "")))
        elif kind in {"tool_call", "tool_result"}:
            label = "Tool call" if kind == "tool_call" else "Tool result"
            text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            self.line(label, text[:1200] + (" … (full result in history)" if len(text) > 1200 else ""))
        elif kind in {"lifecycle", "error"}:
            self.line("Status" if kind == "lifecycle" else "Error", json.dumps(data, ensure_ascii=False))


class EventHistory:
    def __init__(self, root, epoch, civilization, activation, secrets=()):
        digest = lambda value: hashlib.sha256(value.encode()).hexdigest()
        self.directory = Path(root) / digest(epoch) / digest(civilization) / digest(activation)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.jsonl = self.directory / "events.jsonl"
        self.markdown = self.directory / "history.md"
        self.secrets = tuple(secrets)
        self.output = LiveOutput(civilization, secrets)
        self.events = []
        self.source = dict(epoch=epoch, civilization=civilization, activationId=activation)
        if self.jsonl.exists():
            lines = self.jsonl.read_bytes().splitlines(keepends=True)
            for index, line in enumerate(lines):
                if not line.endswith(b"\n") and index == len(lines) - 1:
                    # A killed append may leave only part of the last event. The
                    # preceding complete JSONL records are the durable cursor.
                    atomic_text(self.jsonl, b"".join(lines[:index]).decode("utf-8"))
                    break
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict) or event.get("sequence") != len(self.events):
                        raise ValueError()
                except (ValueError, UnicodeError):
                    raise HistoryError("History is corrupt; preserve it and inspect the service's original event log.") from None
                self.events.append(event)
                self.output.consume(event, emit=False)
        atomic_text(self.directory / "source.json", json.dumps(self.source, ensure_ascii=True) + "\n")
        self.write_markdown()

    @property
    def cursor(self):
        return len(self.events)

    def append(self, events):
        accepted = []
        for event in events:
            if not isinstance(event, dict) or type(event.get("sequence")) is not int:
                raise HistoryError("Invalid event sequence from agent service.")
            sequence = event["sequence"]
            if sequence < self.cursor:
                continue
            if sequence != self.cursor + len(accepted):
                raise HistoryError("Agent event history has a gap; stopped mirroring to preserve its cursor.")
            sanitized = clean(event, self.secrets)
            if sanitized.get("kind") not in KINDS:
                sanitized = dict(sequence=sequence, time=sanitized.get("time"), kind="omitted", data={})
            if not isinstance(sanitized.get("data"), dict):
                raise HistoryError("Invalid event payload from agent service.")
            accepted.append(sanitized)
        if not accepted:
            return
        descriptor = os.open(self.jsonl, os.O_APPEND | os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            for event in accepted:
                stream.write(json.dumps(event, ensure_ascii=True, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.events.extend(accepted)
        self.write_markdown()
        for event in accepted:
            self.output.consume(event)

    def write_markdown(self):
        messages = Messages()
        chunks = Chunks(self.secrets)
        blocks, indices = [], {}
        for event in self.events:
            event = chunks.expand(event)
            if event is None:
                continue
            key, _delta = messages.update(event)
            if key is not None:
                if key not in indices:
                    indices[key] = len(blocks)
                    blocks.append("")
                blocks[indices[key]] = f"**{key[0]}**\n\n{messages.text[key]}"
                continue
            data, kind = event.get("data", {}), event.get("kind")
            if kind == "user_message":
                blocks.append("**Instructions**\n\n" + str(data.get("text", "")))
            elif kind in {"tool_call", "tool_result", "lifecycle", "error"}:
                text = json.dumps(data, ensure_ascii=False, indent=2)
                fence = "`" * max(3, max((len(match.group()) + 1 for match in re.finditer(r"`+", text)), default=3))
                blocks.append(f"**{kind.replace('_', ' ').capitalize()}**\n\n{fence}json\n{text}\n{fence}")
        header = f"# {visible_text(self.source['civilization'])} — activation {visible_text(self.source['activationId'])}\n\n"
        atomic_text(self.markdown, header + "\n\n".join(blocks) + "\n")
