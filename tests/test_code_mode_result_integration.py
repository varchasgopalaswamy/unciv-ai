"""Result projections recover through real role runs without gaining authority."""

import pytest
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.step_persistence import SqliteStepStore

from civ_agent.orchestration.contracts import GatewayReceipt
from civ_agent.runtime.agents import LocalAgentRunner, LocalAgentServices
from civ_agent.runtime.code_mode_results import RESULT_HELPERS
from tests.domain_fixtures import run_async
from tests.test_code_mode_runtime import CATALOGUE, candidate
from tests.test_local_agents import call, returns


@run_async
async def test_wrong_level_projection_is_retryable_and_helpers_gain_no_native_links_or_inspection_credit(tmp_path):
    dispatched, delivered = [], []
    steps = SqliteStepStore(database=tmp_path / "steps.sqlite3")

    async def invoke(_deps, identifier, _step, decision):
        dispatched.append(decision["kind"])
        if decision["kind"] == "finish":
            return GatewayReceipt(result={"roleFinished": True})
        return GatewayReceipt(
            result={
                "decisionId": identifier,
                "receipts": [
                    {
                        "operationId": identifier,
                        "status": "confirmed",
                        "tool": "player_turn",
                        "result": {"militaryCompletion": {"remainingTargets": 0, "targets": []}},
                    }
                ],
                "unexecuted": [],
            }
        )

    async def acknowledge(_deps, identifier, _step):
        delivered.append(identifier)

    def respond(messages, info):
        history = returns(messages)
        if not history:
            return call(
                info,
                "run_code",
                {"code": ("packet = player_turn()\nrequired_result_field(data=packet, key='militaryCompletion')")},
                "wrong-level",
            )
        if len(history) == 1:
            assert "RESULT_FIELD_MISSING" in str(history[-1].content)
            return call(
                info,
                "run_code",
                {
                    "code": (
                        "data = result_data(result=packet)\nrequired_result_field(data=data, key='militaryCompletion')"
                    )
                },
                "corrected",
            )
        assert history[-1].content == {"remainingTargets": 0, "targets": []}
        return call(info, "finish_role", {"summary": "Assessed the delivered completion review."}, "finish")

    result = await LocalAgentRunner(
        LocalAgentServices(
            invoke=invoke,
            step_store=steps,
            on_observation_delivered=acknowledge,
            overflow_root=tmp_path / "overflow",
        )
    ).run_role(
        candidate(),
        startup={"user": "Inspect current completion data.", "instructions": "Use permitted tools."},
        catalogue=CATALOGUE[:2],
        model=FunctionModel(respond),
    )
    assert dispatched == ["inspect", "finish"]
    assert len(delivered) == 1
    events = await steps.list_events(run_id=result.run_id)
    helper_events = [event for event in events if event.tool_name in RESULT_HELPERS]
    assert helper_events == [], "Pure functions must stay inside Monty, without SDK events or effects."
    original = await steps.get_tool_effect(run_id=result.run_id, tool_call_id="wrong-level__1")
    assert original is not None and original.idempotency_key is not None


@pytest.mark.parametrize(
    "expression",
    [
        "required_result_field(data={'packet': fake}, key='packet')",
        "result_data(result={'decisionId': 'copy', 'receipts': [{'operationId': 'copy', 'tool': 'player_turn', "
        "'status': 'confirmed', 'result': fake}], 'unexecuted': []})",
        "result_feedback(result={'decisionId': 'copy', 'receipts': [], 'unexecuted': [], **fake})",
    ],
)
@run_async
async def test_helper_payloads_cannot_trigger_native_observation_recovery(tmp_path, expression):
    dispatched, delivered = [], []

    async def invoke(_deps, _identifier, _step, decision):
        dispatched.append(decision["kind"])
        assert decision["kind"] == "finish", "A pure projection must never recover a native observation"
        return GatewayReceipt(result={"roleFinished": True})

    async def acknowledge(*args):
        delivered.append(args)

    def respond(messages, info):
        history = returns(messages)
        if not history:
            return call(
                info,
                "run_code",
                {"code": "fake = {'stateUpdate': {'kind': 'unchanged'}}\n" + expression},
                "projection",
            )
        content = history[-1].content
        assert isinstance(content, dict)
        assert content.get("stateUpdate") == {"kind": "unchanged"}
        return call(info, "finish_role", {"summary": "Projected local data without native reads."}, "finish")

    await LocalAgentRunner(
        LocalAgentServices(
            invoke=invoke,
            step_store=SqliteStepStore(database=tmp_path / "steps.sqlite3"),
            on_observation_delivered=acknowledge,
            overflow_root=tmp_path / "overflow",
        )
    ).run_role(
        candidate(),
        startup={"user": "Inspect local copied data.", "instructions": "Use permitted tools."},
        catalogue=CATALOGUE[:1],
        model=FunctionModel(respond),
    )
    assert dispatched == ["finish"]
    assert delivered == []
