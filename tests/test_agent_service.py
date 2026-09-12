import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import aiohttp
from aiohttp import web

from civ_agent.agent_service import AgentService, ServiceError, server_ssl_context
from civ_agent.protocol import JsonObject
from civ_agent.tls import client_ssl_context, ensure_server_certificate


class FakeCodex:
    def __init__(self, factory, workspace, notification):
        self.factory, self.workspace, self.notification = factory, workspace, notification
        self.closed = False
        self.thread_id = None

    async def start(self, token, model):
        self.factory.tokens.append(token)
        if self.factory.hold == "startup":
            await asyncio.Future()

    async def request(self, method: str, payload: JsonObject) -> JsonObject:
        self.factory.calls.append((method, payload))
        if method in {"thread/start", "thread/resume"}:
            if method == "thread/start":
                self.factory.thread_count += 1
                self.thread_id = (
                    "thread-one" if self.factory.thread_count == 1 else "thread-" + str(self.factory.thread_count)
                )
            else:
                self.thread_id = payload["threadId"]
            return {
                "thread": {"id": self.thread_id},
                "model": payload.get("model", self.factory.default_model),
                "reasoningEffort": self.factory.default_effort
                if self.factory.ignore_effort
                else payload.get("config", {}).get("model_reasoning_effort", self.factory.default_effort),
            }
        if method == "turn/start":
            record = self.factory.service.records[self.factory.service.active_id]
            path = self.factory.service.records_dir / (
                hashlib.sha256(record["activationId"].encode()).hexdigest() + ".json"
            )
            assert json.loads(path.read_text())["threadId"] == self.thread_id, "Turn started before durable thread ID"
            if self.factory.hold == "turn-start":
                await asyncio.Future()
            self.factory.count += 1
            turn_id = "turn-" + str(self.factory.count)
            # Native app-server starts fresh counters on every process even when resuming
            # a persisted thread. A cumulative variant exercises other protocol sources.
            last = {
                "inputTokens": self.factory.input_tokens,
                "cachedInputTokens": 2,
                "outputTokens": 3,
                "reasoningOutputTokens": 1,
            }
            total = {
                key: value * (self.factory.count if self.factory.cumulative_totals else 1)
                for key, value in last.items()
            }
            await self.notification(
                "thread/tokenUsage/updated",
                {
                    "threadId": self.thread_id,
                    "turnId": turn_id,
                    "tokenUsage": {"total": total, "last": last},
                },
            )
            if self.factory.complete:
                common = {"threadId": self.thread_id, "turnId": turn_id}
                await self.notification(
                    "item/started",
                    {
                        **common,
                        "item": {
                            "id": "snapshot",
                            "type": "mcpToolCall",
                            "server": "civ",
                            "tool": "player_turn",
                            "arguments": {},
                        },
                    },
                )
                await self.notification(
                    "item/completed",
                    {
                        **common,
                        "item": {
                            "id": "snapshot",
                            "type": "mcpToolCall",
                            "status": "completed",
                            "result": {"oldSnapshot": "HUGE_OLD_SNAPSHOT_SENTINEL"},
                        },
                    },
                )
                await self.notification(
                    "item/completed",
                    {
                        **common,
                        "item": {
                            "id": "summary",
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": self.factory.summary,
                        },
                    },
                )
                await self.notification(
                    "turn/completed", {"threadId": self.thread_id, "turn": {"id": turn_id, "status": "completed"}}
                )
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
        self.count, self.complete, self.close_failure = 0, True, False
        self.hold: str | None = None
        self.cumulative_totals = False
        self.input_tokens = 10
        self.default_model, self.default_effort = "configured-model", "medium"
        self.ignore_effort = False
        self.thread_count = 0
        self.summary = "Moved the scout east to explore. Continue scouting toward the coast next turn."

    def __call__(self, executable, workspace, mcp_url, ca_file, notification):
        process = FakeCodex(self, workspace, notification)
        self.instances.append(process)
        return process


class AgentServiceTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="agent-service-test-")
        self.root = Path(self.temporary.name)
        self.factory = FakeFactory()
        self.service = self.make_service()

    def make_service(self, **settings):
        service = AgentService(
            "Rome",
            self.root / "workspace",
            self.root / "service",
            "S" * 43,
            "https://localhost:8765/mcp",
            None,
            process_factory=self.factory,
            **settings,
        )
        self.factory.service = service
        return service

    async def asyncTearDown(self):
        self.factory.close_failure = False
        await self.service.close()
        self.temporary.cleanup()

    def payload(self, identifier="activation-one", **overrides):
        return {
            "activationId": identifier,
            "epoch": "epoch-one",
            "civilization": "Rome",
            "threadId": None,
            "gameTurn": 0,
            "handoff": 0,
            "mcpUrl": "https://localhost:8765/mcp",
            "playerToken": "P" * 43,
            "prompt": "Play one turn",
            "model": None,
            "timeoutSeconds": 20,
            "leaseSeconds": 10,
            **overrides,
        }

    async def terminal(self):
        assert self.service.active_task is not None
        await asyncio.wait_for(asyncio.shield(self.service.active_task), 4)
        return self.service.status()


class AgentServiceTests(AgentServiceTestCase):
    async def test_thread_is_durable_before_turn_and_usage_survives_resume_restart(self):
        await self.service.start(self.payload())
        first = await self.terminal()
        self.assertEqual("COMPLETED", first["phase"])
        self.assertEqual(
            {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 3, "reasoning_output_tokens": 1},
            first["usage"],
        )
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
        self.assertTrue(all(process.workspace == self.root / "workspace" for process in self.factory.instances))
        persisted = "".join(file.read_text() for file in self.service.records_dir.glob("*.json"))
        self.assertNotIn("P" * 43, persisted)
        self.assertNotIn("Q" * 43, persisted)

    async def test_activation_conflicts_and_foreign_threads_or_bindings_rejected(self):
        await self.service.start(self.payload())
        await self.terminal()
        cases = [
            self.payload(prompt="Changed"),
            self.payload("new", threadId="foreign-thread"),
            self.payload("new", threadId="thread-one", epoch="other-epoch"),
            self.payload("new", civilization="Greece"),
            self.payload("new", mcpUrl="http://localhost:8765/mcp"),
        ]
        for payload in cases:
            with self.assertRaises(ServiceError):
                await self.service.start(payload)
        self.assertEqual(1, len(self.factory.instances))

    async def test_fresh_turn_has_only_committed_light_summaries_and_readable_archives(self):
        await self.service.start(self.payload())
        first = await self.terminal()
        self.assertTrue((self.root / "workspace/turn_1/commands.jsonl").exists())
        self.assertTrue((self.root / "workspace/turn_1/summary.md").exists())
        assert self.service.archive is not None
        with self.assertRaisesRegex(ValueError, "no confirmed host handoff"):
            self.service.archive.context(1)
        with self.assertRaisesRegex(ServiceError, "PREVIOUS_TURN_NOT_FINALIZED"):
            await self.service.start(self.payload("premature-next-turn", gameTurn=1, handoff=2))
        finalization = dict(activationId=first["activationId"], epoch="epoch-one", gameTurn=0, committedHandoff=1)
        committed = await self.service.finalize(finalization)
        self.assertEqual(1, committed["committedHandoff"])
        self.assertEqual(committed, await self.service.finalize(finalization))
        with self.assertRaisesRegex(ServiceError, "INVALID_HANDOFF"):
            await self.service.finalize({**finalization, "committedHandoff": 2})
        with self.assertRaisesRegex(ServiceError, "GAME_TURN_ALREADY_COMMITTED"):
            await self.service.start(self.payload("duplicate-turn", threadId=first["threadId"]))
        with self.assertRaisesRegex(ServiceError, "THREAD_NOT_OWNED"):
            await self.service.start(self.payload("wrong-thread", gameTurn=1, handoff=2, threadId=first["threadId"]))
        with self.assertRaisesRegex(ServiceError, "WORKSPACE_EPOCH_CONFLICT"):
            await self.service.start(self.payload("wrong-epoch", epoch="other-epoch"))

        # The player may write local files; trusted completed memory remains service-owned.
        (self.root / "workspace/turn_1/summary.md").write_text("TAMPERED_MEMORY_SENTINEL")
        await self.service.start(self.payload("next-turn", gameTurn=1, handoff=2))
        second = await self.terminal()
        self.assertNotEqual(first["threadId"], second["threadId"])
        self.assertEqual(
            ["thread/start", "thread/start"],
            [method for method, _ in self.factory.calls if method.startswith("thread/")],
        )
        prompt = [payload for method, payload in self.factory.calls if method == "turn/start"][-1]["input"][0]["text"]
        self.assertIn("The service writes all turn files. Do not create or edit them.", prompt)
        self.assertIn("After END_TURN succeeds, make no further tool calls", prompt)
        self.assertIn("at most 200 words", prompt)
        self.assertEqual(1, prompt.count(self.factory.summary))
        self.assertNotIn("HUGE_OLD_SNAPSHOT_SENTINEL", prompt)
        self.assertNotIn("TAMPERED_MEMORY_SENTINEL", prompt)
        self.assertEqual(1, prompt.count("# Turn 0\n"))
        self.assertIn("# Turn 0\n\n" + self.factory.summary, prompt)
        for boilerplate in (
            "Host handoff",
            "Receipt evidence",
            "## Attempt",
            "game turn 0)",
            "self-reported",
            "rolled back",
            "failed attempt",
            "epoch-one",
            "activation-one",
        ):
            self.assertNotIn(boilerplate, prompt)
        self.assertIn("turn_2/", prompt)
        exported = await self.service.archived(first["activationId"])
        self.assertEqual(("Rome", 0), (exported["civilization"], exported["gameTurn"]))
        self.assertIn("HUGE_OLD_SNAPSHOT_SENTINEL", exported["commands"])
        self.assertNotIn("P" * 43, json.dumps(exported))
        with self.assertRaisesRegex(ServiceError, "GAME_TURN_ALREADY_COMMITTED"):
            await self.service.start(self.payload("past-turn", gameTurn=0, handoff=0, epoch="epoch-one"))

    async def test_new_turn_reuses_effective_settings_after_restart_but_never_thread(self):
        await self.service.start(self.payload())
        first = await self.terminal()
        await self.service.finalize(
            dict(activationId=first["activationId"], epoch="epoch-one", gameTurn=0, committedHandoff=1)
        )
        await self.service.close()
        self.factory.default_model, self.factory.default_effort = "changed-config", "low"
        self.service = self.make_service(model="changed-service", reasoning_effort="high")
        await self.service.start(self.payload("next-turn", gameTurn=1, handoff=2))
        second = await self.terminal()
        self.assertEqual(first["effectiveSettings"], second["effectiveSettings"])
        self.assertNotEqual(first["threadId"], second["threadId"])

    async def test_rolled_back_attempt_is_archived_but_new_thread_and_memory_start_clean(self):
        self.factory.summary = "ABORTED_STRATEGY_SENTINEL: built a road for trade."
        await self.service.start(self.payload())
        first = await self.terminal()
        abortion = dict(activationId=first["activationId"], epoch="epoch-one", gameTurn=0, handoff=0)
        aborted = await self.service.abort(abortion)
        self.assertTrue(aborted["aborted"])
        self.assertEqual(aborted, await self.service.abort(abortion))
        with self.assertRaisesRegex(ServiceError, "ACTIVATION_ABORTED"):
            await self.service.finalize(
                dict(activationId=first["activationId"], epoch="epoch-one", gameTurn=0, committedHandoff=1)
            )
        with self.assertRaisesRegex(ServiceError, "THREAD_ABORTED"):
            await self.service.start(self.payload("bad-resume", threadId=first["threadId"]))
        self.factory.summary = "Moved the scout west to inspect the mountains."
        await self.service.start(self.payload("fresh-retry"))
        retry = await self.terminal()
        self.assertNotEqual(first["threadId"], retry["threadId"])
        prompt = [payload for method, payload in self.factory.calls if method == "turn/start"][-1]["input"][0]["text"]
        self.assertNotIn("ABORTED_STRATEGY_SENTINEL", prompt)
        await self.service.finalize(
            dict(activationId=retry["activationId"], epoch="epoch-one", gameTurn=0, committedHandoff=1)
        )
        assert self.service.archive is not None
        memory = self.service.archive.context(1)
        self.assertNotIn("ABORTED_STRATEGY_SENTINEL", memory)
        self.assertIn(self.factory.summary, memory)
        dense = (await self.service.archived(retry["activationId"]))["commands"]
        self.assertIn(
            "ABORTED_STRATEGY_SENTINEL", (self.service.history.path(first["activationId"]) / "events.jsonl").read_text()
        )
        self.assertNotIn('"aborted":true', dense)
        failed_commands = self.root / "workspace/turn_1/attempts/attempt_1/commands.jsonl"
        self.assertIn("HUGE_OLD_SNAPSHOT_SENTINEL", failed_commands.read_text())
        with self.assertRaisesRegex(ServiceError, "GAME_TURN_ALREADY_COMMITTED"):
            await self.service.abort(dict(activationId=retry["activationId"], epoch="epoch-one", gameTurn=0, handoff=0))
        await self.service.start(self.payload("next-turn", gameTurn=1, handoff=2))
        await self.terminal()
        next_prompt = [payload for method, payload in self.factory.calls if method == "turn/start"][-1]["input"][0][
            "text"
        ]
        self.assertEqual(1, next_prompt.count(self.factory.summary))
        self.assertEqual(1, next_prompt.count("# Turn 0\n"))
        self.assertIn("# Turn 0\n\n" + self.factory.summary, next_prompt)
        for excluded in (
            "ABORTED_STRATEGY_SENTINEL",
            "HUGE_OLD_SNAPSHOT_SENTINEL",
            "Host handoff",
            "Receipt evidence",
            "## Attempt",
            "rolled back",
            "failed attempt",
            "fresh-retry",
            "epoch-one",
            "activation-one",
        ):
            self.assertNotIn(excluded, next_prompt)

    async def test_unknown_activation_abort_fences_delayed_start_and_allows_fresh_attempt(self):
        abortion = dict(activationId="activation-one", epoch="epoch-one", gameTurn=0, handoff=0)
        status = await self.service.abort(abortion)
        self.assertTrue(status["aborted"])
        self.assertIsNone(status["threadId"])
        self.assertEqual(status, await self.service.abort(abortion))
        with self.assertRaisesRegex(ServiceError, "ACTIVATION_ABORTED"):
            await self.service.start(self.payload())
        self.assertEqual([], self.factory.instances)
        await self.service.close()
        self.service = self.make_service()
        with self.assertRaisesRegex(ServiceError, "ACTIVATION_ABORTED"):
            await self.service.start(self.payload())
        await self.service.start(self.payload("new-attempt"))
        self.assertEqual("COMPLETED", (await self.terminal())["phase"])

    async def test_fresh_thread_start_cannot_reuse_an_aborted_thread(self):
        await self.service.start(self.payload())
        first = await self.terminal()
        await self.service.abort(dict(activationId=first["activationId"], epoch="epoch-one", gameTurn=0, handoff=0))
        self.factory.thread_count = 0  # Simulate a stale app-server returning the old ID to thread/start.
        turns_before = sum(method == "turn/start" for method, _ in self.factory.calls)
        await self.service.start(self.payload("new-attempt"))
        status = await self.terminal()
        self.assertEqual(("FAILED", "INVALID_CODEX_THREAD"), (status["phase"], status["error"]))
        self.assertEqual(turns_before, sum(method == "turn/start" for method, _ in self.factory.calls))

    async def test_only_terminal_owned_turn_can_be_finalized(self):
        self.factory.complete = False
        await self.service.start(self.payload())
        finalization = dict(activationId="activation-one", epoch="epoch-one", gameTurn=0, committedHandoff=1)
        with self.assertRaisesRegex(ServiceError, "AGENT_BUSY"):
            await self.service.finalize(finalization)
        await self.service.interrupt("activation-one")
        for changed, error in (
            ({"epoch": "other"}, "INVALID_BINDING"),
            ({"gameTurn": 2}, "INVALID_BINDING"),
            ({"committedHandoff": 0}, "INVALID_HANDOFF"),
        ):
            with self.assertRaisesRegex(ServiceError, error):
                await self.service.finalize({**finalization, **changed})
        await self.service.finalize(finalization)

    async def test_recovery_does_not_duplicate_completed_summary_context(self):
        await self.service.start(self.payload())
        first = await self.terminal()
        await self.service.finalize(
            dict(activationId=first["activationId"], epoch="epoch-one", gameTurn=0, committedHandoff=1)
        )
        await self.service.start(self.payload("next-turn", gameTurn=1, handoff=2))
        next_turn = await self.terminal()
        with self.assertRaisesRegex(ServiceError, "THREAD_REQUIRED_FOR_RECOVERY"):
            await self.service.start(self.payload("lost-thread", gameTurn=1, handoff=2))
        await self.service.start(self.payload("retry-turn", gameTurn=1, handoff=2, threadId=next_turn["threadId"]))
        await self.terminal()
        prompt = [payload for method, payload in self.factory.calls if method == "turn/start"][-1]["input"][0]["text"]
        self.assertNotIn(self.factory.summary, prompt)
        self.assertIn("recovery of the same unfinished game turn", prompt)

    async def test_service_defaults_apply_to_native_thread_and_turn_but_run_overrides_win(self):
        await self.service.close()
        self.service = self.make_service(model="service-model", reasoning_effort="high")
        self.assertEqual(
            {"model": "service-model", "reasoningEffort": "high"}, self.service.status()["defaultSettings"]
        )
        await self.service.start(self.payload())
        status = await self.terminal()
        self.assertEqual({"model": "service-model", "reasoningEffort": "high"}, status["effectiveSettings"])
        thread = next(payload for method, payload in self.factory.calls if method == "thread/start")
        turn = next(payload for method, payload in self.factory.calls if method == "turn/start")
        self.assertEqual("service-model", thread["model"])
        self.assertEqual({"model_reasoning_effort": "high"}, thread["config"])
        self.assertEqual(("service-model", "high"), (turn["model"], turn["effort"]))

        await self.service.finalize(
            dict(activationId=status["activationId"], epoch="epoch-one", gameTurn=0, committedHandoff=1)
        )
        self.factory.calls.clear()
        await self.service.start(
            self.payload("run-override", gameTurn=1, handoff=2, model="run-model", reasoningEffort="custom-effort")
        )
        status = await self.terminal()
        self.assertEqual({"model": "run-model", "reasoningEffort": "custom-effort"}, status["effectiveSettings"])
        turn = next(payload for method, payload in self.factory.calls if method == "turn/start")
        self.assertEqual(("run-model", "custom-effort"), (turn["model"], turn["effort"]))

    async def test_resolved_codex_configuration_is_persisted_and_pinned_on_service_restart(self):
        await self.service.start(self.payload())
        first = await self.terminal()
        thread = next(payload for method, payload in self.factory.calls if method == "thread/start")
        self.assertNotIn("model", thread)
        self.assertNotIn("config", thread)
        self.assertEqual({"model": None, "reasoningEffort": None}, first["settings"])
        self.assertEqual({"model": "configured-model", "reasoningEffort": "medium"}, first["effectiveSettings"])
        await self.service.close()
        self.factory.default_model, self.factory.default_effort = "changed-config", "low"
        self.service = self.make_service(model="changed-service", reasoning_effort="high")
        await self.service.start(self.payload("resume", threadId="thread-one"))
        second = await self.terminal()
        self.assertEqual(first["effectiveSettings"], second["effectiveSettings"])
        thread = next(payload for method, payload in self.factory.calls if method == "thread/resume")
        self.assertEqual("configured-model", thread["model"])
        self.assertEqual({"model_reasoning_effort": "medium"}, thread["config"])
        for override in ({"model": "changed-model"}, {"reasoningEffort": "high"}):
            with self.assertRaisesRegex(ServiceError, "THREAD_SETTINGS_CONFLICT"):
                await self.service.start(self.payload("conflicting-resume", threadId="thread-one", **override))
        self.assertEqual(2, len(self.factory.instances))

    async def test_invalid_settings_never_start_a_codex_process(self):
        for key, code in (("model", "INVALID_MODEL"), ("reasoningEffort", "INVALID_REASONING_EFFORT")):
            for value in ("", " ", "high\n", "with space", "x" * 129, [], 3):
                with self.subTest(key=key, value=value):
                    with self.assertRaisesRegex(ServiceError, code):
                        await self.service.start(self.payload(**{key: value}))
        self.assertEqual([], self.factory.instances)

    async def test_unapplied_effort_fails_before_starting_model_work(self):
        self.factory.ignore_effort = True
        await self.service.start(self.payload(reasoningEffort="unsupported-effort"))
        status = await self.terminal()
        self.assertEqual("FAILED", status["phase"])
        self.assertEqual("CODEX_REASONING_EFFORT_NOT_APPLIED", status["error"])
        self.assertFalse(any(method == "turn/start" for method, _ in self.factory.calls))
        self.assertTrue(self.factory.instances[0].closed)

    async def test_interrupt_immediately_after_start_confirms_stopped_before_terminal(self):
        self.factory.complete = False
        await self.service.start(self.payload())
        status = await self.service.interrupt("activation-one")
        self.assertEqual("INTERRUPTED", status["phase"])
        self.assertEqual("INTERRUPTED", status["error"])
        self.assertTrue(self.factory.instances[0].closed)
        self.assertEqual(status, await self.service.interrupt("activation-one"))

    async def test_timeout_covers_startup_and_heartbeat_covers_turn_start_rpc(self):
        for hold, timeout, lease, error in [
            ("startup", 1, 30, "ACTIVATION_TIMEOUT"),
            ("turn-start", 30, 1, "HEARTBEAT_EXPIRED"),
        ]:
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
        loop.set_exception_handler(
            lambda current, context: (
                None
                if isinstance(context.get("exception"), ConnectionResetError)
                else current.default_exception_handler(context)
            )
        )
        self.addCleanup(loop.set_exception_handler, previous_handler)
        material = ensure_server_certificate(self.root, "test-service")
        runner = web.AppRunner(self.service.application(), access_log=None)
        await runner.setup()
        site = web.TCPSite(
            runner, "127.0.0.1", 0, ssl_context=server_ssl_context(material["cert_file"], material["key_file"])
        )
        await site.start()
        port = runner.addresses[0][1]
        url = f"https://localhost:{port}/agent"
        try:
            async with aiohttp.ClientSession() as client:
                with self.assertRaises(aiohttp.ClientConnectorCertificateError):
                    await client.post(url, json={"op": "status"})
                tls = client_ssl_context(material["ca_file"])
                for headers, expected in [
                    ({}, 403),
                    ({"Authorization": "Bearer wrong"}, 401),
                    ({"Authorization": "Bearer " + "S" * 43, "Origin": "https://attacker.invalid"}, 403),
                    ([("Authorization", "Bearer " + "S" * 43), ("Authorization", "Bearer wrong")], 403),
                ]:
                    async with client.post(url, ssl=tls, headers=headers, json={"op": "status"}) as response:
                        self.assertEqual(expected, response.status)
                async with client.post(
                    url, ssl=tls, headers={"Authorization": "Bearer " + "S" * 43}, json={"op": "status"}
                ) as response:
                    self.assertEqual(200, response.status)
                    self.assertEqual("IDLE", (await response.json())["status"]["phase"])
                await self.service.start(self.payload())
                await self.terminal()
                headers = {"Authorization": "Bearer " + "S" * 43}
                async with client.post(
                    url,
                    ssl=tls,
                    headers=headers,
                    json={
                        "op": "finalize",
                        "activationId": "activation-one",
                        "epoch": "epoch-one",
                        "gameTurn": 0,
                        "committedHandoff": 1,
                    },
                ) as response:
                    self.assertEqual(200, response.status)
                    self.assertEqual(1, (await response.json())["status"]["committedHandoff"])
                async with client.post(
                    url, ssl=tls, headers=headers, json={"op": "archive", "activationId": "activation-one"}
                ) as response:
                    self.assertEqual(200, response.status)
                    archive = await response.json()
                    self.assertEqual("Rome", archive["civilization"])
                    self.assertIn(self.factory.summary, archive["summary"])
                    self.assertNotIn("fingerprint", archive)
        finally:
            await runner.cleanup()
        with self.assertRaises(ServiceError) as failure:
            AgentService(
                "Greece", self.root / "workspace", self.root / "service", "T" * 43, "https://localhost:8765/mcp", None
            )
        self.assertEqual("SERVICE_BINDING_CONFLICT", failure.exception.code)
