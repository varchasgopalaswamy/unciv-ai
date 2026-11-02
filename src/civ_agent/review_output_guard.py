"""Bound assistant output using a short suffix, without collecting raw reasoning."""

from dataclasses import dataclass

from civ_agent.protocol import JsonObject

REVIEW_OUTPUT_BYTES = 128 * 1024
REVIEW_WHITESPACE_CHARACTERS = 4096
REVIEW_REPETITION_CHARACTERS = 4096
REVIEW_REPETITION_PERIOD = 64


@dataclass
class ReportStream:
    size: int = 0
    whitespace: int = 0
    in_string: bool = False
    escaped: bool = False
    suffix: str = ""

    def observe(self, text: str, max_bytes: int, max_whitespace: int) -> str | None:
        self.size += len(text.encode("utf-8", errors="replace"))
        if self.size > max_bytes:
            return "REVIEW_OUTPUT_LIMIT"
        self.suffix = (self.suffix + text)[-REVIEW_REPETITION_CHARACTERS:]
        if len(self.suffix) == REVIEW_REPETITION_CHARACTERS and any(
            self.suffix[period:] == self.suffix[:-period] for period in range(1, REVIEW_REPETITION_PERIOD + 1)
        ):
            return "REVIEW_OUTPUT_STALLED"
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
    """Bound every assistant message in a structured turn, regardless of phase."""

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
        self.streams: dict[tuple[str, str], ReportStream] = {}

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
        if not isinstance(text, str):
            return None
        return stream.observe(text, self.max_bytes, self.max_whitespace)
