import asyncio

from civ_agent.agent_service import AgentService


class MailboxHost:
    def __init__(self):
        self.events = []
        self.changed = asyncio.Event()
        self.sent = []
        self.gameplay_claimed = asyncio.Event()
        self.permit_gameplay = False
        self.expire_gameplay = False
        self.gameplay_heartbeats = 0

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
                if method == "thread/start":
                    return {"thread": {"id": workspace.name}, "model": "test-model", "reasoningEffort": "medium"}
                if method == "turn/start":
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


def test_chat_batches_and_replies_while_planning_job_is_held(tmp_path, monkeypatch):
    async def run():
        host = MailboxHost()
        monkeypatch.setattr("civ_agent.chat_runtime.HostConnection", lambda *args: host)
        jobs = Jobs()
        service = AgentService(
            "Rome",
            tmp_path / "Rome",
            tmp_path / "private",
            "S" * 43,
            "https://localhost:8765/mcp",
            None,
            process_factory=jobs,
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
            assert host.sent == ["I will consider your offer."]
            assert service.status()["phase"] == "RUNNING"  # Long planning still in progress.
            await asyncio.sleep(0.1)
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
