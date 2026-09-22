"""Private, restart-safe mirrors of the agent service's exposed event stream."""

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from civ_agent.model_status import status_text
from civ_agent.terminal_output import TerminalOutput, visible_text
from civ_agent.tls import TLSConfigError, private_directory, regular_file
from civ_agent.turn_archive import civilization_directory_name

KINDS = {
    "developer_message",
    "user_message",
    "assistant_delta",
    "assistant_message",
    "reasoning_summary_delta",
    "reasoning_summary",
    "tool_call",
    "tool_result",
    "tool_output",
    "lifecycle",
    "error",
    "action",
    "model_status",
}
PRIVATE_FIELDS = {
    "encrypted_content",
    "encryptedContent",
    "raw_reasoning",
    "rawReasoning",
    "reasoning_text",
    "reasoningText",
}


class HistoryError(Exception):
    pass


def clean(value, secrets):
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return visible_text(value)
    if isinstance(value, dict):
        return {
            visible_text(str(key)): clean(item, secrets) for key, item in value.items() if key not in PRIVATE_FIELDS
        }
    if isinstance(value, list):
        return [clean(item, secrets) for item in value]
    return value


def atomic_text(path, text):
    if path.exists() or path.is_symlink():
        history_file(path)
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
        if (
            type(index) is not int
            or type(count) is not int
            or not 0 <= index < count
            or not isinstance(data.get("text"), str)
        ):
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
            return key, value[len(previous) :] if value.startswith(previous) else value
        if event["kind"] == "reasoning_summary_delta":
            index = event["data"].get("summaryIndex", 0)
            if key in self.summary_index and self.summary_index[key] != index:
                value = "\n\n" + value
            self.summary_index[key] = index
        self.text[key] = previous + value
        return key, value


class LiveOutput:
    def __init__(self, civilization, secrets=(), *, verbose=False, renderer=None, owner=None, history_path=None):
        self.civilization = visible_text(civilization)
        self.verbose = verbose
        self.messages = Messages()
        self.chunks = Chunks(secrets)
        self.renderer = renderer or TerminalOutput()
        self.owner = owner or object()
        self.history_path = history_path
        self.actions = set()

    def finish_line(self):
        self.renderer.finish_line(self.owner)

    def line(self, label, text):
        self.renderer.line(label, text, civilization=self.civilization)

    def error(self, text):
        self.line("Error", text)
        if self.history_path is not None:
            self.line("History", str(self.history_path))

    def consume(self, event, emit=True):
        event = self.chunks.expand(event)
        if event is None:
            return
        key, delta = self.messages.update(event)
        data, kind = event.get("data", {}), event["kind"]
        if kind == "action":
            identity = data.get("itemId", event.get("sequence"))
            if identity in self.actions:
                return
            self.actions.add(identity)
            if emit:
                self.line("Action", str(data.get("description", "Command confirmed.")))
            return
        if not emit:
            return
        if kind == "tool_result":
            result = data.get("result")
            payload = result.get("structuredContent", {}) if isinstance(result, dict) else {}
            failure = data.get("error") or (payload.get("error") if isinstance(payload, dict) else None)
            if failure or (isinstance(result, dict) and result.get("isError")) or data.get("status") == "failed":
                self.error(visible_text(json.dumps(failure or result or data, ensure_ascii=False))[:600])
        if (
            not self.verbose
            and kind in {"developer_message", "user_message"}
            and data.get("source") == "service_startup"
        ):
            return
        if not self.verbose and kind in {
            "tool_call",
            "tool_result",
            "tool_output",
            "reasoning_summary",
            "reasoning_summary_delta",
        }:
            return
        if key is not None:
            self.renderer.stream(
                self.owner,
                key,
                self.civilization,
                key[0],
                delta or "",
                final=kind in {"assistant_message", "reasoning_summary"},
            )
            return
        if kind in {"developer_message", "user_message"}:
            self.line(
                "Developer instructions" if kind == "developer_message" else "Instructions", str(data.get("text", ""))
            )
        elif kind in {"tool_call", "tool_result"}:
            label = "Tool call" if kind == "tool_call" else "Tool result"
            text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            self.line(label, text[:1200] + (" … (full result in history)" if len(text) > 1200 else ""))
        elif kind == "error":
            if data.get("willRetry"):
                self.line("Warning", "Model retry: " + str(data.get("message", "Connection interrupted")))
            else:
                self.error(json.dumps(data, ensure_ascii=False))
        elif kind == "model_status":
            state = data.get("state")
            label = (
                "Error" if state == "failed" else "Warning" if state in {"waiting", "retrying", "warning"} else "Status"
            )
            self.line(label, status_text(data))
        elif kind == "lifecycle":
            phase = str(data.get("phase", "Status")).replace("_", " ").capitalize()
            if data.get("error"):
                self.error(phase + ": " + str(data["error"]))
            else:
                self.line("Status", phase)


def history_file(path):
    try:
        return regular_file(path, private=True)
    except TLSConfigError:
        raise HistoryError("History files must be regular owner-only files, without symbolic links.") from None


def history_directory(path):
    try:
        return private_directory(path)
    except TLSConfigError:
        raise HistoryError("History paths must be owner-only directories, without symbolic links.") from None


def bind_source(path, expected):
    if path.exists() or path.is_symlink():
        history_file(path)
        try:
            if json.loads(path.read_text(encoding="utf-8")) != expected:
                raise ValueError()
        except (ValueError, UnicodeError):
            raise HistoryError(
                "History belongs to a different match, player or activation; preserve it and choose a new history directory."
            ) from None
    else:
        atomic_text(path, json.dumps(expected, ensure_ascii=True) + "\n")


class EventHistory:
    def __init__(
        self, root, epoch, civilization, activation, secrets=(), *, game_turn=None, verbose=False, renderer=None
    ):
        def digest(value: str) -> str:
            return hashlib.sha256(value.encode()).hexdigest()

        self.source = dict(epoch=epoch, civilization=civilization, activationId=activation)
        self.turn_directory: Path | None = None
        if game_turn is None:
            # Keep the original location for an already persisted activation whose
            # scheduler state predates explicit game-turn metadata.
            self.directory = history_directory(root)
            for component in (digest(epoch), digest(civilization), digest(activation)):
                self.directory = history_directory(self.directory / component)
        else:
            if type(game_turn) is not int or game_turn < 0:
                raise HistoryError("History game turn must be a nonnegative integer.")
            try:
                player_name = civilization_directory_name(civilization)
                attempt_name = civilization_directory_name(activation)
            except ValueError:
                raise HistoryError("Invalid player or activation name for history.") from None
            player_directory = history_directory(history_directory(root) / player_name)
            bind_source(player_directory / "source.json", dict(version=2, epoch=epoch, civilization=civilization))
            self.turn_directory = history_directory(player_directory / f"turn_{game_turn}")
            self.directory = history_directory(self.turn_directory / f"attempt_{attempt_name}")
            self.source["gameTurn"] = game_turn
        self.jsonl = self.directory / "events.jsonl"
        self.markdown = self.directory / "history.md"
        self.secrets = tuple(secrets)
        self.output = LiveOutput(
            civilization,
            secrets,
            verbose=verbose,
            renderer=renderer,
            owner=(epoch, civilization, activation),
            history_path=self.markdown,
        )
        self.events = []
        bind_source(self.directory / "source.json", self.source)
        if self.jsonl.exists() or self.jsonl.is_symlink():
            history_file(self.jsonl)
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
                    raise HistoryError(
                        "History is corrupt; preserve it and inspect the service's original event log."
                    ) from None
                self.events.append(event)
                self.output.consume(event, emit=False)
        self.write_markdown()

    def publish_archive(self, archive):
        """Mirror finalized player records fetched from its authenticated service."""
        if self.turn_directory is None:
            raise HistoryError("A game turn is required to mirror a player archive.")
        if not isinstance(archive, dict) or (
            type(archive.get("gameTurn")) is not int
            or archive["gameTurn"] != self.source["gameTurn"]
            or archive.get("civilization") != self.source["civilization"]
            or not isinstance(archive.get("commands"), str)
            or not isinstance(archive.get("summary"), str)
        ):
            raise HistoryError("Player archive does not match the requested civilization and turn.")
        records = []
        try:
            for line in archive["commands"].splitlines():
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError()
                records.append(clean(record, self.secrets))
        except (ValueError, UnicodeError):
            raise HistoryError("Player command archive is not valid JSONL.") from None
        from civ_agent.memory_records import TurnSummary

        try:
            data = TurnSummary.model_validate(archive["summaryData"]).model_dump(exclude_none=True)
            if data.get("turn") != archive["gameTurn"]:
                raise ValueError()
        except (KeyError, ValueError):
            raise HistoryError("Invalid structured turn summary") from None
        commands = "".join(json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n" for record in records)
        summary = clean(archive["summary"], self.secrets)
        atomic_text(self.turn_directory / "commands.jsonl", commands)
        atomic_text(self.turn_directory / "summary.md", summary)
        atomic_text(self.turn_directory / "summary.json", json.dumps(clean(data, self.secrets), indent=2) + "\n")

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
        if self.jsonl.exists() or self.jsonl.is_symlink():
            history_file(self.jsonl)
        descriptor = os.open(self.jsonl, os.O_APPEND | os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
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
            if kind in {"developer_message", "user_message"}:
                label = "Developer instructions" if kind == "developer_message" else "Instructions"
                blocks.append(f"**{label}**\n\n" + str(data.get("text", "")))
            elif kind in {"tool_call", "tool_result", "lifecycle", "error", "action", "model_status"}:
                text = json.dumps(data, ensure_ascii=False, indent=2)
                fence = "`" * max(3, max((len(match.group()) + 1 for match in re.finditer(r"`+", text)), default=3))
                blocks.append(f"**{kind.replace('_', ' ').capitalize()}**\n\n{fence}json\n{text}\n{fence}")
        turn = f" — turn {self.source['gameTurn']}" if "gameTurn" in self.source else ""
        header = f"# {visible_text(self.source['civilization'])}{turn} — activation {visible_text(self.source['activationId'])}\n\n"
        atomic_text(self.markdown, header + "\n\n".join(blocks) + "\n")
