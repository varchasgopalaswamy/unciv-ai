import asyncio
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

import aiohttp
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_service import AgentService, ServiceError, server_ssl_context
from unciv_tls import client_ssl_context, ensure_server_certificate


class FakeCodex:
    def __init__(self, factory, workspace, notification):
        self.factory, self.workspace, self.notification = factory, workspace, notification
        self.closed = False

    async def start(self, token, model):
        self.factory.tokens.append(token)
        if self.factory.hold == "startup":
            await asyncio.Future()

    async def request(self, method, payload):
        self.factory.calls.append((method, payload))
        if method in {"thread/start", "thread/resume"}:
            return {"thread": {"id": "thread-one"}}
        if method == "turn/start":
            record = self.factory.service.records[self.factory.service.active_id]
            path = self.factory.service.records_dir / (hashlib.sha256(record["activationId"].encode()).hexdigest() + ".json")
            assert json.loads(path.read_text())["threadId"] == "thread-one", "Turn started before durable thread ID"
            if self.factory.hold == "turn-start":
                await asyncio.Future()
            self.factory.count += 1
            turn_id = "turn-" + str(self.factory.count)
            # Native app-server starts fresh counters on every process even when resuming
            # a persisted thread. A cumulative variant exercises other protocol sources.
            last = {"inputTokens": self.factory.input_tokens, "cachedInputTokens": 2, "outputTokens": 3, "reasoningOutputTokens": 1}
            total = {key: value * (self.factory.count if self.factory.cumulative_totals else 1) for key, value in last.items()}
            await self.notification("thread/tokenUsage/updated", {
                "threadId": "thread-one", "turnId": turn_id,
                "tokenUsage": {"total": total, "last": last},
            })
            if self.factory.complete:
                await self.notification("turn/completed", {"threadId": "thread-one", "turn": {"id": turn_id, "status": "completed"}})
            return {"turn": {"id": turn_id}}
        raise AssertionError(method)

    async def close(self):
        if self.factory.close_failure:
            raise ServiceError("TEST_STOP_FAILED")
        self.closed = True
        await self.notification("service/disconnected", {})


class FakeFactory:
    def __init__(self):
        self.service = None
        self.instances, self.tokens, self.calls = [], [], []
        self.count, self.complete, self.hold, self.close_failure = 0, True, None, False
        self.cumulative_totals = False
        self.input_tokens = 10

    def __call__(self, executable, workspace, mcp_url, ca_file, notification):
        process = FakeCodex(self, workspace, notification)
        self.instances.append(process)
        return process


class AgentServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="agent-service-test-")
        self.root = Path(self.temporary.name)
        self.factory = FakeFactory()
        self.service = self.make_service()

    def make_service(self):
        service = AgentService("Rome", self.root / "workspace", self.root / "service", "S" * 43, "https://localhost:8765/mcp", None, process_factory=self.factory)
        self.factory.service = service
        return service

    async def asyncTearDown(self):
        self.factory.close_failure = False
        await self.service.close()
        self.temporary.cleanup()

    def payload(self, identifier="activation-one", **overrides):
        return {"activationId": identifier, "epoch": "epoch-one", "civilization": "Rome", "threadId": None,
                "mcpUrl": "https://localhost:8765/mcp", "playerToken": "P" * 43, "prompt": "Play one turn", "model": None,
                "timeoutSeconds": 20, "leaseSeconds": 10, **overrides}

    async def terminal(self):
        await asyncio.wait_for(asyncio.shield(self.service.active_task), 4)
        return self.service.status()

    async def test_thread_is_durable_before_turn_and_usage_survives_resume_restart(self):
        await self.service.start(self.payload())
        first = await self.terminal()
        self.assertEqual("COMPLETED", first["phase"])
        self.assertEqual({"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 3, "reasoning_output_tokens": 1}, first["usage"])
        self.assertEqual(first, await self.service.start(self.payload()))
        self.assertEqual(1, len(self.factory.instances))
        await self.service.close()
        self.service = self.make_service()
        await self.service.start(self.payload("activation-two", threadId="thread-one", playerToken="Q" * 43))
        second = await self.terminal()
        self.assertEqual(first["usage"], second["usage"])
        self.assertEqual(["P" * 43, "Q" * 43], self.factory.tokens)
        self.assertTrue(any(method == "thread/resume" for method, _ in self.factory.calls))
        self.assertTrue(all(process.closed for process in self.factory.instances))
        self.assertTrue(all(process.workspace.name == hashlib.sha256(b"epoch-one").hexdigest() for process in self.factory.instances))
        persisted = "".join(file.read_text() for file in self.service.records_dir.glob("*.json"))
        self.assertNotIn("P" * 43, persisted)
        self.assertNotIn("Q" * 43, persisted)

    async def test_activation_conflicts_and_foreign_threads_or_bindings_rejected(self):
        await self.service.start(self.payload())
        await self.terminal()
        cases = [self.payload(prompt="Changed"), self.payload("new", threadId="foreign-thread"),
                 self.payload("new", threadId="thread-one", epoch="other-epoch"), self.payload("new", civilization="Greece"),
                 self.payload("new", mcpUrl="http://localhost:8765/mcp")]
        for payload in cases:
            with self.assertRaises(ServiceError):
                await self.service.start(payload)
        self.assertEqual(1, len(self.factory.instances))

    async def test_interrupt_immediately_after_start_confirms_stopped_before_terminal(self):
        self.factory.complete = False
        await self.service.start(self.payload())
        status = await self.service.interrupt("activation-one")
        self.assertEqual("INTERRUPTED", status["phase"])
        self.assertEqual("INTERRUPTED", status["error"])
        self.assertTrue(self.factory.instances[0].closed)
        self.assertEqual(status, await self.service.interrupt("activation-one"))

    async def test_timeout_covers_startup_and_heartbeat_covers_turn_start_rpc(self):
        for hold, timeout, lease, error in [("startup", 1, 30, "ACTIVATION_TIMEOUT"), ("turn-start", 30, 1, "HEARTBEAT_EXPIRED")]:
            self.factory.hold = hold
            await self.service.start(self.payload(hold, timeoutSeconds=timeout, leaseSeconds=lease))
            status = await self.terminal()
            self.assertEqual("FAILED", status["phase"])
            self.assertEqual(error, status["error"])
            self.assertTrue(self.factory.instances[-1].closed)

    async def test_unconfirmed_shutdown_never_allows_next_activation(self):
        self.factory.complete = False
        self.factory.close_failure = True
        await self.service.start(self.payload())
        with self.assertRaises(ServiceError):
            await self.service.interrupt("activation-one")
        self.assertNotIn(self.service.status()["phase"], {"FAILED", "COMPLETED", "INTERRUPTED"})
        self.assertEqual("STOP_UNCONFIRMED", self.service.status()["error"])
        with self.assertRaises(ServiceError) as failure:
            await self.service.start(self.payload("second"))
        self.assertEqual("AGENT_BUSY", failure.exception.code)

    async def test_https_auth_origin_duplicate_headers_and_private_state_binding(self):
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        # Rejecting an untrusted server certificate deliberately aborts its TLS handshake.
        loop.set_exception_handler(lambda current, context: None if isinstance(context.get("exception"), ConnectionResetError) else current.default_exception_handler(context))
        self.addCleanup(loop.set_exception_handler, previous_handler)
        material = ensure_server_certificate(self.root, "test-service")
        runner = web.AppRunner(self.service.application(), access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_ssl_context(material["cert_file"], material["key_file"]))
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        url = f"https://localhost:{port}/agent"
        try:
            async with aiohttp.ClientSession() as client:
                with self.assertRaises(aiohttp.ClientConnectorCertificateError):
                    await client.post(url, json={"op": "status"})
                tls = client_ssl_context(material["ca_file"])
                for headers, expected in [({}, 403), ({"Authorization": "Bearer wrong"}, 401),
                                          ({"Authorization": "Bearer " + "S" * 43, "Origin": "https://attacker.invalid"}, 403),
                                          ([("Authorization", "Bearer " + "S" * 43), ("Authorization", "Bearer wrong")], 403)]:
                    async with client.post(url, ssl=tls, headers=headers, json={"op": "status"}) as response:
                        self.assertEqual(expected, response.status)
                async with client.post(url, ssl=tls, headers={"Authorization": "Bearer " + "S" * 43}, json={"op": "status"}) as response:
                    self.assertEqual(200, response.status)
                    self.assertEqual("IDLE", (await response.json())["status"]["phase"])
        finally:
            await runner.cleanup()
        with self.assertRaises(ServiceError) as failure:
            AgentService("Greece", self.root / "workspace", self.root / "service", "T" * 43, "https://localhost:8765/mcp", None)
        self.assertEqual("SERVICE_BINDING_CONFLICT", failure.exception.code)


if __name__ == "__main__":
    unittest.main()
