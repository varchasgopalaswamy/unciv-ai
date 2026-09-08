"""Cursor recovery and faithful, sanitized operator history rendering."""

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runner_history import EventHistory


class SchedulerHistoryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def event(self, sequence, kind, **data):
        return dict(sequence=sequence, time="2026-09-07T12:00:00Z", kind=kind, data=data)

    def test_delta_final_text_once_with_control_and_secret_redaction(self):
        capture = io.StringIO()
        secret = "A" * 43
        with contextlib.redirect_stdout(capture):
            history = EventHistory(self.root, "epoch", "Rome", "activation", (secret,))
            history.append([
                self.event(0, "user_message", text="Play turn 2."),
                self.event(1, "assistant_delta", itemId="m", text="Built "),
                self.event(2, "assistant_delta", itemId="m", text="a Farm.\x1b[31m"),
                self.event(3, "assistant_message", itemId="m", text="Built a Farm.\x1b[0m"),
                self.event(4, "reasoning_summary", itemId="s", text="Improve food first."),
                self.event(5, "reasoning_text", text="DO_NOT_PERSIST_RAW_REASONING"),
                self.event(6, "tool_result", result={"message": "Token " + secret, "encrypted_content": "PRIVATE_CIPHERTEXT"}),
            ])
        self.assertEqual(capture.getvalue().count("Built a Farm."), 1)
        markdown = history.markdown.read_text()
        self.assertEqual(markdown.count("Built a Farm."), 1)
        self.assertIn("Improve food first.", markdown)
        text = history.jsonl.read_text() + markdown + capture.getvalue()
        for forbidden in (secret, "DO_NOT_PERSIST_RAW_REASONING", "PRIVATE_CIPHERTEXT", "\x1b"):
            self.assertNotIn(forbidden, text)
        self.assertIn("[REDACTED]", text)
        self.assertEqual(os.stat(history.jsonl).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(history.markdown).st_mode & 0o777, 0o600)

    def test_cursor_survives_partial_append_and_does_not_reprint_old_events(self):
        with contextlib.redirect_stdout(io.StringIO()):
            first = EventHistory(self.root, "epoch", "Rome", "activation")
            first.append([self.event(0, "assistant_message", itemId="first", text="First visible message.")])
        with first.jsonl.open("ab") as stream:
            stream.write(b'{"sequence":1,"kind":')
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            resumed = EventHistory(self.root, "epoch", "Rome", "activation")
            self.assertEqual(resumed.cursor, 1)
            resumed.append([self.event(1, "assistant_message", itemId="second", text="Second visible message.")])
        self.assertNotIn("First visible message.", capture.getvalue())
        self.assertIn("Second visible message.", capture.getvalue())
        self.assertEqual(len(resumed.jsonl.read_text().splitlines()), 2)
        self.assertEqual(resumed.markdown.read_text().count("First visible message."), 1)

    def test_large_tool_result_chunks_reconstruct_across_restart(self):
        original = dict(itemId="tool", result={"largePayload": "full result " * 10000})
        encoded = json.dumps(original)
        middle = len(encoded) // 2
        chunks = [encoded[:middle], encoded[middle:]]
        with contextlib.redirect_stdout(io.StringIO()):
            first = EventHistory(self.root, "epoch", "Rome", "activation")
            first.append([self.event(0, "tool_result", itemId="tool", encoding="json", chunkIndex=0, chunkCount=2, text=chunks[0])])
            resumed = EventHistory(self.root, "epoch", "Rome", "activation")
            resumed.append([self.event(1, "tool_result", itemId="tool", encoding="json", chunkIndex=1, chunkCount=2, text=chunks[1])])
        self.assertIn(original["result"]["largePayload"], resumed.markdown.read_text())
        self.assertEqual(resumed.cursor, 2)


if __name__ == "__main__":
    unittest.main()
