"""Optional authored intent failures stay failed tool results, not failed roles."""

import asyncio
import json

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import UsageLimits
from pydantic_ai_harness import StepPersistence

from civ_agent.memory_records import CityPlan
from civ_agent.orchestration.authored_memory import authored_memory_capability
from civ_agent.orchestration.authored_plan_tools import (
    AUTHORED_PLAN_TOOLS,
    CITY_MUTATION_TOOLS,
    PLAN_READ_TOOLS,
    STRATEGY_MUTATION_TOOLS,
    CityPlanIntent,
    StrategyIntent,
    authored_plan_capability,
)
from civ_agent.orchestration.contracts import ModelSelection, Reference, RoleRequest
from civ_agent.orchestration.store import JournalConflict
from civ_agent.protocol import JsonObject
from tests.test_authored_memory import operation, setup


def request(identity, *, enabled=True):
    return RoleRequest(
        **identity.model_dump(),
        startup=Reference(sha256="a" * 64),
        catalogue=Reference(sha256="b" * 64),
        selection=ModelSelection(model="gpt-6-luna"),
        typed_authored_plans=enabled,
    )


def returned(messages):
    return [
        part
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]


def test_semantic_inputs_hide_code_owned_tags_and_preserve_document_validation():
    assert "type" not in CityPlanIntent.model_json_schema()["properties"]
    assert "type" not in StrategyIntent.model_json_schema()["properties"]
    city = CityPlanIntent(city="city2", reasons={"construction": "Choose Library"})
    assert city.model_dump(mode="json")["type"] == "city_plan"
    strategy = StrategyIntent(assessment="Develop before expanding")
    assert strategy.model_dump(mode="json")["type"] == "strategy"
    with pytest.raises(ValueError):
        CityPlanIntent.model_validate({"city": "city2", "reasons": {}, "metadata": "not model-owned"})
    choice = {
        "id": "same",
        "objective": "Develop",
        "investment": "Build",
        "expectedObservations": ["Growth"],
        "reviewAfterTurns": 3,
        "reconsiderWhen": "Threat",
    }
    with pytest.raises(ValueError, match="unique"):
        StrategyIntent.model_validate({"assessment": "Develop", "decisions": [choice, choice]})


@pytest.mark.parametrize("filename", ["strategy", " strategy ", "strategy.md", " city-city2 ", "city-city2.md"])
def test_generic_memory_cannot_mutate_reserved_document_aliases(tmp_path, filename):
    async def run():
        async with setup(tmp_path) as fixture:

            def model(messages, info):
                results = returned(messages)
                if not results:
                    return ModelResponse(
                        parts=[ToolCallPart("write_memory", {"file": filename, "content": "new content"}, "reserved")]
                    )
                assert results[0].outcome == "failed"
                return ModelResponse(parts=[TextPart("Use typed intent tools.")])

            agent = Agent(
                FunctionModel(model),
                deps_type=RoleRequest,
                retries=0,
                capabilities=[authored_memory_capability(fixture.client), authored_plan_capability(fixture.client)],
            )
            result = await agent.run("Maintain notes.", deps=request(fixture.identity))
            assert result.output == "Use typed intent tools." and result.usage.requests == 2
            assert await fixture.service.store.list_paths(fixture.prefix, limit=20) == []

    asyncio.run(run())


@pytest.mark.parametrize("purpose", ["gameplay", "economy", "military", "communication"])
def test_new_policy_exposes_only_role_scoped_typed_contracts(tmp_path, purpose):
    async def run():
        async with setup(tmp_path, purpose="communication" if purpose == "military" else purpose) as fixture:
            deps = request(fixture.identity).model_copy(update={"purpose": purpose})

            def model(messages, info):
                tools = {tool.name: tool for tool in info.function_tools}
                allowed = PLAN_READ_TOOLS
                if purpose in {"gameplay", "economy"}:
                    allowed |= CITY_MUTATION_TOOLS
                if purpose == "gameplay":
                    allowed |= STRATEGY_MUTATION_TOOLS
                assert set(tools) & AUTHORED_PLAN_TOOLS == allowed
                assert ("write_memory" in tools) == (purpose != "economy")
                assert ("delete_memory" in tools) == (purpose != "economy")
                for name in allowed:
                    schema = json.dumps(tools[name].parameters_json_schema)
                    for bookkeeping in ("old_text", "expected_version", "fingerprint", '"operation"', '"file"'):
                        assert bookkeeping not in schema
                return ModelResponse(parts=[TextPart("Reason about city intent.")])

            agent = Agent(
                FunctionModel(model),
                deps_type=RoleRequest,
                capabilities=[authored_memory_capability(fixture.client), authored_plan_capability(fixture.client)],
            )
            assert (await agent.run("Review intent.", deps=deps)).output == "Reason about city intent."

    asyncio.run(run())


def test_disabled_policy_preserves_legacy_tools_and_retry_behavior(tmp_path):
    async def run():
        async with setup(tmp_path) as fixture:

            def model(messages, info):
                assert not ({tool.name for tool in info.function_tools} & AUTHORED_PLAN_TOOLS)
                return ModelResponse(
                    parts=[ToolCallPart("write_memory", {"file": "strategy.md", "content": "bad"}, "bad")]
                )

            agent = Agent(
                FunctionModel(model),
                deps_type=RoleRequest,
                retries=0,
                capabilities=[authored_memory_capability(fixture.client), authored_plan_capability(fixture.client)],
            )
            with pytest.raises(UnexpectedModelBehavior):
                await agent.run("Legacy request.", deps=request(fixture.identity, enabled=False))

    asyncio.run(run())


def test_city_plan_is_replaced_through_semantic_tools_without_copying_json_or_versions(tmp_path):
    async def run():
        async with setup(tmp_path, purpose="economy") as fixture:
            old = {"type": "city_plan", "city": "city2", "reasons": {"construction": "Keep Granary"}}
            new = {
                "type": "city_plan",
                "city": "city2",
                "reasons": {
                    "construction": "Build Library when the current growth and production comparison supports it. " * 10
                },
            }
            path = fixture.prefix + "city-city2.md"
            await fixture.proxy.write(path, json.dumps(old), expected_version=None, operation=operation("seed"))
            seen = []
            requests = []
            original_post = fixture.client.post

            async def post(route, request):
                requests.append(request.call.action)
                return await original_post(route, request)

            fixture.client.post = post

            def model(messages, info):
                seen.append(messages)
                if len(seen) == 1:
                    return ModelResponse(parts=[ToolCallPart("read_city_plan", {"city": "city2"}, "read")])
                if len(seen) == 2:
                    content = returned(messages)[-1].content
                    assert isinstance(content, dict)
                    assert CityPlan.model_validate(dict(content).get("content")) == CityPlan.model_validate(old)
                    return ModelResponse(
                        parts=[
                            ToolCallPart(
                                "save_city_plan",
                                {"plan": {key: value for key, value in new.items() if key != "type"}},
                                "save",
                            )
                        ]
                    )
                content = returned(messages)[-1].content
                assert isinstance(content, dict) and dict(content).get("applied")
                return ModelResponse(parts=[TextPart("Updated the city plan.")])

            agent = Agent(
                FunctionModel(model),
                deps_type=RoleRequest,
                retries=0,
                capabilities=[authored_plan_capability(fixture.client)],
            )
            result = await agent.run(
                "Revise city intent.", deps=request(fixture.identity), usage_limits=UsageLimits(request_limit=3)
            )
            assert result.output == "Updated the city plan." and result.usage.requests == 3
            assert requests == ["read_document", "save_document"]
            saved = await fixture.proxy.read(path, max_chars=8000)
            assert CityPlan.model_validate_json(saved.content) == CityPlan.model_validate(new) and saved.version == "2"
            assert saved.metadata["job"] == fixture.identity.job and saved.metadata["purpose"] == "economy"

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["schema", "read_required", "policy", "reserved_memory"])
def test_two_rejected_optional_edits_do_not_abort_gameplay_or_spend_retry_allowances(tmp_path, failure):
    async def run():
        async with setup(tmp_path) as fixture:
            gameplay_calls = []

            async def gameplay() -> str:
                gameplay_calls.append(True)
                return "Native gameplay work continued."

            def model(messages, info):
                returns = returned(messages)
                if len(returns) < 2:
                    if failure == "reserved_memory":
                        return ModelResponse(
                            parts=[
                                ToolCallPart(
                                    "write_memory",
                                    {"file": "city-city2.md", "content": "a second JSON"},
                                    str(len(returns)),
                                )
                            ]
                        )
                    plan: JsonObject = {
                        "type": "city_plan",
                        "city": "city2",
                        "reasons": {"construction": "Choose Library"},
                    }
                    if failure == "schema":
                        plan["reasons"] = {"construction": 7}
                    elif failure == "policy":
                        plan["city"] = "city-not-owned"
                    return ModelResponse(parts=[ToolCallPart("save_city_plan", {"plan": plan}, str(len(returns)))])
                assert all(part.outcome == "failed" for part in returns[:2])
                if len(returns) == 2:
                    return ModelResponse(parts=[ToolCallPart("gameplay", {}, "act")])
                return ModelResponse(parts=[TextPart("Role continues.")])

            agent = Agent(
                FunctionModel(model),
                deps_type=RoleRequest,
                retries=0,
                tools=[gameplay],
                capabilities=[authored_memory_capability(fixture.client), authored_plan_capability(fixture.client)],
            )
            result = await agent.run(
                "Continue valid work after an optional edit fails.",
                deps=request(fixture.identity),
                usage_limits=UsageLimits(request_limit=4),
            )
            assert result.output == "Role continues." and result.usage.requests == 4 and gameplay_calls == [True]
            assert await fixture.proxy.read(fixture.prefix + "city-city2.md", max_chars=8000) is None

    asyncio.run(run())


def test_repeated_optional_failures_still_obey_the_original_request_limit(tmp_path):
    async def run():
        async with setup(tmp_path) as fixture:

            def model(messages, info):
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            "save_strategy",
                            {"strategy": {"assessment": "Develop first"}},
                            "same-model-call-id",
                        )
                    ]
                )

            agent = Agent(
                FunctionModel(model),
                deps_type=RoleRequest,
                retries=0,
                capabilities=[authored_plan_capability(fixture.client)],
            )
            with pytest.raises(UsageLimitExceeded):
                await agent.run(
                    "Repeated failed edits remain bounded.",
                    deps=request(fixture.identity),
                    usage_limits=UsageLimits(request_limit=3),
                )
            assert await fixture.proxy.read(fixture.prefix + "strategy.md", max_chars=8000) is None

    asyncio.run(run())


@pytest.mark.parametrize("error", [JournalConflict("EPOCH_CONFLICT"), RuntimeError("BUG")])
def test_integrity_and_programming_failures_are_not_reclassified_as_optional(tmp_path, error):
    async def run():
        async with setup(tmp_path) as fixture:

            class BrokenClient:
                async def post(self, name, payload):
                    raise error

            def model(messages, info):
                return ModelResponse(parts=[ToolCallPart("read_strategy", {}, "read")])

            agent = Agent(
                FunctionModel(model), deps_type=RoleRequest, capabilities=[authored_plan_capability(BrokenClient())]
            )
            with pytest.raises(type(error), match=str(error)):
                await agent.run("Read a plan.", deps=request(fixture.identity))

    asyncio.run(run())


_native_client = None
_native_persistence = StepPersistence(capture_frontier=True)


def native_client():
    assert _native_client is not None
    return _native_client


def native_plan_model(messages, info):
    prompt = messages[0].parts[0].content
    results = returned(messages)
    if prompt == "legacy":
        assert not ({tool.name for tool in info.function_tools} & AUTHORED_PLAN_TOOLS)
        return ModelResponse(parts=[TextPart("legacy tools unchanged")])
    if prompt == "roundtrip":
        calls = [
            ("read_city_plan", {"city": "city2"}),
            (
                "save_city_plan",
                {"plan": {"city": "city2", "reasons": {"construction": "Choose Library"}}},
            ),
            ("read_city_plan", {"city": "city2"}),
            ("delete_city_plan", {"city": "city2"}),
            ("read_city_plan", {"city": "city2"}),
        ]
        if len(results) < len(calls):
            tool, args = calls[len(results)]
            return ModelResponse(parts=[ToolCallPart(tool, args, "roundtrip" + str(len(results)))])
        assert all(part.outcome == "success" for part in results)
        return ModelResponse(parts=[TextPart("typed roundtrip complete")])
    if len(results) < 2:
        plan: JsonObject = {"city": "city2", "reasons": {"construction": "Choose Library"}}
        if prompt == "schema":
            plan["reasons"] = "invalid"
        return ModelResponse(parts=[ToolCallPart("save_city_plan", {"plan": plan}, "bad" + str(len(results)))])
    assert all(part.outcome == "failed" for part in results[:2])
    if len(results) == 2:
        return ModelResponse(parts=[ToolCallPart("continue_gameplay", {}, "valid-gameplay")])
    return ModelResponse(parts=[TextPart("gameplay continued")])


async def continue_gameplay() -> str:
    return "valid gameplay work completed"
