import asyncio
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path

import aiohttp
from aiohttp import web

from civ_agent.agent_service import AgentService, ServiceError, server_ssl_context
from civ_agent.protocol import JsonObject
from civ_agent.tls import client_ssl_context, ensure_server_certificate, write_private
from tests.test_proposal_gateway import Host


class StartupHost(Host):
    """A permitted starting view for service lifecycle tests with a fake model."""

    def __init__(self, factory):
        super().__init__()
        self.factory = factory

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()

    async def list_tools(self) -> list[JsonObject]:
        return [
            *await super().list_tools(),
            *[
                {"name": name, "description": name, "inputSchema": {"type": "object", "properties": {}}}
                for name in ("player_turn", "player_capabilities", "victory_progress")
            ],
        ]

    async def tool(self, name, args):
        record = self.factory.service.records[self.factory.service.active_id]
        if name == "player_capabilities":
            return {"protocolVersion": "test", "commands": ["END_TURN"], "queries": []}
        if name == "victory_progress":
            return {"turn": record["gameTurn"], "victories": []}
        result = await super().tool(name, args)
        if "epoch" in result:
            result["epoch"] = record["epoch"]
        if "turn" in result:
            result["turn"] = record["gameTurn"]
        return result


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
        if method == "thread/start":
            self.factory.thread_count += 1
            self.thread_id = (
                "thread-one" if self.factory.thread_count == 1 else "thread-" + str(self.factory.thread_count)
            )
            return {
                "thread": {"id": self.thread_id},
                "model": payload.get("model", self.factory.default_model),
                "reasoningEffort": self.factory.default_effort
                if self.factory.ignore_effort
                else payload.get("config", {}).get("model_reasoning_effort", self.factory.default_effort),
                "serviceTier": payload.get("serviceTier"),
            }
        if method == "turn/start":
            record = next(row for row in self.factory.service.records.values() if row.get("threadId") == self.thread_id)
            path = self.factory.service.records_dir / (
                hashlib.sha256(record["activationId"].encode()).hexdigest() + ".json"
            )
            saved = json.loads(path.read_text())
            model_record = saved
            assert model_record["threadId"] == self.thread_id, "Turn started before durable thread ID"
            if self.factory.hold == "turn-start":
                await asyncio.Future()
            self.factory.count += 1
            turn_id = "turn-" + str(self.factory.count)
            # Every activation starts with a fresh conversation and fresh counters.
            last = {
                "inputTokens": self.factory.input_tokens,
                "cachedInputTokens": 2,
                "outputTokens": 3,
                "reasoningOutputTokens": 1,
            }
            total = dict(last)
            await self.notification(
                "thread/tokenUsage/updated",
                {
                    "threadId": self.thread_id,
                    "turnId": turn_id,
                    "tokenUsage": {"total": total, "last": last},
                },
            )
            if getattr(self, "complete", self.factory.complete):
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
                            "text": getattr(self, "summary", self.factory.summary),
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
        self.input_tokens = 10
        self.default_model, self.default_effort = "configured-model", "medium"
        self.ignore_effort = False
        self.thread_count = 0
        self.summary = "Moved the scout east to explore. Continue scouting toward the coast next turn."

    def __call__(self, executable, workspace, mcp_url, ca_file, notification):
        process = FakeCodex(self, workspace, notification)
        self.instances.append(process)
        return process

    def connection(self, *args, **kwargs):
        return StartupHost(self)


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
            connection_factory=self.factory.connection,
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
    async def test_recovered_terminal_accounting_releases_orphaned_permits(self):
        from unittest.mock import AsyncMock

        from civ_agent.telemetry import JobAccounting

        self.service.accounting.start("lost-job", "epoch-one", True)
        self.service.accounting = JobAccounting(self.root / "service" / "telemetry")
        report = self.service.accounting.pending()[0]
        self.assertEqual(report["telemetry"]["phase"], "FAILED")
        connection = AsyncMock()
        connection.tool.return_value = {"granted": True}
        await self.service.flush_telemetry(connection=connection, epoch="epoch-one")
        self.assertEqual(connection.tool.call_args.args[1]["op"], "job_release")
        self.assertEqual(connection.tool.call_args.args[1]["job"], "lost-job")
        self.assertEqual(self.service.accounting.pending(), [])

    async def test_rejected_accounting_on_reconnect_retains_report_and_identifies_telemetry_failure(self):
        from unittest.mock import AsyncMock

        self.service.accounting.start("recovered-job", "epoch-one", True)
        report = self.service.accounting.pending()
        connection = AsyncMock()
        connection.tool.return_value = {"error": "INVALID_ARGUMENT"}
        self.service.chat.connection, self.service.chat.epoch = connection, "epoch-one"
        with self.assertRaisesRegex(ServiceError, "TELEMETRY_NOT_ACCEPTED"):
            await self.service.flush_telemetry()
        self.assertEqual(self.service.accounting.pending(), report)
        connection.tool.return_value = {"granted": True}
        await self.service.flush_telemetry()
        self.assertEqual(self.service.accounting.pending(), [])

    async def test_database_failure_stops_startup_with_a_nonretryable_error(self):
        from unittest.mock import AsyncMock

        from sqlalchemy.exc import SQLAlchemyError

        self.service.gateway.open = AsyncMock(side_effect=SQLAlchemyError("Database failure"))
        await self.service.start(self.payload())
        result = await self.terminal()
        self.assertEqual((result["phase"], result["error"]), ("FAILED", "STATE_STORE_FAILED"))

    async def test_failed_terminal_save_is_visible_to_the_scheduler(self):
        from unittest.mock import patch

        from sqlalchemy.exc import SQLAlchemyError

        persist = self.service._persist

        def fail_terminal(record, **kwargs):
            if record["phase"] == "COMPLETED":
                raise SQLAlchemyError("Database failure")
            return persist(record, **kwargs)

        with patch.object(self.service, "_persist", side_effect=fail_terminal):
            await self.service.start(self.payload())
            with self.assertRaisesRegex(ServiceError, "DURABLE_RECORD_FAILED"):
                await self.terminal()
        failure = self.service.health()["runFailure"]
        assert failure is not None
        self.assertEqual(failure["error"], "DURABLE_RECORD_FAILED")
        self.assertEqual(failure["job"], "activation-one")

    async def test_background_telemetry_rejection_is_a_visible_terminal_failure(self):
        from unittest.mock import AsyncMock, patch

        self.factory.complete = False
        with patch("civ_agent.agent_service.TELEMETRY_SAMPLE_INTERVAL", 0.01):
            await self.service.start(self.payload())
            async with asyncio.timeout(2):
                while self.service.status()["phase"] == "STARTING":
                    await asyncio.sleep(0.001)
            self.service.chat.running = True
            self.service.flush_telemetry = AsyncMock(side_effect=ServiceError("TELEMETRY_NOT_ACCEPTED", 503))
            status = await self.terminal()
        self.assertEqual(status["phase"], "FAILED")
        self.assertEqual(status["error"], "TELEMETRY_NOT_ACCEPTED")
        self.assertTrue(self.factory.instances[-1].closed)
        self.service.chat.running = False

    async def test_heartbeat_renews_native_lease_while_database_worker_is_busy(self):
        from unittest.mock import AsyncMock

        service = self.service
        service.records["busy"] = {"activationId": "busy", "phase": "STARTING"}
        service.active_id = service.gameplay_permit = "busy"
        service.chat.running = True
        service.chat.social = AsyncMock(return_value={"granted": True})
        entered, release = threading.Event(), threading.Event()

        def slow_database():
            entered.set()
            assert release.wait(5)

        work = asyncio.create_task(service.io.call(slow_database))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            status = await asyncio.wait_for(service.heartbeat("busy"), 0.5)
            self.assertEqual(status["phase"], "STARTING")
            service.chat.social.assert_awaited_once_with("job_heartbeat", job="busy")
            self.assertFalse(work.done())
        finally:
            release.set()
            await work
            service.chat.running = False
            service.active_id = None

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
        diagnostics = self.service.diagnostics.path.read_text()
        events = [json.loads(line) for line in diagnostics.splitlines()]
        started = next(event for event in events if event["event"] == "gameplay_job_started")
        self.assertEqual("activation-one", started["activationId"])
        self.assertEqual(0, started["gameTurn"])
        self.assertTrue(any(e["event"] == "gameplay_job_finished" and e["phase"] == "COMPLETED" for e in events))
        self.assertFalse(self.service.diagnostics.path.is_relative_to(self.service.workspace))
        self.assertNotIn("P" * 43, diagnostics)
        self.assertNotIn("S" * 43, diagnostics)
        self.service = self.make_service()
        await self.service.start(self.payload("activation-two", playerToken="Q" * 43))
        second = await self.terminal()
        self.assertEqual(first["usage"], second["usage"])
        self.assertEqual(2, len(set(self.factory.tokens)))
        self.assertTrue(all(token not in {"P" * 43, "Q" * 43} for token in self.factory.tokens))
        self.assertTrue(all(method != "thread/resume" for method, _ in self.factory.calls))
        self.assertTrue(all(process.closed for process in self.factory.instances))
        self.assertTrue(
            all(process.workspace.is_relative_to(self.root / "workspace/jobs") for process in self.factory.instances)
        )
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

    async def test_fresh_turn_uses_committed_memory_without_generating_reports(self):
        await self.service.start(self.payload())
        first = await self.terminal()
        self.assertFalse((self.root / "workspace/turn_0").exists())
        self.assertFalse((self.root / "service/turn-archive").exists())
        with self.assertRaisesRegex(ServiceError, "PREVIOUS_TURN_NOT_FINALIZED"):
            await self.service.start(self.payload("premature-next-turn", gameTurn=1, handoff=2))
        finalization = dict(activationId=first["activationId"], epoch="epoch-one", gameTurn=0, committedHandoff=1)
        committed = await self.service.finalize(finalization)
        self.assertEqual(1, committed["committedHandoff"])
        self.assertEqual(committed, await self.service.finalize(finalization))
        with self.assertRaisesRegex(ServiceError, "INVALID_HANDOFF"):
            await self.service.finalize({**finalization, "committedHandoff": 2})
        with self.assertRaisesRegex(ServiceError, "GAME_TURN_ALREADY_COMMITTED"):
            await self.service.start(self.payload("duplicate-turn"))
        with self.assertRaisesRegex(ServiceError, "INVALID_THREAD"):
            await self.service.start(self.payload("wrong-thread", gameTurn=1, handoff=2, threadId=first["threadId"]))
        with self.assertRaisesRegex(ServiceError, "WORKSPACE_EPOCH_CONFLICT"):
            await self.service.start(self.payload("wrong-epoch", epoch="other-epoch"))

        # The player may write local files; trusted completed memory remains service-owned.
        (self.root / "workspace/turn_0").mkdir(mode=0o700)
        write_private(self.root / "workspace/turn_0/summary.md", b"TAMPERED_MEMORY_SENTINEL")
        await self.service.start(self.payload("next-turn", gameTurn=1, handoff=2))
        second = await self.terminal()
        self.assertNotEqual(first["threadId"], second["threadId"])
        self.assertEqual(
            ["thread/start", "thread/start"],
            [method for method, _ in self.factory.calls if method.startswith("thread/")],
        )
        prompt = [payload for method, payload in self.factory.calls if method == "turn/start"][-1]["input"][0]["text"]
        starts = [payload for method, payload in self.factory.calls if method == "thread/start"]
        instructions = starts[-1]["developerInstructions"]
        self.assertEqual(starts[0]["developerInstructions"], instructions)
        self.assertNotEqual(starts[0]["cwd"], starts[-1]["cwd"])
        self.assertIn("Do not access the filesystem", instructions)
        self.assertNotIn("files: turn_", prompt)
        self.assertIn("After END_TURN succeeds, finish immediately", instructions)
        self.assertNotIn("Previous strategy, next-turn handoff and historical knowledge", instructions)
        self.assertNotIn("Civ tool discovery:", prompt)
        metadata = self.service.records[second["activationId"]]["startup"]
        self.assertEqual(hashlib.sha256(instructions.encode()).hexdigest(), metadata["instructionsSha256"])
        from civ_agent.runner_history import Chunks

        chunks = Chunks()
        messages = []
        for line in (self.service.history.path(second["activationId"]) / "events.jsonl").read_text().splitlines():
            event = chunks.expand(json.loads(line))
            if event is not None and event["kind"] in {"developer_message", "user_message"}:
                messages.append((event["kind"], event["data"]["text"]))
        self.assertEqual(messages, [("developer_message", instructions), ("user_message", prompt)])
        self.assertEqual(0, prompt.count(self.factory.summary))
        self.assertNotIn("HUGE_OLD_SNAPSHOT_SENTINEL", prompt)
        self.assertNotIn("TAMPERED_MEMORY_SENTINEL", prompt)
        self.assertEqual(0, prompt.count("# Turn 0\n"))
        self.assertIn("Previous strategy, next-turn handoff and historical knowledge", prompt)
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
        self.assertNotIn("turn_1/", prompt)
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
        await self.service.start(self.payload("lost-thread", gameTurn=1, handoff=2))
        fresh = await self.terminal()
        self.assertNotEqual(next_turn["threadId"], fresh["threadId"])
        await self.service.start(self.payload("retry-turn", gameTurn=1, handoff=2))
        await self.terminal()
        prompt = [payload for method, payload in self.factory.calls if method == "turn/start"][-1]["input"][0]["text"]
        self.assertNotIn(self.factory.summary, prompt)
        self.assertNotIn("This turn is unfinished", prompt)
        threads = [payload for method, payload in self.factory.calls if method.startswith("thread/")]
        self.assertEqual(threads[0]["developerInstructions"], threads[-1]["developerInstructions"])
        self.assertNotIn("inputSchema", prompt)

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
        await self.service.start(self.payload("resume"))
        second = await self.terminal()
        self.assertEqual(first["effectiveSettings"], second["effectiveSettings"])
        thread = [payload for method, payload in self.factory.calls if method == "thread/start"][-1]
        self.assertEqual("configured-model", thread["model"])
        self.assertEqual({"model_reasoning_effort": "medium"}, thread["config"])
        for override in ({"model": "changed-model"}, {"reasoningEffort": "high"}):
            with self.assertRaisesRegex(ServiceError, "INVALID_THREAD"):
                await self.service.start(self.payload("conflicting-resume", threadId="thread-one", **override))
        self.assertEqual(2, len(self.factory.instances))

    async def test_fast_priority_applies_to_new_and_resumed_turns_without_changing_model(self):
        await self.service.close()
        self.service = self.make_service(model="gpt-5.6-luna", reasoning_effort="medium", fast=True)
        await self.service.start(self.payload())
        first = await self.terminal()
        await self.service.close()
        self.service = self.make_service(fast=True)
        await self.service.start(self.payload("resume-fast"))
        second = await self.terminal()
        self.assertEqual(first["effectiveSettings"], second["effectiveSettings"])
        for method, payload in self.factory.calls:
            if method in {"thread/start", "thread/resume", "turn/start"}:
                self.assertEqual("fast", payload["serviceTier"])
                self.assertEqual("gpt-5.6-luna", payload["model"])
                if method.startswith("thread/"):
                    self.assertEqual(
                        {"model_reasoning_effort": "medium", "features.fast_mode": True}, payload["config"]
                    )
        self.assertTrue(all(method != "thread/resume" for method, _ in self.factory.calls))
        self.assertEqual("fast", second["requestedServiceTier"])
        self.assertEqual("fast", second["serviceTier"])

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
        self.assertTrue(all(process.closed for process in self.factory.instances))
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
        async with asyncio.timeout(4):
            while self.service.status()["phase"] != "RUNNING":
                await asyncio.sleep(0.01)
        with self.assertRaises(ServiceError):
            await self.service.interrupt("activation-one")
        self.assertNotIn(self.service.status()["phase"], {"FAILED", "COMPLETED", "INTERRUPTED"})
        self.assertEqual("STOP_UNCONFIRMED", self.service.status()["error"])
        with self.assertRaises(ServiceError) as failure:
            await self.service.start(self.payload("second"))
        self.assertEqual("STOP_UNCONFIRMED", failure.exception.code)

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
                        "op": "begin_finalize",
                        "activationId": "activation-one",
                        "epoch": "epoch-one",
                        "gameTurn": 0,
                        "committedHandoff": 1,
                    },
                ) as response:
                    self.assertEqual(200, response.status)
                    self.assertIn((await response.json())["status"]["finalizationState"], {"RUNNING", "COMPLETED"})
                await self.service.finalization_tasks["activation-one"]
                self.assertEqual(1, self.service.status("activation-one")["committedHandoff"])
                async with client.post(
                    url, ssl=tls, headers=headers, json={"op": "archive", "activationId": "activation-one"}
                ) as response:
                    self.assertEqual(200, response.status)
                    archive = await response.json()
                    self.assertEqual("Rome", archive["civilization"])
                    self.assertNotIn(self.factory.summary, archive["summary"])
                    self.assertNotIn("fingerprint", archive)
        finally:
            await runner.cleanup()
        with self.assertRaises(ServiceError) as failure:
            AgentService(
                "Greece", self.root / "workspace", self.root / "service", "T" * 43, "https://localhost:8765/mcp", None
            )
        self.assertEqual("SERVICE_BINDING_CONFLICT", failure.exception.code)
