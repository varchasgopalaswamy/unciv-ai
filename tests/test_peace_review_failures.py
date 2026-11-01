"""A model control rejection remains diagnosable without killing gameplay."""

from types import SimpleNamespace

import pytest

from civ_agent.agent_service import AgentService, ServiceError
from civ_agent.peace_context import MAX_PEACE_PROMPT_CHARACTERS
from civ_agent.peace_review import PeaceApproval, TacticalOverview
from civ_agent.structured_decisions import GenerationRecoveryError
from tests.structured_helpers import decision_text
from tests.test_economy_roles import run_async
from tests.test_military_roles import position


@run_async
async def test_rejected_model_turn_retains_sanitized_error_even_without_notification_delivery(tmp_path):
    instances = []

    class Model:
        def __init__(self, notification):
            self.notification = notification
            self.closed = False

        async def start(self, token, settings):
            pass

        async def request(self, method, payload):
            if method == "thread/start":
                return {
                    "thread": {"id": "rejected-thread"},
                    "modelProvider": payload["modelProvider"],
                    "model": payload.get("model") or "configured-model",
                    "reasoningEffort": "medium",
                }
            assert method == "turn/start"
            assert len(payload["input"][0]["text"]) <= MAX_PEACE_PROMPT_CHARACTERS
            error = ServiceError("CODEX_REQUEST_FAILED", 502)
            error.request_failure = {
                "rpcMethod": "turn/start",
                "rpcId": 2,
                "rpcCode": -32602,
                "reason": "invalidParams",
            }
            raise error

        async def close(self):
            self.closed = True
            await self.notification("service/disconnected", {})

    def factory(executable, workspace, mcp_url, ca_file, notification):
        model = Model(notification)
        instances.append(model)
        return model

    service = AgentService(
        "Rome",
        tmp_path / "workspace",
        tmp_path / "state",
        "S" * 43,
        "https://localhost:8765/mcp",
        None,
        process_factory=factory,
    )
    try:
        service.memory.bind("e1")
        caller = SimpleNamespace(id="caller", epoch="e1")
        with pytest.raises(ServiceError, match="CODEX_REQUEST_FAILED"):
            await service.peace_reviews.compute(
                caller, "military", {"position": position(), "warState": {}, "exactPeaceTerms": {}}, TacticalOverview
            )
        record = next(iter(service.records.values()))
        assert record["phase"] == "FAILED" and record["error"] == "CODEX_REQUEST_FAILED"
        assert record["requestFailure"]["reason"] == "invalidParams"
        assert record["requestFailure"]["rpcMethod"] == "turn/start"
        assert record["startup"]["characters"] <= MAX_PEACE_PROMPT_CHARACTERS
        assert service.failure is None and not service.gateway.jobs
        assert instances[0].closed
    finally:
        await service.close()


@pytest.mark.parametrize("stalls", [1, 2])
@pytest.mark.parametrize("role,schema", [("military", TacticalOverview), ("gameplay", PeaceApproval)])
@run_async
async def test_consultations_recover_once_with_the_same_schema_and_thread(tmp_path, role, schema, stalls):
    starts, instances = [], []
    report = {
        name: True if name == "approve" else "Assessment from the delivered position." for name in schema.model_fields
    }

    class Model:
        def __init__(self, notification):
            self.notification = notification

        async def start(self, token, settings):
            pass

        async def request(self, method, payload):
            if method == "thread/start":
                return {
                    "thread": {"id": "thread"},
                    "modelProvider": payload["modelProvider"],
                    "model": payload.get("model") or "configured-model",
                    "reasoningEffort": "medium",
                }
            assert method == "turn/start"
            starts.append(payload)
            turn = f"turn-{len(starts)}"
            common = {"threadId": "thread", "turnId": turn}
            if len(starts) <= stalls:
                await self.notification("service/generationFailed", {**common, "reason": "REVIEW_OUTPUT_STALLED"})
                status = "interrupted"
            else:
                await self.notification(
                    "item/completed",
                    {
                        **common,
                        "item": {
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": decision_text(payload["outputSchema"], {"kind": "submit_report", "report": report}),
                        },
                    },
                )
                status = "completed"
            await self.notification("turn/completed", {"threadId": "thread", "turn": {"id": turn, "status": status}})
            return {"turn": {"id": turn}}

        async def close(self):
            await self.notification("service/disconnected", {})

    def factory(executable, workspace, mcp_url, ca_file, notification):
        process = Model(notification)
        instances.append(process)
        return process

    service = AgentService(
        "Rome",
        tmp_path / "workspace",
        tmp_path / "state",
        "S" * 43,
        "https://localhost:8765/mcp",
        None,
        process_factory=factory,
    )
    try:
        service.memory.bind("e1")
        caller = SimpleNamespace(id="caller", epoch="e1")
        inputs = {"position": position(), "warState": {}, "exactPeaceTerms": {}}
        if stalls == 1:
            assert await service.peace_reviews.compute(caller, role, inputs, schema) == report
        else:
            with pytest.raises(GenerationRecoveryError, match="STRUCTURED_GENERATION_RECOVERY_LIMIT"):
                await service.peace_reviews.compute(caller, role, inputs, schema)
        record = next(iter(service.records.values()))
        assert record["phase"] == ("COMPLETED" if stalls == 1 else "FAILED")
        assert record["generationRetries"] == 1
        assert len(starts) == 2 and len(instances) == 1
        assert starts[0]["threadId"] == starts[1]["threadId"]
        assert starts[0]["outputSchema"] == starts[1]["outputSchema"]
        assert service.failure is None and not service.gateway.jobs
    finally:
        await service.close()
