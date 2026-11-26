"""Frozen execution choices, conservative cache routing, and retention economics."""

import json
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from typing import cast

import httpx2
import pytest
from google.auth.credentials import AnonymousCredentials
from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai.messages import InstructionPart, ModelMessagesTypeAdapter, ModelResponse, TextPart
from pydantic_ai.models import ModelRequestContext, ModelRequestParameters
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIResponsesModel, OpenAIResponsesModelSettings
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.google_cloud import GoogleCloudProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.usage import RequestUsage
from pydantic_ai_harness.step_persistence import SqliteStepStore

from civ_agent.cache_replay import CacheBreakpoint, CacheObservation, CacheTier, replay_cache, select_cache_tier
from civ_agent.execution_policy import (
    CachePolicy,
    CodeModePolicy,
    ExecutionPolicy,
    FocusWindowPolicy,
    legacy_execution_policy,
)
from civ_agent.game_config import AgentDefaults
from civ_agent.orchestration.contracts import ModelSelection, RoleRequest
from civ_agent.orchestration.providers import ProviderClients
from civ_agent.runtime.agents import LocalAgentRunner, LocalAgentServices
from civ_agent.runtime.cache_policy import ProviderCaching, cache_retention, prepared_prefix, reusable_prefix
from civ_agent.runtime.player import LocalPlayer
from civ_agent.runtime.state import AttemptRecord
from tests.domain_fixtures import run_async
from tests.test_local_agents import request
from tests.test_provider_transport_contracts import _response, _Subscription


def test_saved_records_keep_legacy_policy_without_changing_serialized_fingerprints():
    defaults = AgentDefaults.model_validate({})
    role = request()
    attempt = AttemptRecord(id="attempt", player="p1", civilization="Rome", epoch="e1", turn=0, handoff=0)
    for record in (defaults, role, attempt):
        assert record.execution_policy == legacy_execution_policy()
        assert "execution_policy" not in record.model_dump(mode="json")
    frozen = role.model_copy(update={"execution_policy": ExecutionPolicy.current()})
    restored = RoleRequest.model_validate_json(frozen.model_dump_json())
    assert restored.execution_policy.fingerprint == frozen.execution_policy.fingerprint
    with pytest.raises(ValidationError):
        restored.execution_policy.code_mode.__setattr__("enabled", False)
    with pytest.raises(ValidationError, match="LEGACY_EXECUTION_POLICY_CHANGED"):
        ExecutionPolicy(version=0, code_mode=CodeModePolicy(enabled=True))


def test_retention_uses_supported_defaults_without_changing_subscription_policy():
    short = CachePolicy(selection_reason="shortest-supported")
    assert cache_retention(short, provider="openai", authentication="api_key") == "30m"
    assert cache_retention(short, provider="anthropic", authentication="api_key") == "5m"
    assert cache_retention(short, provider="google", authentication="api_key") is True
    override = CachePolicy(retention="1h", selection_reason="explicit-override")
    assert cache_retention(override, provider="anthropic", authentication="api_key") == "1h"
    assert cache_retention(override, provider="openai", authentication="subscription") is True
    with pytest.raises(ValidationError, match="CACHE_SELECTION_EVIDENCE_REQUIRED"):
        CachePolicy(retention="1h", selection_reason="measured-savings")


def test_focused_windows_are_explicit_frozen_v2_choices_without_hybrid_mode():
    old = ExecutionPolicy.model_validate_json('{"version":1,"code_mode":{"enabled":true}}')
    assert not old.code_mode.focused_windows.enabled
    assert not old.intent_handoff_review
    assert old.fingerprint == "59ebc236b5e6b9bd723e4b7776048a886eed42ce1b2cbfadcf0bb28dac48adcb"
    selected = ExecutionPolicy.current()
    assert selected.version == 2 and selected.code_mode.focused_windows.enabled
    assert selected.intent_handoff_review
    restored = ExecutionPolicy.model_validate_json(selected.model_dump_json())
    assert restored.fingerprint == selected.fingerprint
    with pytest.raises(ValidationError):
        restored.code_mode.focused_windows.__setattr__("enabled", False)
    with pytest.raises(ValidationError, match="FOCUSED_WINDOWS_REQUIRE_EXECUTION_POLICY_V2"):
        ExecutionPolicy(
            version=1, code_mode=CodeModePolicy(enabled=True, focused_windows=FocusWindowPolicy(enabled=True))
        )
    with pytest.raises(ValidationError, match="FOCUSED_WINDOWS_REQUIRE_CODE_MODE"):
        ExecutionPolicy(version=2, code_mode=CodeModePolicy(focused_windows=FocusWindowPolicy(enabled=True)))
    with pytest.raises(ValidationError, match="extra_forbidden"):
        CodeModePolicy.model_validate({"enabled": True, "hybrid": True})


@pytest.mark.parametrize(
    "values",
    [
        {"max_seconds": 0},
        {"max_seconds": 31},
        {"max_calls": 0},
        {"max_calls": 13},
        {"queue_timeout": 0},
        {"queue_timeout": float("inf")},
    ],
)
def test_focused_window_caps_remain_bounded(values):
    with pytest.raises(ValidationError):
        FocusWindowPolicy.model_validate(values)


@run_async
async def test_write_duration_telemetry_uses_explicit_buckets_and_keeps_absence_unknown():
    capability = ProviderCaching(ExecutionPolicy(), ModelSelection(model="test"), account_scope="known-account")
    context = SimpleNamespace(run_id="run", run_step=1)
    request_context = SimpleNamespace(attempt=1)
    usage = RequestUsage(input_tokens=300, cache_write_tokens=200, cache_write_1h_tokens=80)
    first = await capability.after_model_request(
        context, request_context=request_context, response=ModelResponse(parts=[], usage=usage)
    )
    assert first.metadata["cache"]["cache_write_1h_tokens"] == 80
    assert first.metadata["cache"]["cache_write_5m_tokens"] is None
    usage.details["cache_write_5m_tokens"] = 120
    second = await capability.after_model_request(
        context, request_context=request_context, response=ModelResponse(parts=[], usage=usage)
    )
    assert second.metadata["cache"]["cache_write_5m_tokens"] == 120


def test_openai_api_translation_is_owned_by_sdk():
    model = OpenAIResponsesModel("gpt-6-luna", provider=OpenAIProvider(api_key="unused"))
    capability = ProviderCaching(
        ExecutionPolicy(), ModelSelection(model="gpt-6-luna", authentication="api_key"), account_scope="account-one"
    )
    settings, parameters = model.prepare_request(
        capability.get_model_settings(), ModelRequestParameters(instruction_parts=[InstructionPart("Public rules")])
    )
    values = cast(dict[str, object], settings)
    assert values["openai_prompt_cache_options"] == {"mode": "implicit", "ttl": "30m"}
    assert values["openai_cache_instructions"] is True
    assert parameters.cache == "30m"
    effective = model.resolve_cache_retention(settings)
    assert effective is not None and effective.total_seconds() == 1800


def test_anthropic_and_gemini_adapters_translate_without_new_credential_flows():
    parameters = ModelRequestParameters(instruction_parts=[InstructionPart("Public rules")])
    model = AnthropicModel("claude-sonnet-4-5", provider=AnthropicProvider(api_key="unused"))
    for retention in ("5m", "1h"):
        settings, _ = model.prepare_request({"cache": retention}, parameters)
        values = cast(dict[str, object], settings)
        assert values["anthropic_cache_instructions"] == retention
        assert values["anthropic_cache_tool_definitions"] == retention
        assert values["anthropic_cache"] == retention
    model = GoogleModel("gemini-2.5-flash", provider=GoogleProvider(api_key="unused"))
    settings, prepared = model.prepare_request({"cache": True}, parameters)
    assert settings is None and prepared.cache is None
    assert model.resolve_cache_retention(settings) is None
    vertex = GoogleModel(
        "gemini-2.5-flash",
        provider=GoogleCloudProvider(
            project="offline-test", location="us-central1", credentials=AnonymousCredentials()
        ),
    )
    settings, prepared = vertex.prepare_request({"cache": True}, parameters)
    assert settings is None and prepared.cache is None
    assert vertex.resolve_cache_retention(settings) is None


@run_async
async def test_routing_uses_prepared_tools_instructions_model_account_and_endpoint():
    model = OpenAIResponsesModel("gpt-6-luna", provider=OpenAIProvider(api_key="unused"))
    parameters = ModelRequestParameters(
        function_tools=[ToolDefinition(name="inspect", parameters_json_schema={"type": "object", "properties": {}})],
        instruction_parts=[InstructionPart("Public rules"), InstructionPart("Injected catalogue", dynamic=True)],
    )
    context = ModelRequestContext(
        model=model, messages=[], model_settings={"cache": "30m"}, model_request_parameters=parameters
    )
    ctx = SimpleNamespace(run_id="run", run_step=1)

    async def route(*, account="one", endpoint=None, params=parameters, serving=model):
        capability = ProviderCaching(
            ExecutionPolicy(),
            ModelSelection(model=serving.model_name, authentication="api_key"),
            account_scope=account,
            endpoint=endpoint,
        )
        prepared = await capability.prepare_model_request(
            ctx, replace(context, model=serving, model_request_parameters=params)
        )
        response = await capability.after_model_request(
            ctx, request_context=prepared, response=ModelResponse(parts=[TextPart("ok")])
        )
        assert response.metadata is not None
        assert response.metadata["cache"]["cache_read_tokens"] is None
        return cast(dict[str, object], prepared.model_settings)["openai_prompt_cache_key"], response.metadata["cache"]

    first, metadata = await route()
    assert first == (await route())[0]
    assert metadata["effective_retention_seconds"] == 1800
    assert metadata["prepared_prefix_bytes"] > 0
    assert first != (await route(account="two"))[0]
    assert first != (await route(endpoint="https://different.example/v1"))[0]
    changed = replace(parameters, instruction_parts=[InstructionPart("Changed public rules")])
    assert first != (await route(params=changed))[0]
    changed = replace(parameters, function_tools=[ToolDefinition(name="another")])
    assert first != (await route(params=changed))[0]
    private_change = replace(
        parameters,
        instruction_parts=[
            InstructionPart("Public rules"),
            InstructionPart("Private role contribution ID", dynamic=True),
        ],
    )
    private_route, private_metadata = await route(params=private_change)
    assert private_route == first
    assert private_metadata["public_prefix_sha256"] == metadata["public_prefix_sha256"]
    assert private_metadata["prepared_prefix_sha256"] != metadata["prepared_prefix_sha256"]
    assert (
        first
        != (await route(serving=OpenAIResponsesModel("gpt-6.1-terra", provider=OpenAIProvider(api_key="unused"))))[0]
    )
    assert "Public rules" not in str(metadata)
    assert prepared_prefix(parameters)["instructions"][1]["content"] == "Injected catalogue"


def test_dynamic_instruction_before_static_content_ends_actual_reusable_prefix():
    parameters = ModelRequestParameters(
        instruction_parts=[
            InstructionPart("Leading public rules"),
            InstructionPart("Private observation", dynamic=True),
            InstructionPart("Later static material"),
        ]
    )
    full = prepared_prefix(parameters)
    reusable = reusable_prefix(full)
    assert [part["content"] for part in full["instructions"]] == [
        "Leading public rules",
        "Private observation",
        "Later static material",
    ]
    assert [part["content"] for part in reusable["instructions"]] == ["Leading public rules"]


@run_async
async def test_volatile_user_content_keeps_cache_route_and_response_provenance():
    seen = []

    def respond(messages, info):
        seen.append(cast(dict[str, object], info.model_settings)["openai_prompt_cache_key"])
        return ModelResponse(parts=[TextPart("Done")], usage=RequestUsage(input_tokens=100, cache_read_tokens=80))

    routes = []
    for task in ("private turn 1", "private turn 2"):
        capability = ProviderCaching(ExecutionPolicy(), ModelSelection(model="test"), account_scope="same-account")
        agent = Agent(FunctionModel(respond), instructions="Shared rules", capabilities=[capability])
        result = await agent.run(task)
        metadata = result.all_messages()[-1].metadata
        assert metadata is not None
        cache = metadata["cache"]
        assert cache["input_tokens"] == 100 and cache["cache_read_tokens"] == 80
        assert cache["cache_write_tokens"] is None and cache["cost_usd"] is None
        routes.append(cache["routing_family_sha256"])
    assert seen[0] == seen[1] and routes[0] == routes[1]


@run_async
async def test_unknown_accounts_are_isolated_between_runs():
    routes = []

    def respond(messages, info):
        routes.append(cast(dict[str, object], info.model_settings)["openai_prompt_cache_key"])
        return ModelResponse(parts=[TextPart("Done")])

    capability = ProviderCaching(ExecutionPolicy(), ModelSelection(model="test"), account_scope=None)
    agent = Agent(FunctionModel(respond), instructions="Shared rules", capabilities=[capability])
    await agent.run("One")
    await agent.run("Two")
    assert routes[0] != routes[1]


@pytest.mark.parametrize("policy", [legacy_execution_policy(), ExecutionPolicy()])
@run_async
async def test_affinity_uses_serving_scope_preserves_case_overrides_and_current_logical_identity(policy):
    seen = []

    async def run(
        *,
        account="one",
        endpoint="https://chatgpt.com/backend-api/codex",
        model="gpt-6-luna",
        headers=None,
        logical="first",
    ):
        capability = ProviderCaching(policy, ModelSelection(model=model), account_scope=account, endpoint=endpoint)

        def respond(messages, info):
            seen.append(info.model_settings)
            return ModelResponse(parts=[TextPart("ok")])

        agent = Agent(FunctionModel(respond, model_name=model), instructions="Common", capabilities=[capability])
        await agent.run("Current", conversation_id=logical, model_settings={"extra_headers": headers or {}})
        return seen[-1]

    first, second = await run(), await run(logical="second")
    assert first["extra_headers"]["session-id"] == second["extra_headers"]["session-id"]
    assert second["extra_headers"]["thread-id"] == "second"
    assert first.get("openai_prompt_cache_key") == second.get("openai_prompt_cache_key")
    for change in ({"account": "two"}, {"endpoint": "https://other.example"}, {"model": "serving-model"}):
        assert (await run(**change))["extra_headers"]["session-id"] != first["extra_headers"]["session-id"]
    assert (await run(account=None))["extra_headers"]["session-id"] != (await run(account=None))["extra_headers"][
        "session-id"
    ]
    overrides = {"SeSsIoN-Id": "caller-session", "THREAD-ID": "caller-thread", "X-CLIENT-REQUEST-ID": "caller-request"}
    assert (await run(headers=overrides))["extra_headers"] == overrides


@pytest.mark.parametrize("policy", [legacy_execution_policy(), ExecutionPolicy(), ExecutionPolicy.current()])
@run_async
async def test_default_runner_affinity_covers_legacy_jobs_and_current_conversation_without_rewriting_history(
    tmp_path, policy
):
    wires = []

    def respond(wire):
        wires.append({"headers": dict(wire.headers), "body": json.loads(wire.content)})
        return _response(wires[-1]["body"])

    clients = ProviderClients(broker=_Subscription(), transport=httpx2.MockTransport(respond))
    try:
        selection = ModelSelection(model="gpt-6-luna")
        model = await clients.model(selection)
        deps = request().model_copy(update={"execution_policy": policy, "selection": selection})
        player = object.__new__(LocalPlayer)
        player.clients = clients
        account, endpoint = await player._cache_owner(deps, model=model)
        assert account is not None

        async def no_actions(*_):
            raise AssertionError("Unexpected native action")

        runner = LocalAgentRunner(
            LocalAgentServices(
                invoke=no_actions,
                step_store=SqliteStepStore(database=tmp_path / "steps.sqlite3"),
                cache_account_scope=account,
                cache_endpoint=endpoint,
            )
        )
        agent = Agent(
            model, instructions="Stable instructions", capabilities=runner._capabilities(deps, name="affinity")
        )
        settings = OpenAIResponsesModelSettings(openai_prompt_cache_key="caller-key")
        first = await agent.run("First", deps=deps, conversation_id="first", model_settings=settings)
        history = first.all_messages()
        original = ModelMessagesTypeAdapter.dump_json(history)
        await agent.run(
            "Next",
            deps=deps,
            message_history=history,
            conversation_id="current",
            model_settings=settings,
        )
        assert ModelMessagesTypeAdapter.dump_json(history) == original
        assert len(wires) == 2
        assert wires[0]["headers"]["session-id"] == wires[1]["headers"]["session-id"]
        assert wires[0]["headers"]["thread-id"] == "first"
        assert wires[1]["headers"]["thread-id"] == wires[1]["headers"]["x-client-request-id"] == "current"
        assert wires[0]["body"]["prompt_cache_key"] == wires[1]["body"]["prompt_cache_key"]
        if policy.version == 0:
            assert wires[0]["body"]["prompt_cache_key"] == "caller-key"
    finally:
        await clients.close()


@run_async
async def test_subscription_account_identity_survives_token_renewal_and_fences_changed_account():
    class Broker:
        account = "first-account"
        token = "first-token"

        async def fetch(self, *, refresh=False, generation=None):
            return {"chatgptAccountId": self.account, "accessToken": self.token}

    broker = Broker()
    seen = []

    def respond(request):
        seen.append(request)
        return httpx2.Response(200)

    clients = ProviderClients(broker=broker, transport=httpx2.MockTransport(respond))
    selection = ModelSelection(model="gpt-6-luna")
    try:
        model = await clients.model(selection)
        first = await clients.cache_account_identity(selection, model=model)
        assert first is not None and "first-account" not in first and "first-token" not in first
        broker.token = "renewed-token"
        assert await clients.cache_account_identity(selection, model=model) == first
        broker.account = "second-account"
        with pytest.raises(ValueError, match="CACHE_ACCOUNT_CHANGED"):
            await clients.cache_account_identity(selection, model=model)
        authentication = clients.model_auth[id(model)]
        async with httpx2.AsyncClient(auth=authentication, transport=httpx2.MockTransport(respond)) as client:
            with pytest.raises(ValueError, match="CACHE_ACCOUNT_CHANGED"):
                await client.post(clients.cache_endpoint(selection) + "/responses")
        assert seen == []
    finally:
        await clients.close()


@run_async
async def test_api_cache_identity_matches_the_retained_client_even_when_key_file_changes(tmp_path):
    path = tmp_path / "key"
    path.write_text("first-key")
    path.chmod(0o600)
    clients = ProviderClients(openai_key_file=path)
    selection = ModelSelection(model="gpt-6-luna", authentication="api_key")
    try:
        model = await clients.model(selection)
        first = await clients.cache_account_identity(selection, model=model)
        path.write_text("second-key")
        assert await clients.cache_account_identity(selection, model=model) == first
        changed = selection.model_copy(update={"reasoning_effort": "low"})
        different = await clients.model(changed)
        assert await clients.cache_account_identity(changed, model=different) != first
        assert (
            await clients.cache_account_identity(selection, model=FunctionModel(lambda *_: ModelResponse(parts=[])))
            is None
        )
    finally:
        await clients.close()


def tier(name, seconds, *, write="1.25", storage="0", refresh=True):
    return CacheTier(
        name=name,
        retention_seconds=seconds,
        input_per_million=Decimal(1),
        read_per_million=Decimal("0.1"),
        write_per_million=Decimal(write),
        storage_per_million_token_hours=Decimal(storage),
        refresh_on_read=refresh,
        provenance="frozen-test-prices",
    )


def observation(timestamp, *, family="shared", points=(("public", 1000),), tokens=1000):
    return CacheObservation(
        timestamp=timestamp,
        family=family,
        input_tokens=tokens,
        breakpoints=tuple(CacheBreakpoint(fingerprint=name, tokens=count) for name, count in points),
    )


def test_gap_replay_refreshes_reads_and_counts_overlapping_breakpoints_once():
    events = [observation(0), observation(240), observation(480)]
    estimate = replay_cache(events, tier("short", 300))
    assert (estimate.read_tokens, estimate.write_tokens, estimate.uncached_tokens) == (2000, 1000, 0)
    assert replay_cache(events, tier("no-refresh", 300, refresh=False)).write_tokens == 2000
    overlap = [observation(0, points=(("public", 1000), ("role", 2000)), tokens=2200)]
    estimate = replay_cache(overlap, tier("explicit", 3600, storage="2"))
    assert (estimate.read_tokens, estimate.write_tokens, estimate.uncached_tokens) == (0, 2000, 200)
    assert estimate.storage_token_hours == Decimal(2000)
    assert estimate.estimated_usd == Decimal("0.0067")
    # Distinct branches share their common prefix's storage exactly once.
    branches = [*overlap, observation(0, points=(("public", 1000), ("other-role", 1500)), tokens=1500)]
    assert replay_cache(branches, tier("explicit", 3600)).storage_token_hours == Decimal(2500)


def test_longer_tier_requires_more_than_ten_percent_estimated_savings():
    short, long = tier("5m", 300), tier("1h", 3600, write="2")
    sparse = [observation(0), observation(600)]
    assert select_cache_tier([replay_cache(sparse, value) for value in (short, long)], [short, long]).tier == "1h"
    expensive = tier("expensive", 3600, write="2.2")
    estimates = [replay_cache(sparse, value) for value in (short, expensive)]
    # 2.30 / 2.50 is an eight percent improvement, so keep the shorter tier.
    assert select_cache_tier(estimates, [short, expensive]).tier == "5m"
    assert replay_cache([observation(0), observation(600, family="another")], short).read_tokens == 0
    with pytest.raises(ValueError, match="OUT_OF_ORDER"):
        replay_cache(list(reversed(sparse)), short)
