import asyncio
import hashlib
import json

import pytest

from civ_agent.agent_service import AgentService, ServiceError
from civ_agent.civilization_guide import civilization_reference
from civ_agent.strategy_guide import strategy_reference
from tests.test_agent_service import FakeCodex, StartupHost


def test_polling_health_reports_failures_and_recovery_without_losing_cursor(tmp_path, monkeypatch):
    class PollingHost(MailboxHost):
        def __init__(self):
            super().__init__()
            self.responses = asyncio.Queue()
            self.cursors = []

        async def tool(self, name, arguments):
            if arguments["op"] != "read":
                return await super().tool(name, arguments)
            self.cursors.append(arguments["cursor"])
            response = await self.responses.get()
            if isinstance(response, Exception):
                raise response
            return response

    async def run():
        host = PollingHost()
        monkeypatch.setattr("civ_agent.chat_runtime.HostConnection", lambda *args, **kwargs: host)
        service = AgentService(
            "Rome", tmp_path / "Rome", tmp_path / "private", "S" * 43, "https://localhost:8765/mcp", None
        )

        async def until(predicate):
            async with asyncio.timeout(4):
                while not predicate():
                    await asyncio.sleep(0.01)

        try:
            await service.chat.connect(
                {"epoch": "g1", "playerToken": "P" * 43, "model": None, "reasoningEffort": None, "timeoutSeconds": 10}
            )

            def health():
                return service.health()["communicationSubscription"]

            assert health()["status"] == "CONNECTING"
            host.responses.put_nowait({"events": [], "nextCursor": 0, "hasMore": False})
            await until(lambda: health()["status"] == "HEALTHY")
            initial_success = health()["lastSuccessAtMs"]
            assert isinstance(initial_success, int)
            host.responses.put_nowait(ValueError("private wire body"))
            host.responses.put_nowait(ValueError("private wire body"))
            await until(lambda: health()["failures"] == 2)
            assert health()["status"] == "RECONNECTING"
            assert health()["consecutiveFailures"] == 2
            last_failure = health()["lastFailureAtMs"]
            assert isinstance(last_failure, int) and last_failure >= initial_success
            assert await service.io.call(service.memory.cursor) == 0
            # Subscription recovery must not erase an independent model job failure.
            service.chat.error = "COMMUNICATION_JOB_FAILED"
            host.responses.put_nowait(
                {
                    "events": [
                        {
                            "id": 1,
                            "kind": "message",
                            "wake": False,
                            "body": {"message": "m1", "author": "Greece", "text": "Hello"},
                        }
                    ],
                    "nextCursor": 1,
                    "hasMore": False,
                }
            )
            await until(lambda: health()["status"] == "HEALTHY" and len(host.cursors) == 5)
            assert host.cursors == [0, 0, 0, 0, 1]
            assert await service.io.call(service.memory.cursor) == 1
            assert health()["consecutiveFailures"] == 0
            assert health()["failures"] == 2
            last_success = health()["lastSuccessAtMs"]
            assert isinstance(last_success, int) and last_success >= last_failure
            assert service.health()["communicationJobError"] == "COMMUNICATION_JOB_FAILED"
            assert service.health()["runFailure"] is None
            assert not (service.workspace / "chat.jsonl").exists()
            assert not (service.workspace / "memory-index.md").exists()
            await service.chat.close()
            assert health()["status"] == "DISCONNECTED"
            assert health()["failures"] == 2
        finally:
            await service.close()
        log = service.diagnostics.path.read_text()
        events = [json.loads(line)["event"] for line in log.splitlines()]
        assert events.count("communication_subscription_failed") == 2
        assert events.count("communication_subscription_recovered") == 1
        assert "private wire body" not in log

    asyncio.run(run())


class MailboxHost:
    def __init__(self, civilization="Rome"):
        self.civilization = civilization
        self.events = []
        self.changed = asyncio.Event()
        self.sent = []
        self.gameplay_claimed = asyncio.Event()
        self.permit_gameplay = False
        self.expire_gameplay = False
        self.gameplay_heartbeats = 0

    async def close(self):
        pass

    async def list_tools(self):
        return []

    def post(self, text, wake=True):
        self.events.append(
            {
                "id": len(self.events) + 1,
                "kind": "message",
                "wake": wake,
                "body": {"message": f"m{len(self.events) + 1}", "author": "Greece", "text": text},
            }
        )
        self.changed.set()

    async def tool(self, name, arguments):
        if arguments["op"] == "job_claim" and arguments["purpose"] == "gameplay":
            self.gameplay_claimed.set()
            return {"granted": self.permit_gameplay}
        if arguments["op"] == "job_heartbeat" and arguments["job"] == "planning":
            self.gameplay_heartbeats += 1
            assert self.permit_gameplay, "A waiting job cannot renew a permit it does not hold"
            return {"granted": not self.expire_gameplay}
        if arguments["op"] == "read":
            cursor = arguments["cursor"]
            while cursor == len(self.events):
                self.changed.clear()
                await self.changed.wait()
            return {"events": self.events[cursor:], "nextCursor": len(self.events), "hasMore": False}
        if arguments["op"] == "state":
            return {
                "civilization": self.civilization,
                "channels": [self.civilization, "Greece"],
                "epoch": "g1",
                "peaceVersion": 0,
                "offers": [],
                "consents": [],
                "outcomes": [],
            }
        if arguments["op"] == "send":
            self.sent.append(arguments["text"])
            return {"sent": True}
        return {"granted": True}


class Jobs:
    def __init__(self):
        self.service = None
        self.default_model, self.default_effort, self.close_failure = "test-model", "medium", False
        self.purposes = []
        self.prompts = []
        self.instructions = []
        self.calls = []
        self.planning_started = asyncio.Event()
        self.replied = asyncio.Event()

    def __call__(self, executable, workspace, url, ca, notification):
        owner = self
        if "economy" in workspace.name:
            return FakeCodex(self, workspace, notification)

        class Process:
            def __init__(self):
                self.task = None
                self.token = None

            async def start(self, token, settings):
                self.token = token

            async def request(self, method, parameters):
                owner.calls.append((method, parameters))
                if method == "thread/start":
                    owner.instructions.append(parameters["developerInstructions"])
                    return {
                        "thread": {"id": workspace.name},
                        "modelProvider": parameters["modelProvider"],
                        "model": parameters.get("model", "test-model"),
                        "reasoningEffort": parameters.get("config", {}).get("model_reasoning_effort", "medium"),
                        "serviceTier": parameters.get("serviceTier"),
                    }
                if method == "turn/start":
                    owner.prompts.append(parameters["input"][0]["text"])
                    purpose = (
                        "communication"
                        if "newly received communication" in parameters["input"][0]["text"]
                        else "gameplay"
                    )
                    owner.purposes.append(purpose)
                    if purpose == "gameplay":
                        owner.planning_started.set()
                    else:

                        async def respond():
                            await asyncio.sleep(0)
                            assert owner.service is not None
                            job = owner.service.gateway.jobs[self.token]
                            await owner.service.gateway.call(job, "chat_read", {}, "read")
                            await owner.service.gateway.call(
                                job, "chat_send", {"to": "Greece", "text": "I will consider your offer."}, "send"
                            )
                            await notification(
                                "turn/completed",
                                {"threadId": workspace.name, "turn": {"id": "t1", "status": "completed"}},
                            )
                            owner.replied.set()

                        self.task = asyncio.create_task(respond())
                    return {"turn": {"id": "t1"}}
                return {}

            async def close(self):
                if self.task:
                    await self.task

        return Process()


def test_transport_failure_during_close_cannot_finalize_communication_as_completed(tmp_path, monkeypatch):
    async def run():
        host = MailboxHost()
        monkeypatch.setattr("civ_agent.chat_runtime.HostConnection", lambda *args, **kwargs: host)
        jobs = Jobs()

        def process_factory(executable, workspace, url, ca, notification):
            process = jobs(executable, workspace, url, ca, notification)
            close = process.close

            async def fail_during_flush():
                await close()
                await notification("service/transportFailure", {"reason": "CODEX_NOTIFICATION_OVERFLOW"})
                await notification("service/transportStatus", {"peakQueuedNotifications": 4096})

            process.close = fail_during_flush
            return process

        service = AgentService(
            "Rome",
            tmp_path / "Rome",
            tmp_path / "private",
            "S" * 43,
            "https://localhost:8765/mcp",
            None,
            process_factory=process_factory,
            connection_factory=lambda *args, **kwargs: StartupHost(jobs),
        )
        jobs.service = service
        try:
            await service.chat.connect(
                {"epoch": "g1", "playerToken": "P" * 43, "model": None, "reasoningEffort": None, "timeoutSeconds": 10}
            )
            await service.review_phase({"epoch": "g1", "gameTurn": 0, "stage": "release"})
            await service.chat.compute("chat-transport")
            record_file = service.state_dir / "chat-jobs" / (hashlib.sha256(b"chat-transport").hexdigest() + ".json")
            record = json.loads(record_file.read_text())
            assert record["phase"] == "FAILED"
            assert record["error"] == record["transportError"] == "CODEX_NOTIFICATION_OVERFLOW"
            assert not service.gateway.jobs
        finally:
            await service.close()
        assert '"event": "codex_transport"' in service.diagnostics.path.read_text()

    asyncio.run(run())


def test_chat_history_drain_retains_process_and_io_until_a_later_close_succeeds(tmp_path, monkeypatch):
    from civ_agent.agent_service import ProcessDrainError

    async def run():
        host = MailboxHost()
        monkeypatch.setattr("civ_agent.chat_runtime.HostConnection", lambda *args, **kwargs: host)
        jobs = Jobs()
        close_calls = 0

        def process_factory(executable, workspace, url, ca, notification):
            process = jobs(executable, workspace, url, ca, notification)
            close = process.close

            async def delayed_flush():
                nonlocal close_calls
                close_calls += 1
                await close()
                if close_calls <= 2:
                    raise ProcessDrainError()

            process.close = delayed_flush
            return process

        service = AgentService(
            "Rome",
            tmp_path / "Rome",
            tmp_path / "private",
            "S" * 43,
            "https://localhost:8765/mcp",
            None,
            process_factory=process_factory,
            connection_factory=lambda *args, **kwargs: StartupHost(jobs),
        )
        jobs.service = service
        try:
            await service.chat.connect(
                {"epoch": "g1", "playerToken": "P" * 43, "model": None, "reasoningEffort": None, "timeoutSeconds": 10}
            )
            await service.review_phase({"epoch": "g1", "gameTurn": 0, "stage": "release"})
            with pytest.raises(ServiceError, match="^CODEX_NOTIFICATION_FLUSH_TIMEOUT$"):
                await service.chat.compute("unconfirmed-chat")
            assert close_calls == 1
            assert "unconfirmed-chat" in service.chat.jobs
            assert service.chat.job_records["unconfirmed-chat"][0]["error"] == "HISTORY_DRAIN_FAILED"
            with pytest.raises(ServiceError, match="^CODEX_NOTIFICATION_FLUSH_TIMEOUT$"):
                await service.close()
            assert close_calls == 2
            assert not service.io.closed and not service.diagnostics.closed
            assert "unconfirmed-chat" in service.chat.jobs
            await service.close()
            assert close_calls == 3
            assert not service.chat.jobs and not service.chat.job_records and not service.gateway.jobs
            assert service.io.closed
            record_file = service.state_dir / "chat-jobs" / (hashlib.sha256(b"unconfirmed-chat").hexdigest() + ".json")
            record = json.loads(record_file.read_text())
            assert record["phase"] == "FAILED" and record["error"] == "HISTORY_DRAIN_FAILED"
        finally:
            await service.close()

    asyncio.run(run())


@pytest.mark.parametrize("strategy_guide", [False, True])
@pytest.mark.parametrize("civilization_guide", [False, True])
@pytest.mark.parametrize("fast", [False, True])
@pytest.mark.parametrize("provider", ["openai", "openrouter"])
def test_chat_batches_and_replies_while_planning_job_is_held(
    tmp_path, monkeypatch, strategy_guide, civilization_guide, fast, provider
):
    async def run():
        monkeypatch.setattr("civ_agent.chat_runtime.COMMUNICATION_BATCH_SECONDS", 0.05)
        monkeypatch.setattr("civ_agent.agent_service.JOB_PERMIT_RENEW_INTERVAL", 0.05)
        model = "test/model" if provider == "openrouter" else "test-model"
        priority = fast and provider == "openai"
        civilization = "France" if civilization_guide else "Rome"
        host = MailboxHost(civilization)
        monkeypatch.setattr("civ_agent.chat_runtime.HostConnection", lambda *args, **kwargs: host)
        jobs = Jobs()

        def connection(*args, **kwargs):
            result = StartupHost(jobs)
            result.civilization = civilization
            return result

        service = AgentService(
            civilization,
            tmp_path / civilization,
            tmp_path / "private",
            "S" * 43,
            "https://localhost:8765/mcp",
            None,
            process_factory=jobs,
            connection_factory=connection,
            strategy_guide=strategy_guide,
            civilization_guide=civilization_guide,
            fast=fast,
            provider=provider,
            model=model,
            reasoning_effort="max",
        )
        jobs.service = service
        try:
            await service.chat.connect(
                {
                    "epoch": "g1",
                    "playerToken": "P" * 43,
                    "model": model,
                    "reasoningEffort": None,
                    "timeoutSeconds": 10,
                }
            )
            await asyncio.sleep(0.1)
            assert jobs.purposes == []  # A quiet mailbox costs no model invocation.
            await service.review_phase({"epoch": "g1", "gameTurn": 0, "stage": "release"})
            await service.start(
                {
                    "activationId": "planning",
                    "epoch": "g1",
                    "civilization": civilization,
                    "gameTurn": 0,
                    "handoff": 0,
                    "threadId": None,
                    "mcpUrl": service.mcp_url,
                    "playerToken": "P" * 43,
                    "prompt": "Play Rome's current game turn.",
                    "model": model,
                    "reasoningEffort": "max",
                    "timeoutSeconds": 20,
                    "leaseSeconds": 30,
                }
            )
            await asyncio.wait_for(host.gameplay_claimed.wait(), 2)
            await service.heartbeat("planning")
            assert host.gameplay_heartbeats == 0
            assert not jobs.planning_started.is_set()
            host.permit_gameplay = True
            await asyncio.wait_for(jobs.planning_started.wait(), 2)
            await service.heartbeat("planning")
            async with asyncio.timeout(2):
                while host.gameplay_heartbeats == 0:
                    await asyncio.sleep(0.01)
            await service.review_phase({"epoch": "g1", "gameTurn": 0, "stage": "hold"})
            host.post("Can we discuss peace?")
            host.post("I can offer a luxury.")
            host.post("Let me know what you need.")
            await asyncio.sleep(0.15)
            assert jobs.purposes == ["gameplay"] and host.sent == []
            await service.review_phase({"epoch": "g1", "gameTurn": 0, "stage": "release"})
            await asyncio.wait_for(jobs.replied.wait(), 3)
            assert jobs.purposes == ["gameplay", "communication"]
            thread_efforts = []
            for method, parameters in jobs.calls:
                if method in {"thread/start", "turn/start"}:
                    assert parameters.get("serviceTier") == ("fast" if priority else None)
                    if method == "thread/start":
                        assert parameters["modelProvider"] == provider
                        thread_efforts.append(parameters["config"]["model_reasoning_effort"])
                        assert parameters["config"].get("features.fast_mode", False) == priority
            assert thread_efforts == ["max", "medium"]
            assert all(strategy_reference() not in prompt for prompt in jobs.instructions)
            assert jobs.prompts[0].count(strategy_reference()) == int(strategy_guide)
            assert strategy_reference() not in jobs.prompts[1]
            assert service.records["planning"]["startup"]["strategyGuide"]["enabled"] == strategy_guide
            reference = civilization_reference("France")
            assert all(reference not in prompt for prompt in jobs.instructions)
            assert jobs.prompts[0].count(reference) == int(civilization_guide)
            assert reference not in jobs.prompts[1]
            assert service.records["planning"]["startup"]["civilizationGuide"]["enabled"] == civilization_guide
            if strategy_guide and civilization_guide:
                assert jobs.prompts[0].index(strategy_reference()) < jobs.prompts[0].index(reference)
            assert all(prompt.count("Civ tool discovery:") == 1 for prompt in jobs.instructions)
            assert all("Civ tool discovery:" not in prompt for prompt in jobs.prompts)
            assert '"mcp__civ__player_capabilities"' in jobs.instructions[0]
            assert '"mcp__civ__chat_read"' in jobs.instructions[1]
            assert '"mcp__civ__chat_send"' in jobs.instructions[1]
            assert '"mcp__civ__player_capabilities"' not in jobs.instructions[1]
            assert "otherwise play to win" in jobs.instructions[1]
            assert host.sent == ["I will consider your offer."]
            assert service.status()["phase"] == "RUNNING"  # Long planning still in progress.
            await asyncio.sleep(0.1)
            assert all(
                record["requestedServiceTier"] == ("fast" if priority else None)
                for record in service.chat.jobs.values()
            )
            host.post("Unmentioned public remark", wake=False)
            await asyncio.sleep(0.15)
            assert jobs.purposes == ["gameplay", "communication"]
            host.expire_gameplay = True
            # Permit failure stops gameplay independently of scheduler HTTP health checks.
            assert service.active_task is not None
            await asyncio.wait_for(service.active_task, 2)
            assert service.status()["error"] == "JOB_PERMIT_EXPIRED"
            assert not service.gateway.jobs
        finally:
            await service.close()

    asyncio.run(run())
