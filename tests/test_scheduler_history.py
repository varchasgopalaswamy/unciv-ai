"""Cursor recovery and faithful, sanitized operator history rendering."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from civ_agent.runner_history import EventHistory


class SchedulerHistoryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "monitoring.sqlite3"

    def event(self, sequence, kind, **data):
        return dict(sequence=sequence, time="2026-09-07T12:00:00Z", kind=kind, data=data)

    def test_delta_final_text_once_with_control_and_secret_redaction(self):
        capture = io.StringIO()
        secret = "A" * 43
        with contextlib.redirect_stdout(capture):
            history = EventHistory(self.root, "epoch", "Rome", "activation", (secret,), game_turn=0)
            history.append(
                [
                    self.event(0, "user_message", text="Play turn 2."),
                    self.event(1, "assistant_delta", itemId="m", text="Built "),
                    self.event(2, "assistant_delta", itemId="m", text="a Farm.\x1b[31m"),
                    self.event(3, "assistant_message", itemId="m", text="Built a Farm.\x1b[0m"),
                    self.event(4, "reasoning_summary_delta", itemId="s", text="Improve food first."),
                    self.event(5, "reasoning_summary", itemId="s", text="Improve food first."),
                    self.event(6, "reasoning_text", text="DO_NOT_PERSIST_RAW_REASONING"),
                    self.event(7, "tool_call", itemId="tool", name="player_units", arguments={}),
                    self.event(8, "tool_output", itemId="tool", text="Worker ready.\n"),
                    self.event(
                        9,
                        "tool_result",
                        result={"message": "Token " + secret, "encrypted_content": "PRIVATE_CIPHERTEXT"},
                    ),
                ]
            )
        self.assertEqual(capture.getvalue().count("Built a Farm."), 1)
        markdown = history.markdown()
        self.assertEqual(markdown.count("Built a Farm."), 1)
        self.assertIn("Improve food first.", markdown)
        self.assertIn("Worker ready.", markdown)
        self.assertIn("player_units", markdown)
        for hidden in ("Improve food first.", "Worker ready.", "player_units", "Tool result:"):
            self.assertNotIn(hidden, capture.getvalue())
        text = json.dumps(history.events) + markdown + capture.getvalue()
        for forbidden in (secret, "DO_NOT_PERSIST_RAW_REASONING", "PRIVATE_CIPHERTEXT", "\x1b"):
            self.assertNotIn(forbidden, text)
        self.assertIn("[REDACTED]", text)
        self.assertEqual(os.stat(history.store.path).st_mode & 0o777, 0o600)

    def test_cursor_survives_restart_and_does_not_reprint_old_events(self):
        with contextlib.redirect_stdout(io.StringIO()):
            first = EventHistory(self.root, "epoch", "Rome", "activation", game_turn=0)
            first.append([self.event(0, "assistant_message", itemId="first", text="First visible message.")])
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            resumed = EventHistory(self.root, "epoch", "Rome", "activation", game_turn=0)
            self.assertEqual(resumed.cursor, 1)
            resumed.append([self.event(1, "assistant_message", itemId="second", text="Second visible message.")])
        self.assertNotIn("First visible message.", capture.getvalue())
        self.assertIn("Second visible message.", capture.getvalue())
        self.assertEqual(len(resumed.events), 2)
        self.assertEqual(resumed.markdown().count("First visible message."), 1)

    def test_large_tool_result_chunks_reconstruct_across_restart(self):
        original = dict(itemId="tool", result={"largePayload": "full result " * 10000})
        encoded = json.dumps(original)
        middle = len(encoded) // 2
        chunks = [encoded[:middle], encoded[middle:]]
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            first = EventHistory(self.root, "epoch", "Rome", "activation", game_turn=0)
            first.append(
                [
                    self.event(
                        0, "tool_result", itemId="tool", encoding="json", chunkIndex=0, chunkCount=2, text=chunks[0]
                    )
                ]
            )
            resumed = EventHistory(self.root, "epoch", "Rome", "activation", game_turn=0, verbose=True)
            resumed.append(
                [
                    self.event(
                        1, "tool_result", itemId="tool", encoding="json", chunkIndex=1, chunkCount=2, text=chunks[1]
                    )
                ]
            )
        self.assertIn("[Rome] Tool result:", capture.getvalue())
        self.assertIn(original["result"]["largePayload"], resumed.markdown())
        self.assertEqual(resumed.cursor, 2)
