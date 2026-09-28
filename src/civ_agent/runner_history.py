"""Operator event persistence and console rendering, without automatic reports."""

import json
import re

from sqlalchemy import select

from civ_agent.model_status import status_text
from civ_agent.monitoring import MonitorStore, get_record, put_record, read_database, record_key, records
from civ_agent.terminal_output import TerminalOutput, visible_text

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


class EventHistory:
    def __init__(self, path, epoch, civilization, activation, secrets=(), *, game_turn, verbose=False, renderer=None):
        if type(game_turn) is not int or game_turn < 0:
            raise HistoryError("History game turn must be a nonnegative integer.")
        if not all(isinstance(value, str) and value for value in (epoch, civilization, activation)):
            raise HistoryError("Invalid history identity.")
        self.source = dict(epoch=epoch, civilization=civilization, activationId=activation, gameTurn=game_turn)
        self.identity = dict(epoch=epoch, civilization=civilization, activation=activation, game_turn=game_turn)
        self.key = record_key(epoch, civilization, activation)
        self.store = MonitorStore(path)
        self.secrets = tuple(secrets)
        self.output = LiveOutput(
            civilization,
            secrets,
            verbose=verbose,
            renderer=renderer,
            owner=(epoch, civilization, activation),
            history_path=path,
        )
        with self.store.engine.begin() as db:
            source = get_record(db, "streams", self.key)
            if source is not None and source != self.source:
                raise HistoryError("History belongs to another turn; preserve its original identity.")
            put_record(db, "streams", self.key, self.source, **self.identity)
        self.cursor = 0
        # Restore only the console decoder state. There is no second in-memory
        # copy of the complete transcript and no Markdown rebuild on each page.
        for event in self.events:
            if event["sequence"] != self.cursor:
                raise HistoryError("History has a sequence gap.")
            self.output.consume(event, emit=False)
            self.cursor += 1

    @property
    def events(self):
        with read_database(self.store.path) as db:
            values = db.scalars(
                select(records.c.payload)
                .where(
                    records.c.collection == "events",
                    records.c.epoch == self.source["epoch"],
                    records.c.civilization == self.source["civilization"],
                    records.c.activation == self.source["activationId"],
                )
                .order_by(records.c.sequence)
            )
            return [json.loads(value) for value in values]

    def append(self, events):
        accepted = []
        for event in events:
            if not isinstance(event, dict) or type(event.get("sequence")) is not int:
                raise HistoryError("Invalid event sequence from agent service.")
            sequence = event["sequence"]
            if sequence < self.cursor:
                continue
            if sequence != self.cursor + len(accepted):
                raise HistoryError("Agent event history has a gap; its cursor was not advanced.")
            sanitized = clean(event, self.secrets)
            if sanitized.get("kind") not in KINDS:
                sanitized = dict(sequence=sequence, time=sanitized.get("time"), kind="omitted", data={})
            if not isinstance(sanitized.get("data"), dict):
                raise HistoryError("Invalid event payload from agent service.")
            accepted.append(sanitized)
        if not accepted:
            return
        with self.store.engine.begin() as db:
            for event in accepted:
                put_record(
                    db,
                    "events",
                    record_key(self.key, event["sequence"]),
                    event,
                    sequence=event["sequence"],
                    **self.identity,
                )
        self.cursor += len(accepted)
        for event in accepted:
            self.output.consume(event)

    def markdown(self):
        return render_markdown(self.source, self.events)


def render_markdown(source, events):
    messages = Messages()
    chunks = Chunks()
    blocks, indices = [], {}
    for event in events:
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
    turn = f" — turn {source['gameTurn']}"
    header = f"# {visible_text(source['civilization'])}{turn} — activation {visible_text(source['activationId'])}\n\n"
    return header + "\n\n".join(blocks) + "\n"
