"""Action-bearing Monty runs preserve native identities and failure boundaries."""

import json
from typing import cast

import pytest
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.tools import DeferredToolResults
from pydantic_ai_harness.step_persistence import SqliteStepStore

from civ_agent.execution_policy import ExecutionPolicy
from civ_agent.orchestration.contracts import GatewayCall, GatewayReceipt
from civ_agent.orchestration.gateway_execution import native_call_id, operation_identity
from civ_agent.orchestration.store import JournalConflict
from civ_agent.runtime.agents import LocalAgentRunner, LocalAgentServices
from civ_agent.runtime.code_mode import validate_catalogue
from civ_agent.runtime.overflow import ScopedOverflowStore
from tests.domain_fixtures import run_async
from tests.test_local_agents import call, request, returns
from tests.test_native_command_service import native

CATALOGUE = [
    {"name": "finish_role", "inputSchema": {}},
    {"name": "player_turn", "inputSchema": {}},
    {"name": "economy_wait", "inputSchema": {}},
]


def candidate():
    return request().model_copy(update={"execution_policy": ExecutionPolicy.current()})


@run_async
async def test_nested_results_spill_but_full_objects_and_child_links_survive(tmp_path):
    seen = []
    store = SqliteStepStore(database=tmp_path / "steps.sqlite3")
    parent = "p" * 128

    async def invoke(deps, identifier, step, decision):
        seen.append((identifier, step, decision))
        if decision["kind"] == "finish":
            return GatewayReceipt(result={"roleFinished": True})
        return GatewayReceipt(
            result={
                "decisionId": identifier,
                "receipts": [
                    {
                        "operationId": identifier + ":0",
                        "tool": "player_turn",
                        "status": "confirmed",
                        "result": {"large": "x" * 30000},
                    }
                ],
                "unexecuted": [],
            }
        )

    def model(messages, info):
        if not returns(messages):
            assert [tool.name for tool in info.function_tools] == ["run_code"]
            return call(
                info,
                "run_code",
                {"code": "view = player_turn()\nassert len(view['receipts'][0]['result']['large']) == 30000\nview"},
                parent,
            )
        output = returns(messages)[-1]
        assert output.tool_name == "run_code"
        assert len(str(output.content)) < 5000
        assert "spill_" in str(output.content)
        return call(info, "finish_role", {"summary": "Done."}, "finish")

    result = await LocalAgentRunner(
        LocalAgentServices(invoke=invoke, step_store=store, overflow_root=tmp_path / "overflow")
    ).run_role(
        candidate(),
        startup={"user": "Review.", "instructions": "Use tools."},
        catalogue=CATALOGUE[:2],
        model=FunctionModel(model),
    )
    child_id = parent + "__1"
    assert seen[0][0] == native_call_id(child_id) and len(seen[0][0]) <= 128
    effect = await store.get_tool_effect(run_id=result.run_id, tool_call_id=child_id)
    assert effect is not None and effect.status == "completed"
    assert effect.idempotency_key == operation_identity(candidate().job, candidate().scope.epoch, 1, child_id)
    part = next(part for part in returns(result.messages) if part.tool_name == "run_code")
    assert len(part.metadata["tool_returns"][child_id].content["receipts"][0]["result"]["large"]) == 30000
    assert list((tmp_path / "overflow").glob("*/*.json"))


@run_async
async def test_caught_fatal_failure_fences_gathered_and_subsequent_dispatch(tmp_path):
    dispatched = []

    async def invoke(deps, identifier, step, decision):
        dispatched.append(identifier)
        raise JournalConflict("NATIVE_RECEIPT_UNCERTAIN")

    def model(messages, info):
        return call(
            info,
            "run_code",
            {
                "code": (
                    "try:\n    player_turn()\nexcept Exception:\n    pass\n"
                    "try:\n    player_turn()\nexcept Exception:\n    pass\n42"
                )
            },
            "fatal",
        )

    runner = LocalAgentRunner(
        LocalAgentServices(
            invoke=invoke, step_store=SqliteStepStore(database=tmp_path / "steps"), overflow_root=tmp_path / "overflow"
        )
    )
    with pytest.raises(JournalConflict, match="NATIVE_RECEIPT_UNCERTAIN"):
        await runner.run_role(
            candidate(),
            startup={"user": "Review.", "instructions": "Use tools."},
            catalogue=CATALOGUE[:2],
            model=FunctionModel(model),
        )
    assert dispatched == ["fatal__1"]


@run_async
async def test_deferred_child_settles_original_identity_and_outcomes_are_inline(tmp_path):
    waits = []

    async def invoke(deps, identifier, step, decision):
        if decision["kind"] == "wait":
            return GatewayReceipt(result={"receipts": [{"result": {"economyWait": {"state": "waiting"}}}]})
        return GatewayReceipt(result={"roleFinished": True})

    async def wait(deps, requests):
        waits.extend(requests.calls)
        return DeferredToolResults(calls={call.tool_call_id: {"waitComplete": True} for call in requests.calls})

    def model(messages, info):
        if not returns(messages):
            return call(
                info, "run_code", {"code": "done = economy_wait()\nassert done['waitComplete']\nNone"}, "waiting"
            )
        output = returns(messages)[-1]
        assert "waiting__1" in str(output.content) and "action_outcomes" in str(output.content)
        return call(info, "finish_role", {"summary": "Done."}, "finish")

    await LocalAgentRunner(
        LocalAgentServices(
            invoke=invoke,
            step_store=SqliteStepStore(database=tmp_path / "steps"),
            on_wait=wait,
            overflow_root=tmp_path / "overflow",
        )
    ).run_role(
        candidate(),
        startup={"user": "Wait.", "instructions": "Use tools."},
        catalogue=CATALOGUE,
        model=FunctionModel(model),
    )
    assert [call.tool_call_id for call in waits] == ["waiting__1"]


@run_async
async def test_spill_owner_isolation_restart_and_integrity(tmp_path):
    deps = candidate()
    store = ScopedOverflowStore.for_owner(tmp_path / "spill", deps)
    handle = await store.write("tool-call", b"private output")
    restarted = ScopedOverflowStore.for_owner(tmp_path / "spill", deps)
    assert await restarted.read(handle) == b"private output"
    for changed in (
        deps.model_copy(update={"purpose": "economy"}),
        deps.model_copy(update={"model_step_offset": 1}),
        deps.model_copy(update={"scope": deps.scope.model_copy(update={"player": "other"})}),
        deps.model_copy(update={"scope": deps.scope.model_copy(update={"epoch": "next"})}),
    ):
        with pytest.raises((OSError, ValueError)):
            await ScopedOverflowStore.for_owner(tmp_path / "spill", changed).read(handle)
    with pytest.raises(FileNotFoundError):
        await store.read("../../secret")
    with pytest.raises(JournalConflict):
        await store.write("tool-call", b"different")
    path = store.directory / (handle + ".json")
    body = json.loads(path.read_text())
    body["sha256"] = "a" * 64
    path.write_text(json.dumps(body))
    with pytest.raises(JournalConflict):
        await store.read(handle)


def test_catalogue_collisions_are_fatal():
    assert validate_catalogue(["player_turn", "execute_commands"]) == {
        "player_turn": "player_turn",
        "execute_commands": "execute_commands",
    }
    with pytest.raises(JournalConflict):
        validate_catalogue(["a-b", "a_b"])
    with pytest.raises(JournalConflict):
        validate_catalogue(["run-code"])


@run_async
async def test_spill_integrity_failure_cannot_be_downgraded_by_sdk_fallback(tmp_path, monkeypatch):
    model_calls = []

    def corrupt(_store, _key, _data):
        raise JournalConflict("OVERFLOW_WRITE_CONFLICT")

    monkeypatch.setattr(ScopedOverflowStore, "_write", corrupt)

    async def invoke(*_):
        return GatewayReceipt(result={"large": "x" * 30000})

    def model(messages, info):
        model_calls.append(True)
        return call(info, "run_code", {"code": "player_turn()"}, "spill-corruption")

    with pytest.raises(JournalConflict, match="OVERFLOW_WRITE_CONFLICT"):
        await LocalAgentRunner(
            LocalAgentServices(
                invoke=invoke,
                step_store=SqliteStepStore(database=tmp_path / "steps"),
                overflow_root=tmp_path / "overflow",
            )
        ).run_role(
            candidate(),
            startup={"user": "Review.", "instructions": "Use tools."},
            catalogue=CATALOGUE[:2],
            model=FunctionModel(model),
        )
    assert len(model_calls) == 1


@run_async
async def test_sequential_native_children_use_delivered_details_and_preserve_action_outcomes(tmp_path):
    async with native(tmp_path / "game", purpose="economy") as m:
        deps = candidate().model_copy(
            update={
                "scope": m.identity.scope,
                "job": m.identity.job,
                "purpose": "economy",
            }
        )
        catalogue = await m.gateway.tools(m.economy)

        async def invoke(deps, identifier, step, decision):
            if decision["kind"] == "finish":
                return GatewayReceipt(result={"roleFinished": True})
            return await m.owner(deps, identifier, step, decision)

        async def execute(deps, run_id, identifier, step, operations):
            result = await m.service.execute_local(
                m.identity,
                GatewayCall(call_id=identifier, model_step=step, decision={"kind": "act", "operations": operations}),
                run_id=run_id,
            )
            return result.result

        async def delivered(_deps, identifier, step):
            await m.owner.acknowledge_delivery(m.identity, identifier, step)

        def model(messages, info):
            if not returns(messages):
                return call(
                    info,
                    "run_code",
                    {
                        "code": (
                            "view = player_turn()\n"
                            "details = city_details(target='city2')\n"
                            "result = execute_commands(commands=[{'kind': 'SET_CITY_FOCUS', 'target': 'city2', 'selection': 'FoodFocus'}])\n"
                            "assert result['receipts'][0]['status'] == 'confirmed'\n"
                            "data = result_data(result=result)\n"
                            "assert isinstance(data, dict)\n"
                            "assert len(data['cities']) == 2\n"
                            "result_feedback(result=result)"
                        )
                    },
                    "native-program",
                )
            result = returns(messages)[-1]
            if len(returns(messages)) == 1:
                assert "action_outcomes" in str(result.content) and "confirmed" in str(result.content)
                presented = cast(dict, result.content)["output"]["receipts"][0]["result"]
                assert "stateUpdate" in presented and "cities" not in presented
                return call(
                    info, "run_code", {"code": "assert isinstance(data, dict)\nlen(data['cities'])"}, "retained-result"
                )
            assert result.content == 2  # The full result still lives in the REPL.
            return call(info, "finish_role", {"summary": "Selected food focus."}, "finish")

        result = await LocalAgentRunner(
            LocalAgentServices(
                invoke=invoke,
                execute_commands=execute,
                on_observation_delivered=delivered,
                step_store=SqliteStepStore(database=tmp_path / "steps"),
                overflow_root=tmp_path / "overflow",
            )
        ).run_role(
            deps,
            startup={"user": "Select focus.", "instructions": "Use native tools."},
            catalogue=catalogue,
            model=FunctionModel(model),
        )
        assert result.usage.requests == 3 and len(m.host.executed) == 1
        first = returns(result.messages)[0]
        command = next(
            child for child in first.metadata["tool_returns"].values() if child.tool_name == "execute_commands"
        )
        assert len(command.content["receipts"][0]["result"]["cities"]) == 2
        assert m.host.executed[0]["selection"] == "FoodFocus"
        with pytest.raises(JournalConflict, match="DELIVERY_RECEIPT_REQUIRED"):
            await m.owner.acknowledge_delivery(m.identity, "uncommitted", 1)
