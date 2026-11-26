"""Maintained Memory and Planning run through local authenticated stores and SDK hooks."""

from typing import Any, cast

import pytest
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.step_persistence import SqliteStepStore

from civ_agent.decision_contracts import DecisionContract
from civ_agent.gateway_batches import BatchExecutor
from civ_agent.orchestration.authored_memory import AuthoredMemoryRequest, AuthoredMemoryService, authored_prefix
from civ_agent.orchestration.contracts import (
    GatewayCall,
    GatewayCatalogue,
    GatewayJob,
    GatewayReceipt,
    ModelSelection,
    Reference,
    RoleRequest,
    Scope,
)
from civ_agent.orchestration.gateway_registry import GatewayRegistry
from civ_agent.orchestration.gateway_state import capture_job
from civ_agent.orchestration.host_effect_service import NativeCommandService
from civ_agent.orchestration.player_plans import PlayerPlanRequest, PlayerPlanService
from civ_agent.orchestration.store import WorkflowStore
from civ_agent.runtime.agents import LocalAgentRunner, LocalAgentServices
from tests.domain_fixtures import managed, run_async
from tests.memory_helpers import stored_records
from tests.test_local_agents import call, returns


@pytest.mark.parametrize("sibling", [False, True], ids=["previous-response-source", "same-response-is-unseen"])
@run_async
async def test_local_typed_plan_cas_and_native_planning_use_canonical_stores(tmp_path, sibling):
    async with managed(tmp_path / "domain") as domain:
        job = domain.economy
        catalogue = await domain.gateway.tools(job)
        contract = DecisionContract(job.purpose, catalogue)
        await domain.gateway.call(job, "player_turn", {}, "local-start")
        state = capture_job(job, delivered_access=BatchExecutor(domain.gateway, job, contract).delivered_access)
        identity = GatewayJob(
            scope=Scope(game="game", player="player", epoch="g1", activation="activation"),
            job=job.id,
            purpose="economy",
        )

        async def resolve(_registered):
            return job

        registry = GatewayRegistry(domain.gateway, game="game", player="player", resolve=resolve)
        await registry.register(identity, state, catalogue)
        artifacts = WorkflowStore(tmp_path / "artifacts", player="player")
        artifacts.bind_epoch(identity.scope.game, identity.scope.epoch)
        (artifacts.root / "agent-steps.sqlite3").touch(mode=0o600)
        memory = AuthoredMemoryService(registry, tmp_path / "authored.sqlite3")
        plans = PlayerPlanService(registry, tmp_path / "planning.sqlite3")
        commands = NativeCommandService(registry, workflow_store=artifacts, authored_store=memory.store)
        original_host = domain.host.tool

        async def host(name, args):
            if name == "player_receipt":
                return domain.host.seen.get(args["requestId"], {"error": "NOT_ENACTED"})
            result = await original_host(name, args)
            if name == "player_command":
                result["epoch"] = args["epoch"]
            return result

        domain.host.tool = host
        calls = []

        class Gateway:
            async def catalogue(self, _request):
                calls.append("catalogue")
                return GatewayCatalogue(catalogue=catalogue)

            async def post(self, route, operation):
                if hasattr(operation, "model_dump"):
                    operation = operation.model_dump(mode="json")
                if route == "memory":
                    return (await memory.invoke(AuthoredMemoryRequest.model_validate(operation))).model_dump(
                        mode="json"
                    )
                assert route == "plans"
                return (await plans.invoke(PlayerPlanRequest.model_validate(operation))).model_dump(mode="json")

        async def invoke(_request, identifier, step, decision):
            return GatewayReceipt(result=(await registry(identity, identifier, step, decision)).result)

        async def execute(_request, run_id, call_id, step, operations):
            result = await commands.execute_local(
                identity,
                GatewayCall(call_id=call_id, model_step=step, decision={"kind": "act", "operations": operations}),
                run_id=run_id,
            )
            return result.result

        def focus(info):
            return call(
                info,
                "execute_commands",
                {
                    "commands": [
                        {
                            "kind": "SET_CITY_FOCUS",
                            "target": "city2",
                            "selection": "FoodFocus",
                            "intent": {"cityPlan": "city-city2.md"},
                        }
                    ]
                },
                "focus",
            )

        def model(messages, info):
            history = returns(messages)
            if not history:
                return call(info, "read_city_plan", {"city": "city2"}, "read-plan")
            if len(history) == 1:
                return call(
                    info,
                    "save_city_plan",
                    {"city": "city2", "reasons": {"construction": "Retain Granary for growth."}},
                    "save-plan",
                )
            if len(history) == 2:
                content = cast(dict[str, Any], history[-1].content)
                assert history[-1].outcome == "success" and content["applied"] is True
                read = call(info, "read_city_plan", {"city": "city2"}, "confirm-plan")
                return ModelResponse(parts=[*read.parts, *focus(info).parts]) if sibling else read
            if len(history) == 3:
                assert cast(dict[str, Any], history[-1].content)["id"] == "city-city2.md"
                return focus(info)
            if len(history) == 4:
                return call(
                    info,
                    "write_plan",
                    {"items": [{"content": "Review both native queues", "status": "in_progress"}]},
                    "tasks",
                )
            assert history[-1].outcome == "success"
            return call(
                info, "finish_role", {"summary": "Recorded city intent and reviewed the native position."}, "finish"
            )

        request = RoleRequest(
            **identity.model_dump(),
            startup=Reference(sha256="a" * 64),
            catalogue=Reference(sha256="b" * 64),
            selection=ModelSelection(model="test"),
            typed_authored_plans=True,
        )
        try:
            runner = LocalAgentRunner(
                LocalAgentServices(
                    invoke=invoke,
                    step_store=SqliteStepStore(database=artifacts.root / "agent-steps.sqlite3"),
                    gateway=Gateway(),
                    execute_commands=execute,
                )
            )
            result = await runner.run_role(
                request,
                startup={
                    "user": "Review current queues.",
                    "instructions": "Native actions and intent remain separate.",
                },
                catalogue=catalogue,
                model=FunctionModel(model),
            )
            assert result.usage.requests == (5 if sibling else 6) and calls == ["catalogue"]
            current = await memory.store.read(authored_prefix(identity.scope) + "city-city2.md", max_chars=8000)
            assert current is not None and "Retain Granary for growth." in current.content
            assert (await plans.store(identity).get_items())[0].content == "Review both native queues"
            decisions = [
                record["body"]
                for record in stored_records(domain.memory).values()
                if record["category"] == "decision" and record["body"].get("native")
            ]
            if sibling:
                assert decisions[-1]["_pinned"]["unverifiedCityPlan"] == "city-city2.md"
                assert "cityPlan" not in decisions[-1]["_pinned"]
            else:
                assert decisions[-1]["_pinned"]["cityPlan"]["key"] == "city-city2.md"
                assert "unverifiedCityPlan" not in decisions[-1]["_pinned"]
            assert domain.roles.ready["economy"] and len(domain.host.executed) == 1
        finally:
            await registry.close()
            artifacts.engine.dispose()
