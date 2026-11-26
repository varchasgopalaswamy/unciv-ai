"""The full SDK/Monty path joins scoped investment edits and real native receipts."""

import json

import pytest
from pydantic_ai.messages import ModelMessagesTypeAdapter, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.step_persistence import SqliteStepStore

from civ_agent.execution_policy import ExecutionPolicy
from civ_agent.orchestration.authored_memory import authored_prefix
from civ_agent.orchestration.contracts import GatewayCall, GatewayJob, GatewayReceipt
from civ_agent.orchestration.investment_workflow import task_id
from civ_agent.runtime.agents import LocalAgentRunner, LocalAgentServices
from tests.domain_fixtures import run_async
from tests.memory_helpers import stored_records
from tests.test_code_mode_catalogue import generated_functions, prepare
from tests.test_investment_workflow import same_kind_choice
from tests.test_local_agents import call, returns
from tests.test_native_player_stores import production_stores


@pytest.mark.parametrize("failure_after_commit", [False, True])
@pytest.mark.parametrize("comparison_case", ["france", "production"])
@run_async
async def test_full_code_workflow_keeps_one_task_and_receipt_progress_after_python_failure(
    tmp_path, failure_after_commit, comparison_case
):
    async with production_stores(tmp_path, native_commands=True) as fixture:
        request = (await prepare(fixture, "gameplay")).model_copy(
            update={"execution_policy": ExecutionPolicy.workflows(code_mode=True)}
        )
        catalogue = fixture.store.get(request.scope, request.catalogue)
        startup = fixture.store.get(request.scope, request.startup)
        identity = GatewayJob(scope=request.scope, job=request.job, purpose=request.purpose)
        path = fixture.store.root / "agent-steps.sqlite3"
        path.touch(mode=0o600)
        steps = SqliteStepStore(database=path)
        finish_calls = []

        async def invoke(deps, identifier, step, decision):
            if decision["kind"] == "finish":
                finish_calls.append(decision)
                return GatewayReceipt(result={"roleFinished": True})
            return await fixture.gateway(deps, identifier, step, decision)

        async def execute(_deps, run_id, identifier, step, operations):
            result = await fixture.commands.execute_local(
                identity,
                GatewayCall(call_id=identifier, model_step=step, decision={"kind": "act", "operations": operations}),
                run_id=run_id,
            )
            return result.result

        async def delivered(_deps, identifier, step):
            await fixture.root.registry.acknowledge_delivery(identity, identifier, step)

        async def opening_delivered(_deps, _run_id, visible):
            assert visible
            if "preparedDelivery" in startup:
                await fixture.root.registry.startup_delivery(identity, startup["preparedDelivery"], delivered=True)

        selected = same_kind_choice(comparison_case)
        program = (
            "opening = delivered_context()\n"
            "assert opening['role'] == 'gameplay'\n"
            "initial = review_investments()\n"
            f"selected = choose_investment(choice={selected.model_dump(mode='json')!r})\n"
            "assert selected['applied'] is True\n"
            "reviewed = review_investments()\n"
            "city_details(target='city1')\n"
            "packet = execute_commands(commands=[{'kind':'SET_CITY_FOCUS','target':'city1',"
            "'selection':'FoodFocus','intent':{'strategy':'strategy.md','decision':'develop','reason':'Apply the chosen food focus'}}])\n"
            "feedback = result_feedback(result=packet)\n"
            "assert feedback['receipts'][0]['status'] == 'confirmed'\n"
            "progress = review_investments()\n"
            "refs = [r['receipt'] for r in progress['progress']['receipts'] if r['commandStatus'] == 'confirmed']\n"
            "assert len(refs) == 1\n"
            "tracked = track_investment(decisionId='develop',status='completed',"
            "nextStep='Inspect subsequent native yields',assessment='The native focus command was confirmed',"
            "receiptRefs=refs)\n"
            "assert tracked['planning']['task']['status'] == 'completed'\n"
            "assert tracked['receiptEvidence'][0]['receipt'] == refs[0]\n"
            "assert tracked['nativeObjectiveVerified'] is False\n"
            "tracked\n"
        )
        program = program.replace("'develop'", repr(selected.id))
        if failure_after_commit:
            program += "1 / 0\n"
        requests = []

        def model(messages, info):
            requests.append(messages)
            assert {tool.name for tool in info.function_tools} == {"run_code"}
            assert {tool.name for tool in info.output_tools} == {"finish_role"}
            generated = generated_functions(info)
            assert {
                "review_investments",
                "choose_investment",
                "track_investment",
                "delivered_context",
            } <= generated.keys()
            assert "save_strategy" not in generated
            assert not any(generated[name] for name in ("review_investments", "choose_investment", "track_investment"))
            if len(requests) == 1:
                return call(info, "run_code", {"code": program}, "investment-program")
            if failure_after_commit and len(requests) == 2:
                assert "division by zero" in str(messages).lower()
                assert "Confirmed actions remain committed" in str(messages) or "action_outcomes" in str(messages)
                return call(
                    info,
                    "run_code",
                    {
                        "code": (
                            "recovered = review_investments()\n"
                            f"assert recovered['planning'][{selected.id!r}]['task']['status'] == 'completed'\n"
                            "assert len(recovered['progress']['receipts']) == 1\n"
                            f"recovered['planning'][{selected.id!r}]['task']['id']"
                        )
                    },
                    "recover-without-replay",
                )
            output = returns(messages)[-1]
            assert output.tool_name == "run_code" and output.outcome == "success", str(output.content)
            if not failure_after_commit:
                assert isinstance(output.content, dict)
                checkpoint = output.content["output"]
                assert checkpoint["strategy"]["decision"]["id"] == selected.id
                assert "content" not in checkpoint["strategy"] and "progress" not in checkpoint["strategy"]
                assert checkpoint["planning"]["task"]["status"] == "completed"
                assert checkpoint["nativeObjectiveVerified"] is False
            return call(
                info,
                "finish_role",
                {"summary": "Confirmed native focus change; authored task completion is separate."},
                "finish",
            )

        result = await LocalAgentRunner(
            LocalAgentServices(
                invoke=invoke,
                execute_commands=execute,
                on_observation_delivered=delivered,
                on_startup_delivered=opening_delivered,
                gateway=fixture.gateway,
                step_store=steps,
                overflow_root=tmp_path / "overflow",
            )
        ).run_role(request, startup=startup, catalogue=catalogue, model=FunctionModel(model))
        assert result.usage.requests == (3 if failure_after_commit else 2)
        assert len(fixture.host.executed) == 1 and fixture.host.view["cities"][0]["focus"] == "FoodFocus"
        tasks = await fixture.plans.store(identity).get_items()
        assert len(tasks) == 1 and tasks[0].id == task_id(selected.id) and tasks[0].status.value == "completed"
        strategy = await fixture.authored.store.read(authored_prefix(request.scope) + "strategy.md", max_chars=8000)
        assert strategy is not None and len(json.loads(strategy.content)["decisions"]) == 1
        assert strategy.version == "1", "Recovery must not rewrite the already committed investment"
        records = [
            record["body"]
            for record in stored_records(fixture.root.memory).values()
            if record["category"] == "decision" and record["body"].get("native")
        ]
        assert len(records) == 1 and records[0]["_pinned"]["decision"]["id"] == selected.id
        assert "unverifiedStrategy" not in records[0]["_pinned"]
        assert len(finish_calls) == 1
        events = await steps.list_events(run_id=result.run_id)
        retained = [
            part
            for event in events
            if "nested_result" in event.metadata
            for message in ModelMessagesTypeAdapter.validate_json(event.metadata["nested_result"])
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        assert any(
            part.tool_name == "choose_investment" and isinstance(part.content, dict) and part.content.get("applied")
            for part in retained
        )
        assert any(
            part.tool_name == "track_investment"
            and isinstance(part.content, dict)
            and "content" in part.content["strategy"]
            and "progress" in part.content["strategy"]
            for part in retained
        )
        assert any(
            part.tool_name == "track_investment"
            and isinstance(part.content, dict)
            and part.content.get("nativeObjectiveVerified") is False
            for part in retained
        )
