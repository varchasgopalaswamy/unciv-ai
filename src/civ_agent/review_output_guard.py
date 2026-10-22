"""Bound structured assistant output without retaining drafts or raw reasoning."""

from dataclasses import dataclass

from civ_agent.protocol import JsonObject

REVIEW_OUTPUT_BYTES = 128 * 1024
REVIEW_WHITESPACE_CHARACTERS = 4096


@dataclass
class ReportStream:
    size: int = 0
    whitespace: int = 0
    in_string: bool = False
    escaped: bool = False

    def observe(self, text: str, max_bytes: int, max_whitespace: int) -> str | None:
        self.size += len(text.encode("utf-8", errors="replace"))
        if self.size > max_bytes:
            return "REVIEW_OUTPUT_LIMIT"
        for character in text:
            if self.in_string:
                if self.escaped:
                    self.escaped = False
                elif character == "\\":
                    self.escaped = True
                elif character == '"':
                    self.in_string = False
            elif character in " \n\r\t":
                self.whitespace += 1
                if self.whitespace > max_whitespace:
                    return "REVIEW_OUTPUT_STALLED"
            else:
                self.whitespace = 0
                self.in_string = character == '"'
        return None


class ReviewOutputGuard:
    """Observe only final or unphased assistant messages from one structured turn."""

    def __init__(
        self,
        thread_id: str,
        *,
        max_bytes: int = REVIEW_OUTPUT_BYTES,
        max_whitespace: int = REVIEW_WHITESPACE_CHARACTERS,
    ) -> None:
        self.thread_id = thread_id
        self.turn_id: str | None = None
        self.max_bytes, self.max_whitespace = max_bytes, max_whitespace
        self.streams: dict[tuple[str, str], ReportStream | None] = {}

    def bind_turn(self, turn_id: str) -> None:
        self.turn_id = turn_id
        self.streams = {key: stream for key, stream in self.streams.items() if key[0] == turn_id}

    def observe(self, method: str, params: JsonObject) -> str | None:
        if params.get("threadId") != self.thread_id:
            return None
        turn_id = params.get("turnId")
        if not isinstance(turn_id, str) or self.turn_id is not None and turn_id != self.turn_id:
            return None
        if method in {"item/started", "item/completed"}:
            item = params.get("item", {})
            if item.get("type") != "agentMessage" or not isinstance(item.get("id"), str):
                return None
            key = turn_id, item["id"]
            if item.get("phase") not in {None, "final_answer"}:
                self.streams[key] = None
                return None
            # A completed item contains the full message, not an additional delta.
            # Check it independently so missing starts/deltas cannot bypass limits.
            stream = ReportStream()
            self.streams[key] = stream
            text = item.get("text", "")
        elif method == "item/agentMessage/delta" and isinstance(params.get("itemId"), str):
            key = turn_id, params["itemId"]
            stream = self.streams.setdefault(key, ReportStream())
            text = params.get("delta", "")
        else:
            return None
        if stream is None or not isinstance(text, str):
            return None
        return stream.observe(text, self.max_bytes, self.max_whitespace)
