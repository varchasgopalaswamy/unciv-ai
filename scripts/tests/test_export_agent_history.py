import importlib.machinery
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
loader = importlib.machinery.SourceFileLoader("history_export", str(SCRIPTS / "export_agent_history"))
spec = importlib.util.spec_from_loader(loader.name, loader)
exporter = importlib.util.module_from_spec(spec)
loader.exec_module(exporter)


class HistoryExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / "match"
        (self.state / "runner").mkdir(parents=True)
        self.codex = self.root / "codex"
        (self.codex / "sessions").mkdir(parents=True)
        (self.state / "runner/run.json").write_text(json.dumps({"epoch": "match-epoch", "sessions": {"Greece": {"threadId": "greece-thread"}}}))
        (self.state / "operator.json").write_text(json.dumps({"token": "SECRET-OPERATOR"}))
        self.source = self.codex / "sessions/rollout-greece-thread.jsonl"

    def tearDown(self):
        self.temporary.cleanup()

    def write_source(self, rows):
        records = [{"type": "session_meta", "payload": {"id": "greece-thread"}}]
        records += [{"type": "response_item", "timestamp": "2026-09-07T18:56:28Z", "payload": row} for row in rows]
        self.source.write_text("".join(json.dumps(row) + "\n" for row in records))

    def test_export_preserves_visible_history_and_filters_hidden_reasoning_and_credentials(self):
        self.write_source([
            {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "PRIVATE_DEVELOPER"}]},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>PRIVATE_ENV</environment_context>"}]},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "You are playing Unciv as Greece. Play turn 2."}]},
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "Checking available city actions."}], "content": [{"text": "PRIVATE_REASONING"}], "encrypted_content": "PRIVATE_ENCRYPTED"},
            {"type": "message", "role": "assistant", "channel": "analysis", "content": [{"type": "output_text", "text": "PRIVATE_ANALYSIS"}]},
            {"type": "custom_tool_call", "name": "exec", "call_id": "call-one", "input": "text(await tools.mcp__civ__player_turn({}));"},
            {"type": "custom_tool_call_output", "call_id": "call-one", "output": [{"type": "input_text", "text": "ACTIVE SECRET-OPERATOR"}]},
            {"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": "The previous turn ended; turn 2 is still active."}]},
        ])
        original = self.source.read_bytes()
        result = exporter.export_history(self.state, self.codex, self.root / "output", "greece")
        history = Path(result[0]["history"]).read_text()
        events = Path(result[0]["events"]).read_text()
        self.assertIn("Checking available city actions.", history)
        self.assertIn("turn 2 is still active", history)
        self.assertIn("mcp__civ__player_turn", history)
        self.assertIn("2026-09-07T18:56:28Z", events)
        self.assertEqual(1, result[0]["reasoningSummaries"])
        for hidden in ["PRIVATE_DEVELOPER", "PRIVATE_ENV", "PRIVATE_REASONING", "PRIVATE_ENCRYPTED", "PRIVATE_ANALYSIS", "SECRET-OPERATOR"]:
            self.assertNotIn(hidden, events + history)
        self.assertEqual(original, self.source.read_bytes())
        self.assertEqual(0o600, Path(result[0]["events"]).stat().st_mode & 0o777)

    def test_exports_only_saved_conversations_and_keeps_previous_exports(self):
        self.write_source([{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Greece report"}]}])
        (self.codex / "sessions/unrelated.jsonl").write_text("PRIVATE_UNRELATED_SESSION")
        one = exporter.export_history(self.state, self.codex, self.root / "output")
        two = exporter.export_history(self.state, self.codex, self.root / "output")
        self.assertNotEqual(one[0]["events"], two[0]["events"])
        self.assertTrue(Path(one[0]["events"]).exists())
        self.assertNotIn("PRIVATE_UNRELATED_SESSION", Path(two[0]["events"]).read_text())
        with self.assertRaises(exporter.ExportError):
            exporter.export_history(self.state, self.codex, self.root / "output", "Rome")

    def test_mismatched_conversation_identity_is_rejected(self):
        self.source.write_text(json.dumps({"type": "session_meta", "payload": {"id": "someone-else"}}) + "\n")
        with self.assertRaises(exporter.ExportError):
            exporter.export_history(self.state, self.codex, self.root / "output")
