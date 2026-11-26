"""Truthful signatures, canonical query arguments and real worker-local helpers."""

import copy
from typing import Any

import pytest
from pydantic import ValidationError
from pydantic_ai import ModelRetry
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.step_persistence import SqliteStepStore
from pydantic_monty import Monty, MontyRuntimeError

from civ_agent.execution_policy import CodeModePolicy, ExecutionPolicy
from civ_agent.inspection_bundle import typed_bundle_model
from civ_agent.observations import ConstructionDetails
from civ_agent.orchestration.contracts import GatewayReceipt
from civ_agent.runtime.agents import LocalAgentRunner, LocalAgentServices
from civ_agent.runtime.code_mode import truthful_nested_schema, truthful_parameters
from civ_agent.runtime.code_mode_instructions import ENTITY_READ_EXAMPLE
from civ_agent.runtime.code_mode_results import (
    LOCAL_HELPER_SOURCE,
    LOCAL_HELPER_VERSION,
    RESULT_HELPERS,
    required_result_field,
    result_data,
    result_entity,
    result_feedback,
)
from civ_agent.tool_schemas import tool_model
from tests.domain_fixtures import run_async
from tests.test_code_mode_runtime import candidate
from tests.test_local_agents import call, returns


def receipt(identifier, tool, result):
    return GatewayReceipt(
        result={
            "decisionId": identifier,
            "unexecuted": [],
            "receipts": [{"operationId": identifier + ":0", "tool": tool, "status": "confirmed", "result": result}],
        }
    )


def test_flat_union_signature_preserves_canonical_requiredness_and_conditional_validation():
    schema = ConstructionDetails.model_json_schema()
    before = copy.deepcopy(schema)
    rendered = truthful_parameters(schema)
    assert rendered["required"] == ["target"]
    assert {"target", "names", "horizonTurns"} <= rendered["properties"].keys()
    assert schema == before and "anyOf" in schema
    assert ConstructionDetails.model_validate({"target": "city2", "names": ["Granary"], "horizonTurns": 10})
    for invalid in ({}, {"target": "city2", "horizonTurns": 10}):
        with pytest.raises(ValidationError):
            ConstructionDetails.model_validate(invalid)


def test_nested_same_title_union_preserves_both_ways_to_supply_intent():
    from civ_agent.memory_records import Intent

    schema = Intent.model_json_schema()
    shown = truthful_nested_schema(schema)
    assert shown["required"] == []
    assert {"reason", "cityPlan"} <= shown["properties"].keys()
    assert Intent.model_validate({"reason": "Develop the city."})
    assert Intent.model_validate({"cityPlan": "city-city1.md"})
    with pytest.raises(ValidationError):
        Intent.model_validate({})


def test_real_monty_helper_parity_for_empty_error_partial_and_invalid_shapes():
    good = receipt("read", "unit_details", {"data": {"entity": {"moves": []}}}).result
    partial = copy.deepcopy(good)
    partial["receipts"].append({"operationId": "later", "status": "unexecuted", "reason": "TARGET_CHANGED"})
    partial["unexecuted"] = ["later"]
    multi = copy.deepcopy(good)
    multi["receipts"].append({"operationId": "other", "tool": "player_turn", "status": "confirmed", "result": {}})
    cases = [
        ("result_data", good, {}),
        ("result_feedback", partial, {}),
        ("result_data", partial, {}),
        ("result_data", multi, {}),
        ("result_entity", good, {}),
        ("result_data", {"source": "s1", "data": []}, {"envelope": "review"}),
        ("result_data", {"source": "s1", "data": {"error": False}}, {"envelope": "review"}),
        ("result_feedback", {"error": "STALE_EPOCH", "receipts": [], "unexecuted": []}, {}),
        ("result_data", {"error": "STALE_EPOCH", "receipts": [], "unexecuted": []}, {}),
        ("result_data", {"decisionId": 1, "error": "retired", "receipts": [], "unexecuted": []}, {}),
        ("result_data", {"plain": "authored"}, {}),
        ("required_result_field", {"empty": []}, {"key": "empty"}),
        ("required_result_field", {}, {"key": "missing"}),
        ("required_result_field", {"data": float("nan")}, {"key": "data"}),
        ("required_result_field", {"data": b"bytes"}, {"key": "data"}),
        ("required_result_field", {1: "key"}, {"key": "data"}),
    ]
    functions: dict[str, Any] = {
        function.__name__: function for function in (result_data, result_feedback, result_entity, required_result_field)
    }
    with Monty(max_processes=1, request_timeout=5) as pool:
        with pool.checkout(limits={"max_memory": 64 * 1024 * 1024}) as session:
            session.feed_run(LOCAL_HELPER_SOURCE)
            for name, value, options in cases:
                args = {"data" if name == "required_result_field" else "result": value, **options}
                expression = name + "(" + ", ".join(key + "=" + key for key in args) + ")"
                try:
                    expected = functions[name](**args)
                except ModelRetry as error:
                    marker = str(error).split(":", 1)[0]
                    with pytest.raises(MontyRuntimeError, match=marker):
                        session.feed_run(expression, inputs=args)
                else:
                    assert session.feed_run(expression, inputs=args) == expected


def test_bundle_arguments_use_canonical_root_object_and_exclude_other_reads():
    bundle = typed_bundle_model((("player_inspect", tool_model("player_inspect", "economy")),))
    good = {"queries": [{"tool": "player_inspect", "arguments": {"subject": "CITY_FOCUS", "target": "city2"}}]}
    assert bundle.model_validate(good).model_dump(exclude_unset=True) == good
    definition = bundle.model_json_schema()["$defs"]["InspectionQuery_player_inspect"]
    assert "arguments" in definition["required"]
    for bad in (
        {"queries": [{"tool": "player_inspect", "arguments": {"arguments": good["queries"][0]["arguments"]}}]},
        {"queries": [{"tool": "unit_details", "arguments": {"target": "unit1"}}]},
    ):
        with pytest.raises(ValidationError):
            bundle.model_validate(bad)


@pytest.mark.parametrize("envelope", ["native", "review"])
def test_entity_accessor_checks_one_explicit_path_without_mutating_packets(envelope):
    data = {"data": {"entity": {"moves": []}}}
    packet = receipt("read", "unit_details", data).result if envelope == "native" else {"source": "s1", "data": data}
    before = copy.deepcopy(packet)
    entity = result_entity(packet, envelope=envelope)
    assert entity == {"moves": []}
    entity["moves"].append("local")
    assert packet == before
    wrong = receipt("read", "unit_details", {"entity": {}}).result
    with pytest.raises(ModelRetry, match="RESULT_FIELD_MISSING"):
        result_entity(wrong)


@run_async
async def test_construction_signature_and_conditional_validation_recover_without_gateway_dispatch(tmp_path):
    dispatched = []

    async def invoke(_deps, identifier, _step, decision):
        dispatched.append(decision)
        if decision["kind"] == "finish":
            return GatewayReceipt(result={"roleFinished": True})
        return receipt(identifier, "construction_details", {"available": True})

    def model(messages, info):
        history = returns(messages)
        code_tool = next(tool for tool in info.function_tools if tool.name == "run_code")
        assert "def construction_details(*, target: str" in code_tool.description
        if not history:
            return call(info, "run_code", {"code": "construction_details()"}, "missing")
        if len(history) == 1:
            assert history[-1].outcome == "retried" and "target" in str(history[-1].content)
            return call(
                info, "run_code", {"code": "construction_details(target='city2', horizonTurns=10)"}, "conditional"
            )
        if len(history) == 2:
            assert history[-1].outcome == "retried" and "names" in str(history[-1].content)
            return call(
                info,
                "run_code",
                {
                    "code": "packet=construction_details(target='city2', names=['Granary'], horizonTurns=10)\nresult_data(result=packet)"
                },
                "corrected",
            )
        assert history[-1].content == {"available": True}
        return call(info, "finish_role", {"summary": "Canonical arguments checked."}, "finish")

    await LocalAgentRunner(
        LocalAgentServices(
            invoke=invoke, step_store=SqliteStepStore(database=tmp_path / "steps"), overflow_root=tmp_path / "overflow"
        )
    ).run_role(
        candidate(),
        startup={"user": "Inspect the known city2.", "instructions": "Use permitted tools."},
        catalogue=[
            {"name": "finish_role", "inputSchema": {}},
            {"name": "construction_details", "inputSchema": ConstructionDetails.model_json_schema()},
        ],
        model=FunctionModel(model),
    )
    assert [item["kind"] for item in dispatched] == ["inspect", "finish"]
    assert dispatched[0]["operations"][0]["arguments"] == {"target": "city2", "names": ["Granary"], "horizonTurns": 10}


@run_async
async def test_helpers_stay_local_across_reuse_restart_and_one_real_tool_budget(tmp_path):
    invoked = []
    steps = SqliteStepStore(database=tmp_path / "steps")
    request = candidate().model_copy(
        update={"execution_policy": ExecutionPolicy(code_mode=CodeModePolicy(enabled=True, max_tool_calls=1))}
    )

    async def invoke(_deps, identifier, _step, decision):
        invoked.append(decision["kind"])
        if decision["kind"] == "finish":
            return GatewayReceipt(result={"roleFinished": True})
        return receipt(identifier, "player_turn", {"turn": 3})

    def model(messages, info):
        history = returns(messages)
        if not history:
            return call(
                info,
                "run_code",
                {
                    "code": "packet=player_turn()\nfor index in range(200):\n    data=result_data(result=packet)\nassert isinstance(data, dict)\nrequired_result_field(data=data, key='turn')"
                },
                "local",
            )
        if len(history) == 1:
            assert history[-1].content == 3
            assert history[-1].metadata["local_result_helpers"] == LOCAL_HELPER_VERSION
            assert set(history[-1].metadata["tool_calls"]) == {"local__1"}
            return call(
                info,
                "run_code",
                {"code": "required_result_field(data={'reused': result_data(result=packet)}, key='reused')"},
                "reuse",
            )
        if len(history) == 2:
            assert history[-1].content == {"turn": 3}
            return call(
                info,
                "run_code",
                {"restart": True, "code": "required_result_field(data={'empty': []}, key='empty')"},
                "reset",
            )
        assert history[-1].content == [] and history[-1].metadata["tool_calls"] == {}
        return call(info, "finish_role", {"summary": "Local helper reuse and reset checked."}, "finish")

    result = await LocalAgentRunner(LocalAgentServices(invoke=invoke, step_store=steps)).run_role(
        request,
        startup={"user": "Inspect once.", "instructions": "Use permitted tools."},
        catalogue=[{"name": name, "inputSchema": {}} for name in ("finish_role", "player_turn")],
        model=FunctionModel(model),
    )
    assert invoked == ["inspect", "finish"]
    assert not any(event.tool_name in RESULT_HELPERS for event in await steps.list_events(run_id=result.run_id))


@run_async
async def test_entity_example_filters_real_shape_and_retains_full_child_receipt(tmp_path):
    async def invoke(_deps, identifier, _step, decision):
        if decision["kind"] == "finish":
            return GatewayReceipt(result={"roleFinished": True})
        return receipt(
            identifier,
            "unit_details",
            {
                "data": {
                    "entity": {
                        "moves": [
                            {"destination": {"x": 1, "y": 2}, "command": {"kind": "MOVE_UNIT"}, "large": "x" * 4000}
                        ]
                    }
                }
            },
        )

    def model(messages, info):
        if not returns(messages):
            return call(info, "run_code", {"code": ENTITY_READ_EXAMPLE}, "entity")
        output = returns(messages)[-1]
        assert output.content == [{"destination": {"x": 1, "y": 2}, "command": {"kind": "MOVE_UNIT"}}]
        assert (
            len(
                output.metadata["tool_returns"]["entity__1"].content["receipts"][0]["result"]["data"]["entity"][
                    "moves"
                ][0]["large"]
            )
            == 4000
        )
        return call(info, "finish_role", {"summary": "Filtered delivered movement."}, "finish")

    await LocalAgentRunner(
        LocalAgentServices(
            invoke=invoke, step_store=SqliteStepStore(database=tmp_path / "steps"), overflow_root=tmp_path / "overflow"
        )
    ).run_role(
        candidate(),
        startup={"user": "Inspect unit1.", "instructions": "Use permitted tools."},
        catalogue=[{"name": name, "inputSchema": {}} for name in ("finish_role", "unit_details")],
        model=FunctionModel(model),
    )


@run_async
async def test_python_keyword_argument_wrapper_preserves_canonical_native_arguments(tmp_path):
    calls = []

    async def invoke(_deps, identifier, _step, decision):
        calls.append(decision)
        if decision["kind"] == "finish":
            return GatewayReceipt(result={"roleFinished": True})
        return receipt(identifier, "chat_history", {"messages": []})

    def model(messages, info):
        if not returns(messages):
            description = next(tool.description for tool in info.function_tools if tool.name == "run_code")
            assert "def chat_history(*, arguments: dict[str, Any]" in description
            assert "with: str" not in description
            return call(
                info,
                "run_code",
                {
                    "code": "packet=chat_history(arguments={'with': 'Greece', 'query': 'plans'})\nresult_data(result=packet)"
                },
                "keyword",
            )
        assert returns(messages)[-1].content == {"messages": []}
        return call(info, "finish_role", {"summary": "Canonical keyword parameter preserved."}, "finish")

    await LocalAgentRunner(
        LocalAgentServices(
            invoke=invoke, step_store=SqliteStepStore(database=tmp_path / "steps"), overflow_root=tmp_path / "overflow"
        )
    ).run_role(
        candidate(),
        startup={"user": "Read the permitted history.", "instructions": "Use permitted tools."},
        catalogue=[{"name": name, "inputSchema": {}} for name in ("finish_role", "chat_history")],
        model=FunctionModel(model),
    )
    assert calls[0]["operations"] == [{"tool": "chat_history", "arguments": {"with": "Greece", "query": "plans"}}]


@run_async
async def test_local_helpers_are_reinstalled_after_sandbox_timeout(tmp_path):
    request = candidate().model_copy(
        update={"execution_policy": ExecutionPolicy(code_mode=CodeModePolicy(enabled=True, execution_timeout=0.1))}
    )
    dispatched = []

    async def invoke(_deps, _identifier, _step, decision):
        dispatched.append(decision["kind"])
        assert decision["kind"] == "finish"
        return GatewayReceipt(result={"roleFinished": True})

    def model(messages, info):
        history = returns(messages)
        if not history:
            return call(info, "run_code", {"code": "while True:\n    pass"}, "timeout")
        if len(history) == 1:
            assert history[-1].outcome == "retried" and "reset" in str(history[-1].content).lower()
            return call(
                info, "run_code", {"code": "required_result_field(data={'fresh': []}, key='fresh')"}, "after-timeout"
            )
        assert history[-1].content == [] and history[-1].metadata["tool_calls"] == {}
        return call(info, "finish_role", {"summary": "Worker reset and pure helper reinstall checked."}, "finish")

    await LocalAgentRunner(
        LocalAgentServices(invoke=invoke, step_store=SqliteStepStore(database=tmp_path / "steps"))
    ).run_role(
        request,
        startup={"user": "Check sandbox recovery.", "instructions": "Use permitted tools."},
        catalogue=[{"name": "finish_role", "inputSchema": {}}],
        model=FunctionModel(model),
    )
    assert dispatched == ["finish"]
