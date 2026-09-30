"""A bounded preview of already-filtered, operator-visible review history."""

from collections import OrderedDict

MAX_TRACE_ITEMS = 64
MAX_TRACE_CHARACTERS = 32768


class ReviewTrace:
    def __init__(self):
        self.items = OrderedDict()
        self.truncated = False

    def append(self, event):
        kind, data = event["kind"], event["data"]
        item = data.get("itemId", event["sequence"])
        part = 0
        delta = False
        if kind in {"assistant_delta", "assistant_message"}:
            label, text = "Assistant", data.get("text", "")
            delta = kind == "assistant_delta"
        elif kind in {"reasoning_summary_delta", "reasoning_summary"}:
            label, text = "Reasoning summary", data.get("text", "")
            delta = kind == "reasoning_summary_delta"
            part = data.get("summaryIndex", 0)
            if not delta:
                for key in list(self.items):
                    if key[:2] == (label, item) and key[2] != 0:
                        del self.items[key]
        elif kind in {"tool_call", "tool_result"}:
            label = "Tool"
            previous = self.items.get((label, item, 0))
            text = str(data.get("tool") or data.get("type") or "Tool")
            if kind == "tool_result":
                text = previous["text"] if previous else text
                status = (
                    "failed" if data.get("error") or data.get("success") is False else data.get("status", "completed")
                )
                text += " · " + str(status)
        elif kind in {"model_status", "error"}:
            label, text = "Status", data.get("message", "")
        elif kind == "lifecycle":
            label, text = "Status", str(data.get("phase", "")).capitalize()
            if data.get("error"):
                text += ": " + str(data["error"])
        else:
            return
        key = label, item, part
        previous = self.items.get(key)
        if delta and previous:
            text = previous["text"] + text
        self.items[key] = {"label": label, "text": text, "time": previous["time"] if previous else event["time"]}
        while len(self.items) > MAX_TRACE_ITEMS:
            self.items.popitem(last=False)
            self.truncated = True
        excess = sum(len(value["text"]) for value in self.items.values()) - MAX_TRACE_CHARACTERS
        while excess > 0:
            first = next(iter(self.items))
            value = self.items[first]
            count = len(value["text"])
            if count <= excess:
                del self.items[first]
            else:
                value["text"] = value["text"][excess:]
            excess -= count
            self.truncated = True

    def snapshot(self):
        return {"entries": [dict(value) for value in self.items.values()], "truncated": self.truncated}
