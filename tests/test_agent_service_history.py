"""Large histories and slow storage must not starve HTTPS control or lose receipts."""

import asyncio
import hashlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import aiohttp
from aiohttp import web

from civ_agent.agent_service import TERMINAL, AgentService, server_ssl_context
from civ_agent.runner_history import Chunks
from civ_agent.tls import client_ssl_context, ensure_server_certificate
from tests.test_agent_service import FakeFactory


class ServiceHistoryTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.certificates = tempfile.TemporaryDirectory(prefix="history-control-tls-")
        cls.addClassCleanup(cls.certificates.cleanup)
        cls.material = ensure_server_certificate(Path(cls.certificates.name), "control")

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="history-control-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.factory = FakeFactory()
        self.factory.complete = False
        self.service = AgentService(
            "Rome",
            self.root / "workspace",
            self.root / "service",
            "S" * 43,
            "https://localhost:8765/mcp",
            None,
            process_factory=self.factory,
            connection_factory=self.factory.connection,
        )
        self.factory.service = self.service
        self.runner = web.AppRunner(self.service.application(), access_log=None)
        await self.runner.setup()
        self.addAsyncCleanup(self.runner.cleanup)
        site = web.TCPSite(
            self.runner,
            "127.0.0.1",
            0,
            ssl_context=server_ssl_context(self.material["cert_file"], self.material["key_file"]),
        )
        await site.start()
        self.url = f"https://localhost:{self.runner.addresses[0][1]}/agent"
        self.client = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=client_ssl_context(self.material["ca_file"])),
            timeout=aiohttp.ClientTimeout(total=1),
        )
        self.addAsyncCleanup(self.client.close)
        self.payload = {
            "activationId": "activation",
            "epoch": "epoch",
            "civilization": "Rome",
            "threadId": None,
            "gameTurn": 0,
            "handoff": 0,
            "mcpUrl": "https://localhost:8765/mcp",
            "playerToken": "P" * 43,
            "prompt": "Play one turn",
            "model": None,
            "timeoutSeconds": 30,
            "leaseSeconds": 1,
        }

    async def start(self):
        await self.service.start(self.payload)
        deadline = time.monotonic() + 3
        while self.service.status()["phase"] == "STARTING" and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        self.assertEqual("RUNNING", self.service.status()["phase"])

    async def control(self, operation):
        began = time.monotonic()
        async with self.client.post(
            self.url,
            headers={"Authorization": "Bearer " + "S" * 43},
            json={"op": operation, "activationId": "activation"},
        ) as response:
            self.assertEqual(200, response.status)
            status = (await response.json())["status"]
        self.assertLess(time.monotonic() - began, 1)
        self.assertNotIn(status.get("error"), {"HEARTBEAT_EXPIRED", "ACTIVATION_TIMEOUT"})
        return status

    async def notify(self, method, **parameters):
        await self.service._notification(method, {"threadId": "thread-one", "turnId": "turn-1", **parameters})

    async def test_https_heartbeats_continue_during_a_blocked_periodic_archive_render(self):
        entered, release = threading.Event(), threading.Event()
        export = self.service._export

        def stalled(record):
            entered.set()
            if not release.wait(8):
                raise TimeoutError("Test did not release renderer")
            return export(record)

        with patch("civ_agent.agent_service.HISTORY_RENDER_INTERVAL", 0.05):
            await self.start()
            with patch.object(self.service, "_export", side_effect=stalled):
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    # Exceed the one-second lease while storage remains blocked.
                    # Only independently serviced heartbeats can keep it alive.
                    for _ in range(12):
                        self.assertEqual("RUNNING", (await self.control("status"))["phase"])
                        await self.control("heartbeat")
                        await asyncio.sleep(0.1)
                finally:
                    release.set()
                await self.service.io.call(lambda: None)
            await self.service.interrupt("activation")
        self.assertEqual("INTERRUPTED", self.service.status()["phase"])
        self.assertTrue(self.factory.instances[0].closed)
        self.assertIn("INTERRUPTED", (self.root / "workspace/turn_0/turn.json").read_text())

    async def test_large_result_burst_keeps_all_receipts_and_one_payload_per_observation(self):
        await self.start()
        snapshot = {
            "map": {
                "tiles": [
                    {
                        "position": {"x": i % 100, "y": i // 100},
                        "terrain": "Grassland",
                        "yields": {"food": 2, "production": 1},
                        "units": [],
                    }
                    for i in range(1800)
                ]
            }
        }
        result = {"structuredContent": snapshot, "content": [{"type": "text", "text": json.dumps(snapshot)}]}
        original = json.dumps(result)
        count = 24
        self.assertGreater(len(original.encode()) * count, 8_000_000)

        async def produce():
            for index in range(count):
                await self.notify(
                    "item/started",
                    item={
                        "id": f"snapshot-{index}",
                        "type": "mcpToolCall",
                        "server": "civ",
                        "tool": "player_turn",
                        "arguments": {},
                    },
                )
                await self.notify(
                    "item/completed",
                    item={"id": f"snapshot-{index}", "type": "mcpToolCall", "status": "completed", "result": result},
                )
                command = {
                    "requestId": f"request-{index}",
                    "epoch": "epoch",
                    "revision": f"before-{index}",
                    "kind": "ACKNOWLEDGE_ALERT",
                }
                receipt = {**command, "revision": f"after-{index}", "turn": 0, "isYourTurn": True}
                await self.notify(
                    "item/started",
                    item={
                        "id": f"command-{index}",
                        "type": "mcpToolCall",
                        "server": "civ",
                        "tool": "player_command",
                        "arguments": command,
                    },
                )
                await self.notify(
                    "item/completed",
                    item={
                        "id": f"command-{index}",
                        "type": "mcpToolCall",
                        "status": "completed",
                        "result": {
                            "structuredContent": receipt,
                            "content": [{"type": "text", "text": json.dumps(receipt)}],
                        },
                    },
                )
            await self.notify(
                "item/completed",
                item={
                    "id": "answer",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "Read and acknowledged the alerts.",
                },
            )
            await self.notify("turn/completed", turn={"id": "turn-1", "status": "completed"})

        producer = asyncio.create_task(produce())
        try:
            while self.service.status()["phase"] not in TERMINAL:
                if producer.done():
                    await producer
                await self.control("heartbeat")
                await self.control("status")
                await asyncio.sleep(0.05)
            await producer
            assert self.service.active_task is not None
            await self.service.active_task
        finally:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
        self.assertEqual("COMPLETED", self.service.status()["phase"])
        self.assertEqual(original, json.dumps(result))
        path = self.service.history.path("activation") / "events.jsonl"
        self.assertLess(path.stat().st_size, len(original.encode()) * count * 0.7)
        chunks = Chunks()
        expanded = [
            item for line in path.read_text().splitlines() if (item := chunks.expand(json.loads(line))) is not None
        ]
        results = [e["data"]["result"] for e in expanded if e["kind"] == "tool_result"]
        self.assertEqual(count * 2, len(results))
        self.assertTrue(all(r["content"] == [] for r in results))
        self.assertEqual(count, sum(r["structuredContent"] == snapshot for r in results))
        await self.service.finalize(
            {"activationId": "activation", "epoch": "epoch", "gameTurn": 0, "committedHandoff": 1}
        )
        archive = await self.service.archived("activation")
        rows = [json.loads(line) for line in archive["commands"].splitlines()]
        receipts = [
            e["data"]["result"]["structuredContent"]
            for e in rows
            if e["kind"] == "tool_result" and e["data"]["itemId"].startswith("command-")
        ]
        self.assertEqual([f"request-{i}" for i in range(count)], [r["requestId"] for r in receipts])
        before = path.read_bytes()
        await self.service.close()
        restored = AgentService(
            "Rome",
            self.root / "workspace",
            self.root / "service",
            "S" * 43,
            "https://localhost:8765/mcp",
            None,
            process_factory=self.factory,
        )
        try:
            self.assertEqual(archive, await restored.archived("activation"))
            self.assertEqual(before, path.read_bytes())
        finally:
            await restored.close()

    async def test_terminal_status_and_checkpoint_wait_for_the_final_archive_flush(self):
        await self.start()
        entered, release = threading.Event(), threading.Event()
        export = self.service._export

        def stalled(record):
            if record["phase"] == "COMPLETED":
                entered.set()
                if not release.wait(8):
                    raise TimeoutError("Test did not release final flush")
            return export(record)

        with patch.object(self.service, "_export", side_effect=stalled):
            try:
                await self.notify(
                    "item/completed",
                    item={
                        "id": "answer",
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "The final report is durable.",
                    },
                )
                await self.notify("turn/completed", turn={"id": "turn-1", "status": "completed"})
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                self.assertTrue(self.factory.instances[0].closed)
                self.assertNotIn((await self.control("status"))["phase"], TERMINAL)
                record_path = self.service.records_dir / (hashlib.sha256(b"activation").hexdigest() + ".json")
                self.assertNotIn(json.loads(record_path.read_text())["phase"], TERMINAL)
            finally:
                release.set()
            assert self.service.active_task is not None
            await self.service.active_task
        self.assertEqual("COMPLETED", self.service.status()["phase"])
        self.assertEqual("COMPLETED", json.loads(record_path.read_text())["phase"])
        self.assertNotIn("The final report is durable.", (self.root / "workspace/turn_0/summary.md").read_text())
        self.assertTrue((self.root / "workspace/turn_0/summary.json").exists())

    async def test_async_finalization_keeps_http_responsive_beyond_old_timeout(self):
        await self.start()
        await self.notify("turn/completed", turn={"id": "turn-1", "status": "completed"})
        assert self.service.active_task is not None
        await self.service.active_task
        entered, release = threading.Event(), threading.Event()
        export = self.service._export

        def stalled(record):
            entered.set()
            if not release.wait(10):
                raise TimeoutError("Test did not release finalization")
            return export(record)

        payload = {
            "op": "begin_finalize",
            "activationId": "activation",
            "epoch": "epoch",
            "gameTurn": 0,
            "committedHandoff": 1,
        }
        with patch.object(self.service, "_export", side_effect=stalled) as exports:
            try:
                async with self.client.post(
                    self.url, headers={"Authorization": "Bearer " + "S" * 43}, json=payload
                ) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual((await response.json())["status"]["finalizationState"], "RUNNING")
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                # Repeating a request whose reply was lost must not create another writer.
                self.assertEqual(
                    self.service.begin_finalize({k: v for k, v in payload.items() if k != "op"})["finalizationState"],
                    "RUNNING",
                )
                for _ in range(6):
                    self.assertEqual((await self.control("status"))["finalizationState"], "RUNNING")
                    await asyncio.sleep(0.9)
            finally:
                release.set()
            await self.service.finalization_tasks["activation"]
            self.assertEqual(exports.call_count, 1)
        self.assertEqual(self.service.status()["finalizationState"], "COMPLETED")
        await self.service.finalize({k: v for k, v in payload.items() if k != "op"})
        self.assertEqual(len(self.factory.instances), 1)
