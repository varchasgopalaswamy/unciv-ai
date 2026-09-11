import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

import aiohttp
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_history import HistoryLog, HistoryStore, MAX_EVENT_BYTES, MAX_PAGE_BYTES, canonical_tool_result, encoded
from agent_service import AgentService, server_ssl_context
from unciv_tls import client_ssl_context, ensure_server_certificate


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="agent-history-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.service_token, self.player_token = "S" * 43, "P" * 43
        self.record = {"activationId": "activation", "epoch": "epoch", "civilization": "Rome", "threadId": "thread", "phase": "STARTING"}
        self.store = HistoryStore(self.root / "history", self.service_token)
        self.store.begin(self.record, "Play a turn " + self.player_token, self.player_token)

    def notify(self, method, **parameters):
        self.store.notify("activation", "thread", method, {"threadId": "thread", "turnId": "turn", **parameters})

    def terminal(self):
        self.record["phase"] = "COMPLETED"
        self.store.lifecycle(self.record)

    def test_visible_messages_summaries_and_tools_retained_without_raw_reasoning_or_tokens(self):
        self.store.bind_turn("activation", "turn")
        self.notify("item/reasoning/textDelta", itemId="reasoning", delta="RAW-REASONING-SECRET")
        self.notify("rawResponseItem/completed", item={"type": "reasoning", "encrypted_content": "ENCRYPTED-REASONING-SECRET"})
        self.notify("item/reasoning/summaryTextDelta", itemId="reasoning", summaryIndex=0, delta="I will improve the capital's food output.")
        self.notify("item/completed", item={"id": "reasoning", "type": "reasoning", "summary": ["A food improvement will help the capital grow."], "content": ["RAW-CONTENT-SECRET"], "encrypted_content": "ENCRYPTED-CONTENT-SECRET"})
        self.notify("item/started", item={"id": "tool", "type": "mcpToolCall", "server": "civ", "tool": "player_turn", "arguments": {"authorization": "Bearer " + self.service_token, "nested": {"token": self.player_token}}})
        self.notify("item/completed", item={"id": "tool", "type": "mcpToolCall", "status": "completed", "result": {"content": [{"type": "text", "text": "Ready " + self.player_token}]}, "error": None})
        self.notify("item/agentMessage/delta", itemId="answer", delta="My worker is improving the farm. " + self.player_token[:20])
        self.notify("item/agentMessage/delta", itemId="answer", delta=self.player_token[20:] + " I selected Pottery.")
        self.notify("item/completed", item={"id": "answer", "type": "agentMessage", "text": "Farm started; Pottery selected. " + self.player_token, "phase": "final_answer"})
        self.terminal()
        log = self.store.logs["activation"]
        serialized = log.path.read_text()
        markdown = log.markdown.read_text()
        for secret in (self.player_token, self.service_token, "RAW-REASONING-SECRET", "RAW-CONTENT-SECRET", "ENCRYPTED-CONTENT-SECRET", "ENCRYPTED-REASONING-SECRET"):
            self.assertNotIn(secret, serialized + markdown)
        self.assertIn("[REDACTED]", serialized)
        self.assertIn("Reasoning summary", markdown)
        self.assertIn("Farm started; Pottery selected.", markdown)
        self.assertNotIn("My worker is improving the farm.", markdown)
        self.assertIn("player_turn", markdown)
        kinds = [event["kind"] for event in self.store.page("activation")["events"]]
        self.assertIn("reasoning_summary_delta", kinds)
        self.assertIn("reasoning_summary", kinds)
        self.assertIn("assistant_delta", kinds)
        self.assertIn("assistant_message", kinds)
        self.assertIn("tool_call", kinds)
        self.assertIn("tool_result", kinds)
        self.assertEqual(0o600, os.stat(log.path).st_mode & 0o777)
        self.assertEqual(0o600, os.stat(log.markdown).st_mode & 0o777)

    def test_turn_binding_drops_replayed_old_and_foreign_conversation_events(self):
        self.notify("item/completed", turnId="old-turn", item={"id": "old", "type": "agentMessage", "text": "OLD-COMPLETION"})
        self.notify("item/completed", threadId="foreign-thread", item={"id": "foreign", "type": "agentMessage", "text": "FOREIGN-COMPLETION"})
        self.notify("item/completed", item={"id": "actual", "type": "agentMessage", "text": "ACTUAL-COMPLETION"})
        self.store.bind_turn("activation", "turn")
        self.terminal()
        text = self.store.logs["activation"].path.read_text()
        self.assertIn("ACTUAL-COMPLETION", text)
        self.assertNotIn("OLD-COMPLETION", text)
        self.assertNotIn("FOREIGN-COMPLETION", text)

    def test_large_results_are_lossless_chunked_and_pages_bounded_and_recoverable(self):
        log = self.store.logs["activation"]
        data = {"itemId": "huge", "type": "mcpToolCall", "result": {"content": [{"type": "text", "text": "雪🐈" * 50000}], "tiles": list(range(5000))}}
        log.append("tool_result", data, event_time="2026-09-08T00:00:00Z")
        self.terminal()
        cursor = 0
        all_events = []
        pages = 0
        while True:
            page = self.store.page("activation", cursor, 100)
            self.assertLessEqual(len(encoded(page)), MAX_PAGE_BYTES)
            self.assertTrue(all(len(encoded(event)) <= MAX_EVENT_BYTES for event in page["events"]))
            all_events.extend(page["events"])
            cursor = page["nextCursor"]
            pages += 1
            if not page["hasMore"]:
                break
        self.assertGreater(pages, 1)
        chunks = [event for event in all_events if event["kind"] == "tool_result"]
        self.assertEqual(data, json.loads("".join(event["data"]["text"] for event in chunks)))
        self.assertEqual(list(range(len(all_events))), [event["sequence"] for event in all_events])
        self.assertIn("2026-09-08T00:00:00Z", log.markdown.read_text())
        self.assertIn("4999", log.markdown.read_text())
        restored = HistoryStore(self.root / "history", self.service_token)
        self.assertEqual(self.store.page("activation", 0, 2), restored.page("activation", 0, 2))
        for cursor, limit in [(-1, 1), (True, 1), (999999, 1), (0, 0), (0, 101)]:
            with self.assertRaises(ValueError):
                restored.page("activation", cursor, limit)

    def test_interrupted_partial_secret_and_partial_journal_line_recover_safely(self):
        self.store.bind_turn("activation", "turn")
        self.notify("item/agentMessage/delta", itemId="answer", delta="Beginning. " + self.player_token[:40])
        self.record["phase"] = "INTERRUPTED"
        self.store.lifecycle(self.record)
        log = self.store.logs["activation"]
        self.assertNotIn(self.player_token[:40], log.path.read_text())
        before = log.sequence
        with log.path.open("ab") as stream:
            stream.write(b'{"sequence":999,"kind":"partial')
        restored = HistoryLog(log.directory, (self.service_token,))
        self.assertEqual(before, restored.sequence)
        restored.append("error", {"message": "Recovered"})
        self.assertEqual(before + 1, restored.sequence)
        self.assertNotIn('"partial', restored.path.read_text())

    def test_duplicate_mcp_payload_is_stored_once_with_metadata_and_distinct_content_preserved(self):
        self.store.bind_turn("activation", "turn")
        payload = {"city": "Rome", "gold": 5000, "token": self.player_token}
        duplicate = {"type": "text", "text": json.dumps(payload, indent=2)}
        note = {"type": "text", "text": "Read this additional confirmation."}
        annotated = {**duplicate, "annotations": {"audience": ["user"]}}
        result = {"structuredContent": payload, "content": [duplicate, note, annotated], "isError": False, "_meta": {"example": "retained"}}
        original = json.dumps(result)
        self.notify("item/completed", item={"id": "receipt", "type": "mcpToolCall", "status": "completed", "result": result})
        self.assertEqual(original, json.dumps(result), "Archiving must not modify the live response")
        stored = next(e["data"]["result"] for e in self.store.page("activation")["events"] if e["kind"] == "tool_result")
        self.assertEqual({"city": "Rome", "gold": 5000, "token": "[REDACTED]"}, stored["structuredContent"])
        self.assertEqual(2, len(stored["content"]))
        self.assertEqual(note, stored["content"][0])
        self.assertEqual(annotated["annotations"], stored["content"][1]["annotations"])
        self.assertEqual(result["_meta"], stored["_meta"])
        self.assertNotIn(self.player_token, self.store.logs["activation"].path.read_text())

    def test_deduplication_never_discards_malformed_or_different_json(self):
        for text in ('{"value":1}', '{"value":false}', '{bad json', 'A separate explanation'):
            result = {"structuredContent": {"value": True}, "content": [{"type": "text", "text": text}]}
            with self.subTest(text=text):
                self.assertEqual(result, canonical_tool_result(result))
        result = {"structuredContent": {"value": True}, "content": [{"type": "text", "text": '{ "value": true }'}]}
        self.assertEqual([], canonical_tool_result(result)["content"])

    def test_deferred_render_keeps_events_durable_and_skips_unchanged_markdown(self):
        store = HistoryStore(self.root / "deferred", self.service_token, auto_render=False)
        store.begin(self.record, "Play the turn", self.player_token)
        log = store.logs["activation"]
        log.append("tool_result", {"itemId": "receipt", "result": {"gold": 5000}})
        self.assertFalse(log.markdown.exists())
        restored = HistoryStore(self.root / "deferred", self.service_token, auto_render=False)
        self.assertEqual(store.page("activation"), restored.page("activation"))
        log.render()
        self.assertIn('"gold": 5000', log.markdown.read_text())
        before = log.markdown.stat()
        log.render()
        self.assertEqual((before.st_ino, before.st_mtime_ns), (log.markdown.stat().st_ino, log.markdown.stat().st_mtime_ns))


class HistoryHttpsTests(unittest.IsolatedAsyncioTestCase):
    async def test_events_use_service_auth_validate_cursor_and_cannot_read_foreign_activation(self):
        with tempfile.TemporaryDirectory(prefix="agent-events-https-") as temporary:
            root = Path(temporary)
            material = ensure_server_certificate(root, "events")
            service = AgentService("Rome", root / "workspace", root / "service", "S" * 43, "https://localhost:8765/mcp", None)
            record = {"activationId": "own", "epoch": "epoch", "civilization": "Rome", "threadId": "thread", "phase": "COMPLETED", "usage": {}}
            service.records["own"] = record
            service.history.begin(record, "Actual prompt", "P" * 43)
            runner = web.AppRunner(service.application(), access_log=None)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_ssl_context(material["cert_file"], material["key_file"]))
            await site.start()
            url = f"https://localhost:{site._server.sockets[0].getsockname()[1]}/agent"
            try:
                async with aiohttp.ClientSession() as client:
                    async def request(payload, token="S" * 43):
                        async with client.post(url, ssl=client_ssl_context(material["ca_file"]), headers={"Authorization": "Bearer " + token}, json=payload) as response:
                            return response.status, await response.text()
                    status, body = await request({"op": "events", "activationId": "own", "cursor": 0, "limit": 1})
                    self.assertEqual(200, status)
                    result = json.loads(body)
                    self.assertEqual("Actual prompt", result["events"][0]["data"]["text"])
                    self.assertEqual(1, result["nextCursor"])
                    self.assertTrue(result["hasMore"])
                    self.assertEqual(401, (await request({"op": "events", "activationId": "own"}, "wrong"))[0])
                    self.assertEqual(404, (await request({"op": "events", "activationId": "foreign"}))[0])
                    self.assertEqual(400, (await request({"op": "events", "activationId": "own", "cursor": -1}))[0])
                    self.assertEqual(400, (await request({"op": "events", "activationId": "own", "path": "/tmp/secret"}))[0])
            finally:
                await runner.cleanup()


if __name__ == "__main__":
    unittest.main()
