"""Native Responses providers retain typed tools, settings and usage without inference."""

import asyncio
import json

import httpx2
import pytest
from pydantic_ai import Agent, ToolOutput
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters

from civ_agent.orchestration.contracts import ModelSelection, RoleReport
from civ_agent.orchestration.provider_capabilities import ProviderModelContract, freeze_model_contract
from civ_agent.orchestration.provider_contracts import ModelCapabilities
from civ_agent.orchestration.providers import ProviderClients, model_settings
from civ_agent.peace_contracts import PeaceApproval, TacticalOverview
from civ_agent.strategic_review import StrategicAdvice
from civ_agent.tls import write_private
from tests.test_strategic_review import assessment


@pytest.mark.parametrize(
    "role,schema,report",
    [
        *[
            (role, RoleReport, {"summary": role + " prepared"})
            for role in ("gameplay", "economy", "military", "communication")
        ],
        ("review", StrategicAdvice, assessment()),
        ("peace_main", PeaceApproval, {"approve": True, "reason": "Exact terms reviewed"}),
        (
            "peace_military",
            TacticalOverview,
            {name: "Known frozen evidence" for name in TacticalOverview.model_fields},
        ),
    ],
)
def test_public_router_responses_adapter_supports_native_role_tools_outputs_and_usage(tmp_path, role, schema, report):
    async def run():
        requests = []

        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            assert str(request.url) == "https://openrouter.ai/api/v1/responses"
            assert request.headers["authorization"] == "Bearer synthetic-test-key"
            assert body["model"] == "openai/gpt-6-luna"
            assert body["reasoning"]["effort"] == "medium"
            assert body["provider"] == {"require_parameters": True}
            assert "service_tier" not in body
            assert {tool["name"] for tool in body["tools"]} == {"read_state", "submit_report"}
            name, arguments = ("read_state", {}) if len(requests) == 1 else ("submit_report", report)
            return httpx2.Response(
                200,
                json={
                    "id": f"resp-{len(requests)}",
                    "object": "response",
                    "created_at": 0,
                    "status": "completed",
                    "model": body["model"],
                    "output": [
                        {
                            "id": f"fc-{len(requests)}",
                            "type": "function_call",
                            "call_id": f"call-{len(requests)}",
                            "name": name,
                            "arguments": json.dumps(arguments),
                            "status": "completed",
                        }
                    ],
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 7,
                        "total_tokens": 17,
                        "input_tokens_details": {"cached_tokens": 3},
                        "output_tokens_details": {"reasoning_tokens": 2},
                    },
                },
            )

        key = tmp_path / "router-key"
        write_private(key, b"synthetic-test-key")
        clients = ProviderClients(openrouter_key_file=key, transport=httpx2.MockTransport(respond))
        selection = router_selection()
        try:
            contract = await clients.prepare(selection, catalog=catalog(selection.model))
            assert not requests
            model = await clients.model(selection, contract=contract)

            async def read_state() -> dict:
                return {"turn": 3, "role": role}

            agent = Agent(model, output_type=ToolOutput(schema, name="submit_report"), tools=[read_state], retries=0)
            result = await asyncio.wait_for(
                agent.run(
                    "Exercise frozen typed interfaces",
                    model_settings=model_settings(selection),
                ),
                5,
            )
            assert result.output == schema.model_validate(report)
            usage = result.usage
            assert (usage.requests, usage.input_tokens, usage.cache_read_tokens, usage.output_tokens) == (
                2,
                20,
                6,
                14,
            )
            assert usage.details["reasoning_tokens"] == 4
            assert len(requests) == 2
        finally:
            await clients.close()

    asyncio.run(run())


def router_selection(**changes):
    return ModelSelection(provider="openrouter", authentication="api_key", model="openai/gpt-6-luna", **changes)


def catalog(model, **changes):
    return ModelCapabilities.model_validate(
        {
            "id": model,
            "context_length": 32000,
            "supported_parameters": ["tools", "structured_outputs", "reasoning"],
            "reasoning": {"mandatory": False, "supported_efforts": ["none", "low", "medium", "high", "xhigh", "max"]},
            "endpoints": ["/api/v1/responses"],
            **changes,
        }
    )


def test_frozen_contract_survives_client_reconstruction_without_catalog_or_inference_reads(tmp_path):
    async def run():
        key = tmp_path / "router-key"
        write_private(key, b"synthetic-test-key")
        selection = router_selection()
        forbidden = httpx2.MockTransport(lambda _: pytest.fail("Preflight must not make HTTP requests"))
        first = ProviderClients(openrouter_key_file=key, transport=forbidden)
        try:
            contract = await first.prepare(selection, catalog=catalog(selection.model))
            frozen = contract.model_dump_json()
        finally:
            await first.close()
        restored = ProviderModelContract.model_validate_json(frozen)
        second = ProviderClients(openrouter_key_file=key, transport=forbidden)
        try:
            model = await second.model(selection, contract=restored)
            assert model.context_window == 32000
            assert restored.fingerprint == contract.fingerprint and "synthetic-test-key" not in frozen
            assert await second.model(selection, contract=restored) is model
            different = await second.model(selection)
            assert different is not model
        finally:
            await second.close()

    asyncio.run(run())


@pytest.mark.parametrize("changed", ["selection", "profile", "settings", "sdk", "catalog"])
def test_changed_contract_cannot_reach_inference_or_reuse_the_valid_cached_model(tmp_path, changed):
    async def run():
        key = tmp_path / "router-key"
        write_private(key, b"synthetic-test-key")
        clients = ProviderClients(
            openrouter_key_file=key,
            transport=httpx2.MockTransport(lambda _: pytest.fail("Changed contracts must not reach HTTP")),
        )
        selection = router_selection()
        try:
            contract = await clients.prepare(selection, catalog=catalog(selection.model))
            await clients.model(selection, contract=contract)
            wire = contract.model_dump(mode="json")
            if changed == "selection":
                wire["selection_sha256"] = "f" * 64
            elif changed == "profile":
                wire["profile"]["supports_forced_tool_choice"] = not wire["profile"]["supports_forced_tool_choice"]
            elif changed == "settings":
                wire["settings"]["max_retries"] = 1
            elif changed == "sdk":
                wire["sdk_version"] = "different-sdk"
            else:
                wire["catalog"]["context_length"] += 1
            altered = ProviderModelContract.model_validate(wire)
            assert altered.fingerprint != contract.fingerprint
            with pytest.raises(ValueError, match="CONFLICT|CHANGED"):
                await clients.model(selection, contract=altered)
        finally:
            await clients.close()

    asyncio.run(run())


def test_managed_preparation_requires_router_catalog_and_refuses_unknown_context(tmp_path):
    async def run():
        key = tmp_path / "openai-key"
        write_private(key, b"synthetic-test-key")
        clients = ProviderClients(openai_key_file=key)
        try:
            with pytest.raises(ValueError, match="CATALOG_REQUIRED"):
                await clients.prepare(router_selection())
            with pytest.raises(ValueError, match="CONTEXT_WINDOW_UNKNOWN"):
                await clients.prepare(
                    ModelSelection(model="not-a-real-model", authentication="api_key", reasoning_effort="none")
                )
        finally:
            await clients.close()

    asyncio.run(run())


def test_contract_cannot_describe_another_models_profile(tmp_path):
    async def run():
        key = tmp_path / "openai-key"
        write_private(key, b"synthetic-test-key")
        clients = ProviderClients(openai_key_file=key)
        try:
            selection = ModelSelection(model="gpt-6-luna", authentication="api_key")
            model = await clients.model(selection)
            with pytest.raises(ValueError, match="IDENTITY_CONFLICT"):
                freeze_model_contract(selection.model_copy(update={"model": "gpt-6-sol"}), model)
            with pytest.raises(ValueError, match="IDENTITY_CONFLICT"):
                freeze_model_contract(selection.model_copy(update={"authentication": "subscription"}), model)
        finally:
            await clients.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "change,error",
    [
        ({"supported_parameters": ["structured_outputs", "reasoning"]}, "tool calling"),
        ({"supported_parameters": ["tools", "reasoning"]}, "structured outputs"),
        ({"supported_parameters": ["tools", "structured_outputs"]}, "reasoning"),
        ({"reasoning": {"supported_efforts": ["low"]}}, "effort"),
        ({"endpoints": ["/api/v1/chat/completions"]}, "RESPONSES_UNSUPPORTED"),
    ],
)
def test_catalog_rejects_unsupported_requests_before_creating_any_client(tmp_path, change, error):
    async def run():
        selection = router_selection()
        clients = ProviderClients(openrouter_key_file=tmp_path / "must-not-read")
        try:
            with pytest.raises(ValueError, match=error):
                await clients.prepare(selection, catalog=catalog(selection.model, **change))
            assert not clients.models
        finally:
            await clients.close()

    asyncio.run(run())


@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-6-sol", "gpt-6.2-astra", "gpt-6.2-sol"])
def test_subscription_defaults_keep_medium_and_context_without_fetching_tokens(model):
    class Broker:
        async def fetch(self, **_):
            pytest.fail("Preflight must not request a token")

    async def run():
        clients = ProviderClients(broker=Broker())
        try:
            selection = ModelSelection(model=model)
            contract = await clients.prepare(selection)
            actual = await clients.model(selection, contract=contract)
            _, parameters = actual.prepare_request(
                model_settings(selection), ModelRequestParameters(output_mode="tool")
            )
            assert parameters.thinking == "medium" and contract.context_window == 1050000
            assert contract.catalog is None  # SDK metadata is not account availability.
        finally:
            await clients.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "selection,error",
    [
        (ModelSelection(model="not-a-real-model"), "REASONING_UNSUPPORTED"),
        (ModelSelection(model="gpt-6-luna", reasoning_effort="max"), "EFFORT_UNSUPPORTED"),
        (ModelSelection(model="gpt-6.1-terra", reasoning_effort="max"), "EFFORT_UNSUPPORTED"),
        (ModelSelection(model="gpt-6.1-sol", reasoning_effort="none"), "REASONING_REQUIRED"),
        *[
            (ModelSelection(model=model, reasoning_effort=effort), "REASONING_REQUIRED|EFFORT_UNSUPPORTED")
            for model in ("gpt-6.2-astra", "gpt-6.2-sol")
            for effort in ("none", "minimal")
        ],
    ],
)
def test_subscription_preflight_does_not_silently_change_unsupported_effort(selection, error):
    class Broker:
        async def fetch(self, **_):
            pytest.fail("Preflight must not request a token")

    async def run():
        clients = ProviderClients(broker=Broker())
        try:
            with pytest.raises(ValueError, match=error):
                await clients.prepare(selection)
        finally:
            await clients.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "authentication,effort",
    [
        (authentication, effort)
        for authentication, efforts in (
            ("subscription", ("low", "medium", "high", "xhigh")),
            ("api_key", ("low", "medium", "high", "xhigh", "max")),
        )
        for effort in efforts
    ],
)
@pytest.mark.parametrize("model_name", ["gpt-6.1-terra", "gpt-6.2-astra", "gpt-6.2-sol"])
def test_advertised_efforts_reach_the_sdk_request_without_aliasing(tmp_path, authentication, effort, model_name):
    class Broker:
        async def fetch(self, **_):
            return {"accessToken": "synthetic-test-token", "chatgptAccountId": "synthetic-test-account"}

    async def run():
        key = tmp_path / "openai-key"
        write_private(key, b"synthetic-test-key")
        requests = []

        def respond(request):
            requests.append(json.loads(request.content))
            expected_url = (
                "https://chatgpt.com/backend-api/codex/responses"
                if authentication == "subscription"
                else "https://api.openai.com/v1/responses"
            )
            assert str(request.url) == expected_url
            assert requests[-1]["reasoning"]["effort"] == effort
            assert requests[-1]["model"] == model_name
            if model_name.startswith("gpt-6.2-"):
                assert "reasoning.encrypted_content" in requests[-1]["include"]
            return httpx2.Response(503, json={"error": {"message": "Synthetic service failure"}})

        clients = ProviderClients(broker=Broker(), openai_key_file=key, transport=httpx2.MockTransport(respond))
        selection = ModelSelection(model=model_name, authentication=authentication, reasoning_effort=effort)
        try:
            contract = await clients.prepare(selection)
            assert not requests
            model = await clients.model(selection, contract=contract)
            with pytest.raises(ModelHTTPError):
                await model.request(
                    [ModelRequest(parts=[UserPromptPart("Synthetic request")])],
                    model_settings(selection),
                    ModelRequestParameters(),
                )
            assert len(requests) == 1
        finally:
            await clients.close()

    asyncio.run(run())


def test_priority_and_zero_hidden_retries_are_preserved_by_responses_client(tmp_path):
    async def run():
        key = tmp_path / "openai-key"
        write_private(key, b"synthetic-test-key")
        requests = []

        def respond(request):
            requests.append(json.loads(request.content))
            assert str(request.url) == "https://api.openai.com/v1/responses"
            assert requests[-1]["service_tier"] == "priority"
            assert requests[-1]["reasoning"]["effort"] == "high"
            return httpx2.Response(503, json={"error": {"message": "Synthetic service failure"}})

        clients = ProviderClients(openai_key_file=key, transport=httpx2.MockTransport(respond))
        selection = ModelSelection(model="gpt-6-sol", authentication="api_key", fast=True, reasoning_effort="high")
        try:
            contract = await clients.prepare(selection)
            model = await clients.model(selection, contract=contract)
            with pytest.raises(ModelHTTPError):
                await asyncio.wait_for(
                    model.request(
                        [ModelRequest(parts=[UserPromptPart("Synthetic request")])],
                        model_settings(selection),
                        ModelRequestParameters(),
                    ),
                    5,
                )
            assert len(requests) == 1
            with pytest.raises(ValueError, match="FAST_UNSUPPORTED"):
                await clients.prepare(router_selection(fast=True), catalog=catalog("openai/gpt-6-luna"))
        finally:
            await clients.close()

    asyncio.run(run())
