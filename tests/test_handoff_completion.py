"""Native handoffs end tool access while final provider usage is drained."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from civ_agent import service_recovery
from civ_agent.agent_service import AgentService, ServiceError
from civ_agent.civilization_state import StateConflict
from civ_agent.proposal_gateway import public_result
from tests.test_agent_service import AgentServiceTestCase, FakeCodex, FakeFactory


class HandoffCodex(FakeCodex):
    async def request(self, method, payload):
        if method != "turn/interrupt":
            return await super().request(method, payload)
        self.factory.calls.append((method, payload))
        self.factory.interrupt_started.set()
        await self.factory.release_interrupt.wait()
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
            "turn/completed",
            {"threadId": self.thread_id, "turn": {"id": payload["turnId"], "status": self.factory.completion}},
        )
        if self.factory.completion == "completed":
            raise ServiceError("CODEX_REQUEST_FAILED")  # Completion won the race with interruption.
        return {}


class HandoffFactory(FakeFactory):
    def __init__(self):
        super().__init__()
        self.interrupt_started = asyncio.Event()
        self.release_interrupt = asyncio.Event()
        self.completion = "interrupted"

    def __call__(self, executable, workspace, mcp_url, ca_file, notification):
        if "economy" in workspace.name:
            return super().__call__(executable, workspace, mcp_url, ca_file, notification)
        process = HandoffCodex(self, workspace, notification)
        self.instances.append(process)
        return process


async def native_handoff(service, job, arguments, call_id):
    """Exercise native receipt handling through the trusted two-role coordinator."""
    if job.gameplay_fenced:
        return await service.gateway.call(job, "player_command", arguments, call_id)
    roles = service.turn_roles
    roles.native_call_id = call_id
    await service.gateway.call(
        job,
        "finish_role",
        {key: arguments[key] for key in ("deferReason", "deferChoices") if key in arguments},
        call_id + ":role",
    )
    ended = asyncio.create_task(job.gameplay_ended.wait())
    assert roles.advance_task is not None
    try:
        await asyncio.wait({ended, roles.advance_task}, return_when=asyncio.FIRST_COMPLETED)
        if roles.advance_task.done() and roles.advance_task.exception() is not None:
            await roles.advance_task
    finally:
        ended.cancel()
        await asyncio.gather(ended, return_exceptions=True)
    return public_result(await service.io.call(service.memory.call, job.id, call_id, "player_command", arguments))


class HandoffFailureTests(AgentServiceTestCase):
    async def test_repeated_handoff_proof_can_arrive_during_checkpoint_capture(self):
        self.factory.complete = False
        await self.service.start(self.payload())
        async with asyncio.timeout(2):
            while self.service.status()["phase"] == "STARTING":
                await asyncio.sleep(0.001)
        job = self.service.gateway.jobs[self.factory.tokens[-1]]
        await native_handoff(self.service, job, {"kind": "END_TURN"}, "end")
        proof = {"activationId": job.id, "epoch": "epoch-one", "gameTurn": 0, "committedHandoff": 1}
        await self.service.finish_handoff(proof)
        await self.terminal()
        self.assertTrue((await service_recovery.barrier(self.service, True))["ready"])
        try:
            status = await self.service.finish_handoff(proof)
            self.assertEqual(status["confirmedHandoff"], 1)
            self.assertEqual(status["phase"], "COMPLETED")
            self.assertTrue(self.service.memory.is_paused())
        finally:
            await service_recovery.barrier(self.service, False)

    async def test_finalization_retries_failed_completion_save(self):
        await self.service.start(self.payload())
        status = await self.terminal()
        proof = {"activationId": status["activationId"], "epoch": status["epoch"], "gameTurn": 0, "committedHandoff": 1}
        save = self.service._save

        async def failing(record, **kwargs):
            if "finalizationFinishedAtMs" in record:
                raise OSError("Completion marker unavailable")
            return await save(record, **kwargs)

        with patch.object(self.service, "_save", side_effect=failing):
            with pytest.raises(OSError, match="Completion marker"):
                await self.service.finalize(proof)
        assert self.service.status()["finalizationState"] == "PENDING"
        assert "finalizationFinishedAtMs" not in self.service.records[status["activationId"]]
        assert (await self.service.finalize(proof))["finalizationState"] == "COMPLETED"
        assert [name for name, _ in self.factory.calls].count("turn/start") == 1
        # A completed archive must still retry a lost telemetry acknowledgement.
        with (
            patch.object(self.service.chat, "running", True),
            patch.object(
                self.service,
                "flush_telemetry",
                new_callable=AsyncMock,
                side_effect=[OSError("Lost acknowledgement"), None],
            ) as flush,
            patch.object(self.service, "_export", side_effect=AssertionError("Archive rendered twice")),
        ):
            with pytest.raises(OSError, match="Lost acknowledgement"):
                await self.service.finalize(proof)
            assert (await self.service.finalize(proof))["finalizationState"] == "COMPLETED"
            assert flush.await_count == 2

    async def test_handoff_receipt_recovery_fences_all_tools(self):
        self.factory.complete = False
        await self.service.start(self.payload())
        async with asyncio.timeout(2):
            while self.service.status()["phase"] == "STARTING":
                await asyncio.sleep(0.001)
        job = self.service.gateway.jobs[self.factory.tokens[-1]]
        call = self.service.memory.call

        def fail_response(job_id, call_id, name, arguments, result=None):
            if result is not None and call_id == "end":
                raise OSError("Response not saved")
            return call(job_id, call_id, name, arguments, result)

        arguments = {"kind": "END_TURN", "deferReason": "Hold position"}
        release = asyncio.Event()
        process = self.factory.instances[-1]
        request = process.request

        async def delayed(method, payload):
            if method == "turn/interrupt":
                await release.wait()
            return await request(method, payload)

        with patch.object(process, "request", side_effect=delayed):
            try:
                with patch.object(self.service.memory, "call", side_effect=fail_response):
                    with pytest.raises(OSError, match="Response not saved"):
                        await native_handoff(self.service, job, arguments, "end")
                assert self.service.memory.call_receipt(job.id, "end") is not None
                await self.service.finish_handoff(
                    {"activationId": job.id, "epoch": "epoch-one", "gameTurn": 0, "committedHandoff": 1}
                )
                assert job.gameplay_fenced
                result = await native_handoff(self.service, job, arguments, "end")
                assert "memoryPhase" not in result
                assert await self.service.gateway.tools(job) == []
                assert len(self.service.memory.requests_for_job(job.id)) == 1
                with pytest.raises(StateConflict, match="TURN_ENDED"):
                    await self.service.gateway.call(job, "memory_update", {"changes": []}, "late")
            finally:
                release.set()
            status = await self.terminal()
            assert status["phase"] == "COMPLETED", status

    async def test_completion_timeout_stops_model_and_preserves_committed_turn(self):
        self.factory.complete = False
        await self.service.start(self.payload())
        async with asyncio.timeout(2):
            while self.service.status()["phase"] == "STARTING":
                await asyncio.sleep(0.001)
        job = self.service.gateway.jobs[self.factory.tokens[-1]]

        async def accepted_before_timeout(method, payload):
            await job.gameplay_ended.wait()
            return {}

        with (
            patch.object(self.factory.instances[-1], "request", side_effect=accepted_before_timeout),
            patch("civ_agent.agent_service.MODEL_INTERRUPT_TIMEOUT", 0.02),
        ):
            await native_handoff(self.service, job, {"kind": "END_TURN"}, "end")
            status = await self.terminal()
        assert status["phase"] == "FAILED" and status["error"] == "COMPLETION_TIMEOUT"
        assert not status["usageComplete"]
        assert job.model_stopped.is_set() and self.factory.instances[-1].closed
        assert len(self.service.memory.requests_for_job(job.id)) == 1

    async def test_failed_handoff_record_stops_model_and_retains_accepted_turn(self):
        self.factory.complete = False
        await self.service.start(self.payload())
        async with asyncio.timeout(2):
            while self.service.status()["phase"] == "STARTING":
                await asyncio.sleep(0.001)
        job = self.service.gateway.jobs[self.factory.tokens[-1]]
        save = self.service._save

        async def failing(record, **kwargs):
            if record.get("gameplayEndedAtMs") and record["phase"] == "RUNNING":
                raise OSError("Storage unavailable")
            return await save(record, **kwargs)

        with patch.object(self.service, "_save", side_effect=failing):
            result = await native_handoff(
                self.service, job, {"kind": "END_TURN", "deferReason": "Hold position"}, "end"
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
        assert any(action["action"] == "Ended turn." for action in self.service.memory.summary(0)["actions"])


@pytest.mark.parametrize(
    "trace_available,completion", [(True, "interrupted"), (False, "interrupted"), (False, "completed")]
)
def test_handoff_drains_final_usage_without_additional_model_work(tmp_path, monkeypatch, trace_available, completion):
    async def run():
        factory = HandoffFactory()
        factory.complete = False
        factory.completion = completion
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
        monkeypatch.setattr(
            service.provider_usage,
            "read",
            lambda thread: (
                {
                    "usage": {
                        "input_tokens": 20,
                        "cached_input_tokens": 4,
                        "output_tokens": 6,
                        "reasoning_output_tokens": 2,
                    },
                    "modelRequests": 1,
                }
                if trace_available and thread == "thread-one"
                else None
            ),
        )
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
            result = await native_handoff(service, job, {"kind": "END_TURN", "deferReason": "Hold position"}, "end")
            assert result["stateUpdate"]["kind"] == "turn_ended"
            assert "memoryPhase" not in result
            await asyncio.wait_for(factory.interrupt_started.wait(), 2)
            assert (
                await service.gateway.call(
                    job, "player_command", {"kind": "END_TURN", "deferReason": "Hold position"}, "end"
                )
                == result
            )
            assert not factory.instances[-1].closed
            assert service.active_task is not None and not service.active_task.done()
            for name, arguments in [
                ("player_turn", {}),
                ("player_command", {"kind": "END_TURN"}),
                ("chat_read", {}),
                ("chat_send", {"to": "Greece", "text": "Hello"}),
                ("trade_read", {"proposalId": "trade1"}),
            ]:
                with pytest.raises(StateConflict, match="TURN_ENDED"):
                    await service.gateway.call(job, name, arguments, "late-" + name)
            assert await service.gateway.tools(job) == []
            # Release the interrupt reply and final provider counters.
            factory.release_interrupt.set()
            assert service.active_task is not None
            await asyncio.wait_for(asyncio.shield(service.active_task), 2)
            status = service.status()
            assert status["phase"] == "COMPLETED"
            assert status["completionReason"] == "native_handoff"
            assert status["usage"]["input_tokens"] == 20
            assert status["usageComplete"] == (trace_available or completion == "completed")
            assert service.completion == completion
            assert "COMPLETED" not in exports
            assert factory.instances[-1].closed
            assert [name for name, _ in factory.calls].count("turn/start") == 1
            assert [name for name, _ in factory.calls].count("turn/interrupt") == 1
            assert "frozenTurnSummary" not in status
            frozen = service.memory.summary(0)
            assert any(action["action"] == "Ended turn." for action in frozen["actions"])
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
            assert exports == []
            assert finalized["gameplayElapsedMs"] >= 0 and finalized["finalizationElapsedMs"] >= 0
            assert (await service.finalize(proof))["finalizationElapsedMs"] == finalized["finalizationElapsedMs"]
            history = service.history.path("job") / "events.jsonl"
            events = [json.loads(line) for line in history.read_text().splitlines()]
            withheld = [event for event in events if event["data"].get("source") == "service_handoff"]
            assert not withheld
        finally:
            factory.release_interrupt.set()
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
            assert arguments["op"] == "job_release"
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
