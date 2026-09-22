import asyncio

import pytest

from civ_agent.agent_service import AgentService
from civ_agent.strategy_guide import strategy_reference
from tests.test_agent_service import StartupHost


class MailboxHost:
    def __init__(self):
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
            {"id": len(self.events) + 1, "kind": "message", "wake": wake, "body": {"author": "Greece", "text": text}}
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
                "civilization": "Rome",
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
        self.purposes = []
        self.prompts = []
        self.instructions = []
        self.calls = []
        self.planning_started = asyncio.Event()
        self.replied = asyncio.Event()

    def __call__(self, executable, workspace, url, ca, notification):
        owner = self

        class Process:
            def __init__(self):
                self.task = None
                self.token = None

            async def start(self, token, model):
                self.token = token

            async def request(self, method, parameters):
                owner.calls.append((method, parameters))
                if method == "thread/start":
                    owner.instructions.append(parameters["developerInstructions"])
                    return {
                        "thread": {"id": workspace.name},
                        "model": "test-model",
                        "reasoningEffort": "medium",
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


@pytest.mark.parametrize("strategy_guide", [False, True])
@pytest.mark.parametrize("fast", [False, True])
def test_chat_batches_and_replies_while_planning_job_is_held(tmp_path, monkeypatch, strategy_guide, fast):
    async def run():
        host = MailboxHost()
        monkeypatch.setattr("civ_agent.chat_runtime.HostConnection", lambda *args, **kwargs: host)
        jobs = Jobs()
        service = AgentService(
            "Rome",
            tmp_path / "Rome",
            tmp_path / "private",
            "S" * 43,
            "https://localhost:8765/mcp",
            None,
            process_factory=jobs,
            connection_factory=lambda *args, **kwargs: StartupHost(jobs),
            strategy_guide=strategy_guide,
            fast=fast,
        )
        jobs.service = service
        try:
            await service.chat.connect(
                {
                    "epoch": "g1",
                    "playerToken": "P" * 43,
                    "model": "test-model",
                    "reasoningEffort": "medium",
                    "timeoutSeconds": 10,
                }
            )
            await asyncio.sleep(0.1)
            assert jobs.purposes == []  # A quiet mailbox costs no model invocation.
            await service.start(
                {
                    "activationId": "planning",
                    "epoch": "g1",
                    "civilization": "Rome",
                    "gameTurn": 0,
                    "handoff": 0,
                    "threadId": None,
                    "mcpUrl": service.mcp_url,
                    "playerToken": "P" * 43,
                    "prompt": "Play Rome's current game turn.",
                    "model": "test-model",
                    "reasoningEffort": "medium",
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
            assert host.gameplay_heartbeats == 1
            host.post("Can we discuss peace?")
            host.post("I can offer a luxury.")
            host.post("Let me know what you need.")
            await asyncio.wait_for(jobs.replied.wait(), 3)
            assert jobs.purposes == ["gameplay", "communication"]
            for method, parameters in jobs.calls:
                if method in {"thread/start", "turn/start"}:
                    assert parameters.get("serviceTier") == ("fast" if fast else None)
                    if method == "thread/start":
                        assert parameters["config"]["model_reasoning_effort"] == "medium"
                        assert parameters["config"].get("features.fast_mode", False) == fast
            assert all(prompt.count(strategy_reference()) == int(strategy_guide) for prompt in jobs.instructions)
            assert all(strategy_reference() not in prompt for prompt in jobs.prompts)
            assert service.records["planning"]["startup"]["strategyGuide"]["enabled"] == strategy_guide
            assert all(prompt.count("Civ tool discovery:") == 1 for prompt in jobs.instructions)
            assert all("Civ tool discovery:" not in prompt for prompt in jobs.prompts)
            assert '"mcp__civ__player_capabilities"' in jobs.instructions[0]
            assert '"mcp__civ__chat_read"' in jobs.instructions[1]
            assert '"mcp__civ__chat_send"' in jobs.instructions[1]
            assert '"mcp__civ__player_capabilities"' not in jobs.instructions[1]
            assert "otherwise your objective is to win the game" in jobs.instructions[1]
            assert host.sent == ["I will consider your offer."]
            assert service.status()["phase"] == "RUNNING"  # Long planning still in progress.
            await asyncio.sleep(0.1)
            assert all(
                record["requestedServiceTier"] == ("fast" if fast else None) for record in service.chat.jobs.values()
            )
            host.post("Unmentioned public remark", wake=False)
            await asyncio.sleep(1.2)
            assert jobs.purposes == ["gameplay", "communication"]
            host.expire_gameplay = True
            await service.heartbeat("planning")
            assert service.active_task is not None
            await asyncio.wait_for(service.active_task, 2)
            assert service.status()["error"] == "JOB_PERMIT_EXPIRED"
            assert not service.gateway.jobs
        finally:
            await service.close()

    asyncio.run(run())
