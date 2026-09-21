"""Native turn completion stops the model without losing receipts or known usage."""

import asyncio
import json
from unittest.mock import patch

import pytest

from civ_agent.agent_service import AgentService, ServiceError
from civ_agent.civilization_state import StateConflict
from tests.test_agent_service import AgentServiceTestCase, FakeCodex, FakeFactory


class HandoffCodex(FakeCodex):
    async def request(self, method, payload):
        if method != "turn/interrupt":
            return await super().request(method, payload)
        self.factory.calls.append((method, payload))
        # A last native usage notification must be retained while stopping.
        await self.notification(
            "thread/tokenUsage/updated",
            {
                "threadId": self.thread_id,
                "turnId": payload["turnId"],
                "tokenUsage": {
                    "total": {"inputTokens": 20, "cachedInputTokens": 4, "outputTokens": 6, "reasoningOutputTokens": 2}
                },
            },
        )
        await self.notification(
            "turn/completed", {"threadId": self.thread_id, "turn": {"id": payload["turnId"], "status": "interrupted"}}
        )
        return {}


class HandoffFactory(FakeFactory):
    def __call__(self, executable, workspace, mcp_url, ca_file, notification):
        process = HandoffCodex(self, workspace, notification)
        self.instances.append(process)
        return process


class HandoffFailureTests(AgentServiceTestCase):
    async def test_failed_handoff_summary_stops_model_and_retains_accepted_turn(self):
        self.factory.complete = False
        await self.service.start(self.payload())
        async with asyncio.timeout(2):
            while self.service.status()["phase"] == "STARTING":
                await asyncio.sleep(0.001)
        job = self.service.gateway.jobs[self.factory.tokens[-1]]
        with patch.object(self.service.memory, "summary", side_effect=OSError("Storage unavailable")):
            result = await self.service.gateway.call(
                job, "player_command", {"kind": "END_TURN", "deferReason": "Hold position"}, "end"
            )
            assert result["stateUpdate"]["kind"] == "turn_ended"
            status = await self.terminal()
        assert status["phase"] == "FAILED" and status["error"] == "HANDOFF_RECORD_FAILED"
        assert not status["usageComplete"]
        assert self.factory.instances[-1].closed and job.model_stopped.is_set()
        assert job.gameplay_fenced
        proof = {
            "activationId": status["activationId"],
            "epoch": status["epoch"],
            "gameTurn": status["gameTurn"],
            "committedHandoff": 1,
        }
        await self.service.finish_handoff(proof)
        assert (await self.service.finalize(proof))["committedHandoff"] == 1
        assert any(action["action"] == "End turn" for action in self.service.memory.summary(0)["actions"])


@pytest.mark.parametrize("held_response", [True, False])
def test_handoff_stops_model_retains_usage_and_defers_archive(tmp_path, monkeypatch, held_response):
    async def run():
        factory = HandoffFactory()
        factory.complete = False
        service = AgentService(
            "Rome",
            tmp_path / "workspace",
            tmp_path / "service",
            "S" * 43,
            "https://localhost:8765/mcp",
            None,
            process_factory=factory,
            connection_factory=factory.connection,
        )
        factory.service = service
        payload = {
            "activationId": "job",
            "epoch": "epoch",
            "civilization": "Rome",
            "gameTurn": 0,
            "handoff": 0,
            "threadId": None,
            "mcpUrl": service.mcp_url,
            "playerToken": "P" * 43,
            "prompt": "Play Rome's current game turn.",
            "model": None,
            "timeoutSeconds": 20,
            "leaseSeconds": 10,
        }
        exports = []
        original_export = service._export

        def export(record):
            exports.append(record["phase"])
            return original_export(record)

        monkeypatch.setattr(service, "_export", export)
        try:
            await service.start(payload)
            async with asyncio.timeout(2):
                while service.status()["phase"] == "STARTING":
                    await asyncio.sleep(0.001)
            job = service.gateway.jobs[factory.tokens[-1]]
            # An argument rejection must not end the model job.
            failed = await service.gateway.call(
                job, "player_command", {"kind": "END_TURN", "intent": {"deferReason": "invalid nesting"}}, "invalid"
            )
            assert "error" in failed and not job.gameplay_ended.is_set()
            result = await service.gateway.call(
                job, "player_command", {"kind": "END_TURN", "deferReason": "Hold position"}, "end"
            )
            job.handoff_response_held = held_response
            assert result["stateUpdate"]["kind"] == "turn_ended"
            with pytest.raises(StateConflict, match="TURN_ENDED"):
                await service.gateway.call(job, "memory_read", {"ids": []}, "too-late")
            assert service.active_task is not None
            await asyncio.wait_for(asyncio.shield(service.active_task), 2)
            status = service.status()
            assert status["phase"] == "COMPLETED"
            assert status["completionReason"] == "native_handoff"
            assert status["usage"]["input_tokens"] == 20
            assert status["usageComplete"] == held_response
            assert "COMPLETED" not in exports
            assert factory.instances[-1].closed
            assert [name for name, _ in factory.calls].count("turn/start") == 1
            assert [name for name, _ in factory.calls].count("turn/interrupt") == 1
            assert "frozenTurnSummary" not in status
            frozen = service.records["job"]["frozenTurnSummary"]
            assert any(action["action"] == "End turn" for action in frozen["actions"])
            proof = {"activationId": "job", "epoch": "epoch", "gameTurn": 0, "committedHandoff": 1}
            with pytest.raises(ServiceError, match="INVALID_BINDING"):
                await service.finish_handoff({**proof, "epoch": "other"})
            with pytest.raises(ServiceError, match="INVALID_HANDOFF"):
                await service.finish_handoff({**proof, "committedHandoff": 2})
            await service.finish_handoff(proof)
            # Later private activity cannot silently change this turn's summary.
            monkeypatch.setattr(
                service.memory, "summary", lambda *args, **kwargs: pytest.fail("Reread live memory after handoff")
            )
            finalized = await service.finalize(proof)
            assert finalized["committedHandoff"] == 1
            assert "COMPLETED" in exports
            assert finalized["gameplayElapsedMs"] >= 0 and finalized["finalizationElapsedMs"] >= 0
            assert (await service.finalize(proof))["finalizationElapsedMs"] == finalized["finalizationElapsedMs"]
            history = service.history.path("job") / "events.jsonl"
            events = [json.loads(line) for line in history.read_text().splitlines()]
            withheld = [event for event in events if event["data"].get("source") == "service_handoff"]
            assert len(withheld) == 2
            assert all(event["data"]["deliveredToModel"] is False for event in withheld)
        finally:
            await service.close()

    asyncio.run(run())


def test_finished_game_accounting_flush_retries_without_starting_models(tmp_path):
    class Connection:
        def __init__(self):
            self.reports = []
            self.lose_ack = True

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def tool(self, name, arguments):
            assert name == "player_communication"
            if arguments["op"] == "state":
                return {"civilization": "Rome", "epoch": "epoch"}
            assert arguments["op"] == "job_report"
            self.reports.append(arguments)
            if self.lose_ack:
                self.lose_ack = False
                raise OSError("Acknowledgement lost")
            return {"granted": True}

    async def run():
        connection = Connection()
        factory = HandoffFactory()
        service = AgentService(
            "Rome",
            tmp_path / "workspace",
            tmp_path / "service",
            "S" * 43,
            "https://localhost:8765/mcp",
            None,
            process_factory=factory,
            connection_factory=lambda *args, **kwargs: connection,
        )
        try:
            service.accounting.start("job", "epoch", True)
            service.accounting.sample(
                {
                    "activationId": "job",
                    "phase": "COMPLETED",
                    "usage": {"input_tokens": 123},
                    "usageReported": True,
                    "usageComplete": True,
                }
            )
            payload = {"epoch": "epoch", "playerToken": "P" * 43}
            with pytest.raises(OSError, match="Acknowledgement lost"):
                await service.flush_accounting(payload)
            assert service.accounting.pending()
            assert await service.flush_accounting(payload) == {"flushed": True}
            assert connection.reports[0] == connection.reports[1]
            assert not service.accounting.pending()
            assert not factory.instances and not service.chat.running
            with pytest.raises(ServiceError, match="INVALID_BINDING"):
                await service.flush_accounting({**payload, "epoch": "abandoned"})
        finally:
            await service.close()

    asyncio.run(run())
