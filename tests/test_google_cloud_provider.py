"""Google Cloud uses the native Gemini transport and the shared role lifecycle."""

import asyncio
import json
import stat
from datetime import UTC, datetime, timedelta
from typing import Annotated
from unittest.mock import AsyncMock, Mock

import google.auth
import httpx2
import pytest
from google.oauth2.credentials import Credentials
from pydantic import BaseModel, Field
from pydantic_ai import Agent, ToolOutput
from pydantic_ai.exceptions import ModelAuthenticationError, ModelConnectionError, ModelUnavailableError
from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.tools import ToolDefinition
from pydantic_ai_harness.step_persistence import SqliteStepStore

from civ_agent import container_supervisor as supervisor
from civ_agent import google_cloud_auth
from civ_agent.commands import game, runtime
from civ_agent.execution_policy import ExecutionPolicy
from civ_agent.game_config import GameConfig
from civ_agent.google_cloud_auth import CONFIG_NAME, CREDENTIALS_NAME, import_login, saved_config
from civ_agent.orchestration.contracts import GatewayReceipt, ModelSelection, RoleReport
from civ_agent.orchestration.gateway_tools import gateway_tools
from civ_agent.orchestration.google_cloud_model import GoogleCloudModel
from civ_agent.orchestration.managed_player import NativePlayerSession, NativePlayerSettings
from civ_agent.orchestration.provider_capabilities import prepare_profile_contract
from civ_agent.orchestration.provider_preparation import prepare_managed_sessions
from civ_agent.orchestration.providers import ProviderClients, model_settings
from civ_agent.orchestration.worker_configuration import GoogleCloudConfig, WorkerProviderConfig, provider_clients
from civ_agent.peace_contracts import PeaceApproval
from civ_agent.runtime.agents import LocalAgentRunner, LocalAgentServices
from civ_agent.runtime.model_policy import LocalModelPolicy, ModelPolicySettings, SanitizeProviderErrors
from civ_agent.strategic_review import StrategicAdvice
from civ_agent.tls import write_private_json
from civ_agent.tool_schemas import tool_model
from tests.test_local_agents import request as role_request
from tests.test_strategic_review import assessment


def selection(model="gemini-3.8-flash", **changes):
    return ModelSelection(provider="google-cloud", authentication="adc", model=model, **changes)


def adc_file(path):
    write_private_json(
        path,
        {
            "type": "authorized_user",
            "client_id": "synthetic-client",
            "client_secret": "synthetic-secret",
            "refresh_token": "synthetic-refresh",
            "quota_project_id": "old-project",
        },
    )
    return path


def cloud_clients(tmp_path, monkeypatch, respond, *, project="credited-project", location="global"):
    loaded = Mock(return_value=Credentials("synthetic-access-token", quota_project_id=project))
    monkeypatch.setattr(google_cloud_auth, "load_credentials", loaded)
    clients = ProviderClients(
        google_cloud=GoogleCloudConfig(
            project=project, location=location, credentials_file=adc_file(tmp_path / "adc.json")
        ),
        transport=httpx2.MockTransport(respond),
    )
    return clients, loaded


def response(model, part):
    return httpx2.Response(
        200,
        json={
            "modelVersion": model,
            "responseId": "synthetic-response",
            "candidates": [{"content": {"role": "model", "parts": [part]}, "finishReason": "STOP"}],
            "usageMetadata": {
                "promptTokenCount": 10,
                "cachedContentTokenCount": 3,
                "candidatesTokenCount": 5,
                "thoughtsTokenCount": 2,
                "totalTokenCount": 17,
            },
        },
    )


@pytest.mark.parametrize("mode", ["request", "stream", "count_tokens"])
@pytest.mark.parametrize("timeout", [None, 3])
def test_production_google_client_keeps_sdk_long_decode_timeout_and_closes(tmp_path, monkeypatch, mode, timeout):
    async def run():
        monkeypatch.setattr(
            google_cloud_auth, "load_credentials", Mock(return_value=Credentials("synthetic-access-token"))
        )
        clients = ProviderClients(
            google_cloud=GoogleCloudConfig(project="credited-project", credentials_file=adc_file(tmp_path / "adc.json"))
        )
        sent = []

        async def send(http, request, **_kwargs):
            sent.append(http)
            assert http.timeout.read == 20 * 60
            assert http.timeout.connect == 5
            expected = 20 * 60 if timeout is None else timeout
            assert request.extensions["timeout"]["read"] == expected
            assert request.headers["x-server-timeout"] == str(expected)
            if mode == "count_tokens":
                answer = httpx2.Response(200, json={"totalTokens": 42})
            elif mode == "stream":
                body = response("gemini-3.8-flash", {"text": "Ready"})
                answer = httpx2.Response(
                    200, content=b"data: " + body.content + b"\n\n", headers={"content-type": "text/event-stream"}
                )
            else:
                answer = response("gemini-3.8-flash", {"text": "Ready"})
            answer.request = request
            return answer

        monkeypatch.setattr(httpx2.AsyncClient, "send", send)
        try:
            model = await clients.model(selection())
            messages: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart("Read state")])]
            settings = model_settings(selection())
            if timeout is not None:
                settings["timeout"] = timeout
            parameters = ModelRequestParameters()
            if mode == "stream":
                async with model.request_stream(messages, settings, parameters) as streamed:
                    async for _ in streamed:
                        pass
                    assert streamed.get().text == "Ready"
            elif mode == "count_tokens":
                assert (await model.count_tokens(messages, settings, parameters)).input_tokens == 42
            else:
                assert (await model.request(messages, settings, parameters)).text == "Ready"
            assert len(sent) == 1 and not sent[0].is_closed
        finally:
            await clients.close()
        assert sent[0].is_closed

    asyncio.run(run())


@pytest.mark.parametrize("model", ["gemini-3.8-flash", "gemini-4-argon", "gemini-3.5-flash-lite"])
@pytest.mark.parametrize(
    "schema,report",
    [
        (RoleReport, {"summary": "Role completed"}),
        (StrategicAdvice, assessment()),
        (PeaceApproval, {"approve": True, "reason": "Exact terms reviewed"}),
    ],
)
def test_native_gemini_tools_typed_reports_thinking_and_usage(tmp_path, monkeypatch, model, schema, report):
    async def run():
        requests = []

        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            assert request.url.host == "aiplatform.googleapis.com"
            assert request.url.path.endswith(
                f"/projects/credited-project/locations/global/publishers/google/models/{model}:generateContent"
            )
            assert request.headers["authorization"] == "Bearer synthetic-access-token"
            assert request.headers["x-goog-user-project"] == "credited-project"
            assert "key" not in request.url.params
            assert body["generationConfig"]["thinkingConfig"]["thinking_level"] == "MEDIUM"
            declarations = [tool for group in body["tools"] for tool in group.get("functionDeclarations", [])]
            assert {tool["name"] for tool in declarations} == {"read_state", "submit_report"}
            assert "service_tier" not in body and "reasoning" not in body
            if len(requests) == 1:
                part = {"functionCall": {"name": "read_state", "args": {}}, "thoughtSignature": "c2lnbmF0dXJl"}
            else:
                parts = [part for content in body["contents"] for part in content["parts"]]
                assert any(part.get("functionResponse", {}).get("name") == "read_state" for part in parts)
                assert any(part.get("thoughtSignature") == "c2lnbmF0dXJl" for part in parts)
                part = {"functionCall": {"name": "submit_report", "args": report}}
            return response(model, part)

        clients, loaded = cloud_clients(tmp_path, monkeypatch, respond)
        try:
            chosen = selection(model)
            contract = await clients.prepare(chosen)
            assert not requests and contract.api_family == "generate-content"
            assert contract.context_window and contract.context_window >= 1_000_000
            actual = await clients.model(chosen, contract=contract)

            async def read_state() -> dict:
                return {"turn": 3}

            agent = Agent(actual, tools=[read_state], output_type=ToolOutput(schema, name="submit_report"))
            result = await agent.run("Complete this role", model_settings=model_settings(chosen))
            assert result.output == schema.model_validate(report)
            usage = result.usage
            assert (usage.requests, usage.input_tokens, usage.cache_read_tokens, usage.output_tokens) == (2, 20, 6, 14)
            assert usage.output_reasoning_tokens == 4
            assert all(call.kwargs["project"] == "credited-project" for call in loaded.call_args_list)
            assert "synthetic-" not in contract.model_dump_json()
        finally:
            await clients.close()

    asyncio.run(run())


def test_offline_preparation_and_resume_do_not_resolve_google_credentials_or_send_requests(monkeypatch):
    async def run():
        forbidden = Mock(side_effect=AssertionError("Offline preparation must not resolve credentials"))
        monkeypatch.setattr(google.auth, "default", forbidden)
        monkeypatch.setattr(google.auth, "load_credentials_from_file", forbidden)
        monkeypatch.setattr(google_cloud_auth, "load_credentials", forbidden)
        config = GameConfig(agents={"provider": "google-cloud", "model": "gemini-4-argon", "fast": True}).resolved()
        settings = NativePlayerSettings.model_validate(config.settings(config.agent_players[0]))
        original = NativePlayerSession(game="game", player="p0", settings=settings)
        prepared = await prepare_managed_sessions([original])
        assert all(contract.api_family == "generate-content" for contract in prepared[0].provider_contracts.values())
        assert await prepare_managed_sessions([original], frozen=prepared) == prepared
        assert original.get_selection("gameplay").authentication == "adc"
        assert not original.get_selection("gameplay").fast
        forbidden.assert_not_called()

    asyncio.run(run())


def test_gcloud_adc_login_is_private_and_container_mounts_only_selected_files(tmp_path, monkeypatch, capsys):
    source = adc_file(tmp_path / "source.json")
    root = tmp_path / "auth"
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(source))
    args = runtime.parser(group="auth", action="login").parse_args(
        ["--provider", "google-cloud", "--project", "credited-project", "--auth-dir", str(root)]
    )
    assert runtime.run(args) == 0
    config = saved_config(root)
    assert config.project == "credited-project" and config.location == "global"
    assert config.credentials_file == root / CREDENTIALS_NAME
    assert config.credentials_file is not None
    assert config.credentials_file.read_bytes() == source.read_bytes()
    assert stat.S_IMODE(config.credentials_file.stat().st_mode) == 0o600
    argv = " ".join(supervisor.agent_auth_arguments(root, "Rome", "google-cloud"))
    assert CREDENTIALS_NAME in argv and CONFIG_NAME in argv and ":ro" in argv
    assert "auth-ipc" not in argv and "openrouter.key" not in argv and str(source) not in argv
    status = runtime.parser(group="auth", action="status").parse_args(
        ["--provider", "google-cloud", "--auth-dir", str(root)]
    )
    assert runtime.run(status) == 0
    output = capsys.readouterr().out
    assert "credited-project" in output and "synthetic-" not in output
    assert provider_clients(WorkerProviderConfig(google_cloud=config)).google_cloud == config


def test_google_cloud_cli_and_subordinate_roles_select_adc():
    args = game.parser().parse_args(["--provider", "google-cloud", "--model", "gemini-3.8-flash"])
    config = game.resolve_options(args)
    settings = NativePlayerSettings.model_validate(config.settings(config.agent_players[0]))
    for role in ("gameplay", "communication", "economy", "military", "review", "peace_main", "peace_military"):
        assert settings.get_selection(role) == selection()


def test_native_adc_loader_overrides_old_quota_project(tmp_path):
    credentials = google_cloud_auth.load_credentials(adc_file(tmp_path / "adc.json"), project="credited-project")
    assert credentials.quota_project_id == "credited-project"


def test_expired_google_credentials_refresh_without_changing_cache_identity(tmp_path, monkeypatch):
    async def run():
        tokens = []

        def respond(request):
            tokens.append(request.headers["authorization"])
            return response("gemini-3.8-flash", {"text": "Ready"})

        clients, loaded = cloud_clients(tmp_path, monkeypatch, respond)
        credentials = loaded.return_value

        def refresh(_request):
            credentials.token = "synthetic-renewed-token"
            credentials.expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)

        credentials.expiry = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
        renewal = Mock(side_effect=refresh)
        monkeypatch.setattr(credentials, "refresh", renewal)
        try:
            model = await clients.model(selection())
            before = await clients.cache_account_identity(selection(), model=model)
            result = await Agent(model).run("Continue", model_settings=model_settings(selection()))
            assert result.output == "Ready" and tokens == ["Bearer synthetic-renewed-token"]
            renewal.assert_called_once()
            assert before == await clients.cache_account_identity(selection(), model=model)
        finally:
            await clients.close()

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["transport", "retryable", "invalid_login"])
def test_google_auth_failures_follow_native_retry_categories(tmp_path, monkeypatch, failure):
    from google.auth.exceptions import RefreshError, TransportError

    async def run():
        requests = []

        def respond(request):
            requests.append(request)
            return response("gemini-3.8-flash", {"text": "Ready"})

        clients, loaded = cloud_clients(tmp_path, monkeypatch, respond)
        credentials = loaded.return_value
        credentials.expiry = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)

        def refresh(_request):
            if renewal.call_count == 1:
                if failure == "transport":
                    raise TransportError("synthetic-private-diagnostic")
                raise RefreshError("synthetic-private-diagnostic", retryable=failure == "retryable")
            credentials.token = "renewed-token"
            credentials.expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)

        renewal = Mock(side_effect=refresh)
        monkeypatch.setattr(credentials, "refresh", renewal)
        try:
            model = await clients.model(selection())
            sleeps = AsyncMock()
            agent = Agent(
                model,
                capabilities=[
                    SanitizeProviderErrors(),
                    LocalModelPolicy(ModelPolicySettings(initial_delay=0, maximum_delay=0), sleep=sleeps),
                ],
            )
            if failure == "invalid_login":
                with pytest.raises(ModelAuthenticationError) as raised:
                    await agent.run("Continue", model_settings=model_settings(selection()))
                assert "synthetic-private-diagnostic" not in str(raised.value)
                assert not requests and sleeps.await_count == 0
            else:
                result = await agent.run("Continue", model_settings=model_settings(selection()))
                assert result.output == "Ready" and renewal.call_count == 2
                assert sleeps.await_count == 1 and len(requests) == 1
        finally:
            await clients.close()

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["request", "stream", "count_tokens"])
@pytest.mark.parametrize(
    "failure,expected",
    [("transport", ModelConnectionError), ("retryable", ModelUnavailableError), ("login", ModelAuthenticationError)],
)
def test_google_model_normalizes_adc_errors_without_agent_hooks(tmp_path, monkeypatch, mode, failure, expected):
    from google.auth.exceptions import RefreshError, TransportError

    async def run():
        clients, loaded = cloud_clients(tmp_path, monkeypatch, lambda _: pytest.fail("Refresh precedes inference"))
        credentials = loaded.return_value
        credentials.expiry = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
        error = (
            TransportError("private-refresh-detail")
            if failure == "transport"
            else RefreshError("private-refresh-detail", retryable=failure == "retryable")
        )
        credentials.refresh = Mock(side_effect=error)
        try:
            model = await clients.model(selection())
            messages: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart("Read state")])]
            with pytest.raises(expected) as raised:
                if mode == "stream":
                    async with model.request_stream(messages, model_settings(selection()), ModelRequestParameters()):
                        pytest.fail("Refresh must fail before the stream opens")
                else:
                    await getattr(model, mode)(messages, model_settings(selection()), ModelRequestParameters())
            assert str(raised.value) == "GOOGLE_CLOUD_AUTH_ERROR"
            assert raised.value.model_name == selection().model
            credentials.refresh.assert_called_once()
        finally:
            await clients.close()

    asyncio.run(run())


@pytest.mark.parametrize("code_mode", [False, True])
def test_google_native_role_runner_executes_tools_and_code_mode(tmp_path, monkeypatch, code_mode):
    async def run():
        requests, effects = [], []

        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            declarations = [tool for group in body["tools"] for tool in group.get("functionDeclarations", [])]
            finish = next(tool for tool in declarations if tool["name"] == "finish_role")
            assert finish["parameters_json_schema"]["properties"]["requests"]["maxItems"] == 100
            assert body["toolConfig"]["functionCallingConfig"]["mode"] == "AUTO"
            assert (
                tool_model("finish_role", "gameplay").model_json_schema()["properties"]["requests"]["maxItems"] == 100
            )
            if len(requests) == 1:
                names = {tool["name"] for tool in declarations}
                assert ("run_code" in names) == code_mode
                name, args = ("run_code", {"code": "view = player_turn()\nview"}) if code_mode else ("player_turn", {})
            else:
                name, args = "finish_role", {"summary": "Reviewed the current turn"}
            part = {"functionCall": {"name": name, "args": args}, "thoughtSignature": "c2lnbmF0dXJl"}
            return response("gemini-3.8-flash", part)

        clients, _ = cloud_clients(tmp_path, monkeypatch, respond)
        try:
            chosen = selection()
            contract = await clients.prepare(chosen)
            model = await clients.model(chosen, contract=contract)
            deps = role_request().model_copy(
                update={
                    "selection": chosen,
                    "provider_contract": contract,
                    **({"execution_policy": ExecutionPolicy.current()} if code_mode else {}),
                }
            )

            async def invoke(_deps, _identifier, _step, decision):
                effects.append(decision)
                return GatewayReceipt(result={"roleFinished": True} if decision["kind"] == "finish" else {"turn": 3})

            runner = LocalAgentRunner(
                LocalAgentServices(
                    invoke=invoke,
                    step_store=SqliteStepStore(database=tmp_path / "steps.sqlite3"),
                    overflow_root=tmp_path / "overflow",
                )
            )
            result = await runner.run_role(
                deps,
                startup={"user": "Review the current turn", "instructions": "Use the permitted tools"},
                catalogue=[{"name": "player_turn", "inputSchema": {}}, {"name": "finish_role", "inputSchema": {}}],
                model=model,
            )
            assert result.report.summary == "Reviewed the current turn"
            assert [decision["kind"] for decision in effects] == ["inspect", "finish"]
            assert result.usage.requests == 2
        finally:
            await clients.close()

    asyncio.run(run())


def test_google_standard_tool_mode_keeps_full_schema_and_pydantic_validation(tmp_path, monkeypatch):
    class Report(BaseModel):
        choices: Annotated[list[str], Field(max_length=2)]
        maxItems: str

    async def run():
        requests = []

        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            declaration = body["tools"][0]["functionDeclarations"][0]
            properties = declaration["parameters_json_schema"]["properties"]
            assert properties["choices"]["maxItems"] == 2
            assert properties["maxItems"]["type"] == "string"
            assert body["toolConfig"]["functionCallingConfig"]["mode"] == "AUTO"
            if len(requests) == 1:
                return response("gemini-3.8-flash", {"text": "The role is done."})
            choices = ["one", "two", "three"] if len(requests) == 2 else ["one"]
            return response(
                "gemini-3.8-flash",
                {"functionCall": {"name": "submit_report", "args": {"choices": choices, "maxItems": "kept"}}},
            )

        clients, _ = cloud_clients(tmp_path, monkeypatch, respond)
        try:
            model = await clients.model(selection())
            agent = Agent(model, output_type=ToolOutput(Report, name="submit_report"), retries=2)
            result = await agent.run("Choose", model_settings=model_settings(selection()))
            assert result.output == Report(choices=["one"], maxItems="kept")
            assert len(requests) == 3
            assert Report.model_json_schema()["properties"]["choices"]["maxItems"] == 2
        finally:
            await clients.close()

    asyncio.run(run())


def test_google_actual_construction_union_keeps_schema_and_valid_inputs(tmp_path, monkeypatch):
    from jsonschema import Draft202012Validator

    async def run():
        invoke = AsyncMock()
        tool = gateway_tools("gameplay", [{"name": "construction_details", "inputSchema": {}}], invoke)[0]
        definition = tool.tool_def
        canonical = json.dumps(definition.parameters_json_schema, sort_keys=True)
        validator = Draft202012Validator(definition.parameters_json_schema)
        examples = [
            {"target": "city1"},
            {"target": "city1", "horizonTurns": None},
            {"target": "city1", "names": ["Granary"], "horizonTurns": 3},
            {"target": "city1", "names": [], "horizonTurns": 3},
            {"target": "city1", "horizonTurns": 3},
            {"target": "city1", "names": ["Granary"], "horizonTurns": 101},
            {"names": ["Granary"]},
        ]
        expected = [True, True, True, False, False, False, False]
        assert [validator.is_valid(value) for value in examples] == expected
        calls = []

        def respond(request):
            calls.append(request)
            body = json.loads(request.content)
            schema = body["tools"][0]["functionDeclarations"][0]["parameters_json_schema"]
            assert "anyOf" in schema and "properties" not in schema
            assert body["toolConfig"]["functionCallingConfig"]["mode"] == "AUTO"
            assert [Draft202012Validator(schema).is_valid(value) for value in examples] == expected
            return response("gemini-3.8-flash", {"text": "Ready"})

        clients, _ = cloud_clients(tmp_path, monkeypatch, respond)
        try:
            model = await clients.model(selection())
            await model.request(
                [ModelRequest(parts=[UserPromptPart("Read help")])],
                model_settings(selection()),
                ModelRequestParameters(function_tools=[definition]),
            )
            assert len(calls) == 1
            assert json.dumps(definition.parameters_json_schema, sort_keys=True) == canonical
            invoke.assert_not_awaited()
        finally:
            await clients.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "choice,strict,allow_text,expected",
    [
        (None, None, False, "AUTO"),
        (None, True, False, "ANY"),
        (None, True, True, "VALIDATED"),
        ("auto", None, True, "AUTO"),
        ("required", None, True, "ANY"),
        ("none", None, True, "NONE"),
    ],
)
def test_google_default_mode_preserves_explicit_strict_and_forced_choices(
    tmp_path, monkeypatch, choice, strict, allow_text, expected
):
    async def run():
        def respond(request):
            config = json.loads(request.content)["toolConfig"]["functionCallingConfig"]
            assert config["mode"] == expected
            return response("gemini-3.8-flash", {"text": "Ready"})

        clients, _ = cloud_clients(tmp_path, monkeypatch, respond)
        try:
            parameters = ModelRequestParameters(
                allow_text_output=allow_text,
                function_tools=[
                    ToolDefinition(
                        name="read_state", parameters_json_schema={"type": "object", "properties": {}}, strict=strict
                    )
                ],
            )
            settings = model_settings(selection())
            if choice is not None:
                settings["tool_choice"] = choice
            model = await clients.model(selection())
            await model.request([ModelRequest(parts=[UserPromptPart("Read state")])], settings, parameters)
            assert parameters.function_tools[0].strict is strict
            assert settings.get("tool_choice") == choice
        finally:
            await clients.close()

    asyncio.run(run())


def test_google_model_level_forced_choice_is_preserved(tmp_path, monkeypatch):
    async def run():
        def respond(request):
            assert json.loads(request.content)["toolConfig"]["functionCallingConfig"]["mode"] == "ANY"
            return response("gemini-3.8-flash", {"functionCall": {"name": "read_state", "args": {}}})

        clients, _ = cloud_clients(tmp_path, monkeypatch, respond)
        try:
            base = await clients.model(selection())
            assert isinstance(base, GoogleCloudModel)
            provider = base.provider
            assert provider is not None
            model = GoogleCloudModel(selection().model, provider=provider, settings={"tool_choice": "required"})
            await model.request(
                [ModelRequest(parts=[UserPromptPart("Read state")])],
                None,
                ModelRequestParameters(
                    function_tools=[
                        ToolDefinition(name="read_state", parameters_json_schema={"type": "object", "properties": {}})
                    ]
                ),
            )
        finally:
            await clients.close()

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["request", "stream", "count_tokens"])
@pytest.mark.parametrize("with_file", [False, True])
def test_google_tool_json_references_are_lossless_text_on_every_transport(tmp_path, monkeypatch, mode, with_file):
    async def run():
        schema = {
            "results": [
                {
                    "schema": {
                        "$defs": {"MoveCommand": {"type": "object", "description": "Déplacer"}},
                        "properties": {"commands": {"items": {"$ref": "#/$defs/MoveCommand"}}},
                    }
                }
            ]
        }
        files = [BinaryContent(data=b"synthetic-image", media_type="image/png")] if with_file else []
        part = ToolReturnPart(
            "command_help", [schema, *files] if files else schema, "call-help", metadata={"retained": True}
        )
        ordinary = ToolReturnPart("player_turn", {"turn": 1, "text": "#/$defs/MoveCommand"}, "call-turn")
        messages = [
            ModelRequest(parts=[UserPromptPart("Read command help")]),
            ModelResponse(
                parts=[ToolCallPart("command_help", {}, "call-help"), ToolCallPart("player_turn", {}, "call-turn")]
            ),
            ModelRequest(parts=[part, ordinary]),
        ]
        original = ModelMessagesTypeAdapter.dump_json(messages)
        requests = []

        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            returns = {
                part["functionResponse"]["name"]: part["functionResponse"]
                for content in body["contents"]
                for part in content["parts"]
                if "functionResponse" in part
            }
            help_result = returns["command_help"]
            assert list(help_result["response"]) == ["return_value"]
            text = help_result["response"]["return_value"]
            assert isinstance(text, str)
            assert json.loads(text) == part.model_response_object(wrap_if_error=False).get("return_value", schema)
            assert returns["player_turn"]["response"] == ordinary.content
            assert bool(help_result.get("parts")) == with_file
            if with_file:
                assert help_result["parts"][0]["inlineData"]["mime_type"] == "image/png"
            if mode == "count_tokens":
                assert request.url.path.endswith(":countTokens")
                return httpx2.Response(200, json={"totalTokens": 42})
            answer = response("gemini-3.8-flash", {"text": "Ready"})
            if mode == "stream":
                assert request.url.path.endswith(":streamGenerateContent")
                return httpx2.Response(
                    200, content=b"data: " + answer.content + b"\n\n", headers={"content-type": "text/event-stream"}
                )
            return answer

        clients, _ = cloud_clients(tmp_path, monkeypatch, respond)
        try:
            model = await clients.model(selection())
            settings, parameters = model_settings(selection()), ModelRequestParameters()
            if mode == "count_tokens":
                assert (await model.count_tokens(messages, settings, parameters)).input_tokens == 42
            elif mode == "stream":
                async with model.request_stream(messages, settings, parameters) as streamed:
                    async for _ in streamed:
                        pass
                    assert streamed.get().text == "Ready"
            else:
                assert (await model.request(messages, settings, parameters)).text == "Ready"
            assert len(requests) == 1
            assert ModelMessagesTypeAdapter.dump_json(messages) == original
            assert part.files == files and part.metadata == {"retained": True}
        finally:
            await clients.close()

    asyncio.run(run())


def test_google_agent_keeps_structured_reference_results_in_history(tmp_path, monkeypatch):
    async def run():
        payload = {"schema": {"$ref": "#/$defs/MoveCommand", "$defs": {"MoveCommand": {"type": "object"}}}}
        requests = []

        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            if len(requests) == 1:
                part = {"functionCall": {"name": "command_help", "args": {}}, "thoughtSignature": "c2lnbmF0dXJl"}
            else:
                returned = body["contents"][-1]["parts"][0]["functionResponse"]["response"]
                assert json.loads(returned["return_value"]) == payload
                part = {"text": "Ready"}
            return response("gemini-3.8-flash", part)

        clients, _ = cloud_clients(tmp_path, monkeypatch, respond)
        try:

            async def command_help() -> dict:
                return payload

            result = await Agent(await clients.model(selection()), tools=[command_help]).run("Read help")
            assert result.output == "Ready" and len(requests) == 2
            returns = [
                part
                for message in result.all_messages()
                if isinstance(message, ModelRequest)
                for part in message.parts
                if isinstance(part, ToolReturnPart)
            ]
            assert len(returns) == 1 and returns[0].content == payload
        finally:
            await clients.close()

    asyncio.run(run())


@pytest.mark.parametrize("status", [429, 500, 503])
def test_google_transient_errors_use_existing_visible_retries(tmp_path, monkeypatch, status):
    async def run():
        requests = []

        def respond(request):
            requests.append(json.loads(request.content))
            if len(requests) < 3:
                return httpx2.Response(status, json={"error": {"code": status, "message": "Synthetic failure"}})
            return response("gemini-3.8-flash", {"text": "Recovered"})

        clients, _ = cloud_clients(tmp_path, monkeypatch, respond)
        try:
            chosen = selection()
            model = await clients.model(chosen)
            sleeps = AsyncMock()
            agent = Agent(
                model,
                capabilities=[LocalModelPolicy(ModelPolicySettings(initial_delay=0, maximum_delay=0), sleep=sleeps)],
            )
            result = await agent.run("Continue", model_settings=model_settings(chosen))
            assert result.output == "Recovered" and len(requests) == 3
            assert requests[0] == requests[1] == requests[2] and sleeps.await_count == 2
        finally:
            await clients.close()

    asyncio.run(run())


def test_no_google_sdk_retries_or_environment_key_fallback(tmp_path, monkeypatch):
    async def run():
        requests = []
        monkeypatch.setenv("GOOGLE_API_KEY", "wrong-ai-studio-key")

        def respond(request):
            requests.append(request)
            assert "key" not in request.url.params
            assert request.headers["authorization"] == "Bearer synthetic-access-token"
            return httpx2.Response(503, json={"error": {"code": 503, "message": "Synthetic failure"}})

        clients, _ = cloud_clients(tmp_path, monkeypatch, respond)
        try:
            chosen = selection()
            model = await clients.model(chosen)
            with pytest.raises(ModelUnavailableError):
                await model.request(
                    [ModelRequest(parts=[UserPromptPart("Hello")])], model_settings(chosen), ModelRequestParameters()
                )
            assert len(requests) == 1
        finally:
            await clients.close()

    asyncio.run(run())


def test_cloud_cache_scope_separates_billing_projects_and_locations(tmp_path, monkeypatch):
    async def run():
        identities, endpoints = set(), set()
        for project, location in (("first", "global"), ("second", "global"), ("first", "us-central1")):
            clients, _ = cloud_clients(
                tmp_path, monkeypatch, lambda _: pytest.fail("No inference"), project=project, location=location
            )
            try:
                model = await clients.model(selection())
                identities.add(await clients.cache_account_identity(selection(), model=model))
                endpoints.add(clients.cache_endpoint(selection()))
            finally:
                await clients.close()
        assert len(identities) == len(endpoints) == 3

    asyncio.run(run())


def test_google_only_container_launch_needs_no_subscription_or_router(tmp_path, monkeypatch):
    root, auth = tmp_path / "match", tmp_path / "auth"
    import_login(auth, project="credited-project", credentials_file=adc_file(tmp_path / "source.json"))
    config = GameConfig(agents={"provider": "google-cloud", "model": "gemini-3.8-flash"}).resolved()
    write_private_json(root / "resolved.json", config.model_dump())
    record = supervisor.identity(root)
    record.update(auth_root=str(auth), images={"runtime": "sha256:" + "1" * 64})
    write_private_json(root / "container.json", record)
    login, authorize = Mock(), Mock()
    monkeypatch.setattr(supervisor, "ensure_auth", login)
    monkeypatch.setattr(supervisor, "authorize_agent", authorize)
    monkeypatch.setattr(supervisor, "cleanup", lambda _: None)
    monkeypatch.setattr(supervisor, "create_network", lambda *_, **__: {"subnets": [{"subnet": "10.55.0.0/24"}]})
    monkeypatch.setattr(supervisor, "firewall", lambda *_: None)
    monkeypatch.setattr(supervisor.GameRun, "await_ready", lambda *_: None)
    run = supervisor.GameRun(root)
    started = []
    monkeypatch.setattr(run, "start", lambda arguments, *_: started.append(arguments))
    run.setup()
    login.assert_not_called()
    authorize.assert_not_called()
    assert len(started) == 1
    arguments = " ".join(started[0])
    assert CREDENTIALS_NAME in arguments and CONFIG_NAME in arguments
    assert "/auth-ipc" not in arguments and "openrouter.key" not in arguments and "synthetic-" not in arguments


def test_preflight_rejects_google_subscription_and_priority_without_authentication():
    async def run():
        for chosen, error in (
            (selection().model_copy(update={"authentication": "subscription"}), "GOOGLE_CLOUD_ADC_REQUIRED"),
            (selection(fast=True), "FAST_UNSUPPORTED"),
        ):
            with pytest.raises(ValueError, match=error):
                await prepare_profile_contract(chosen)

    asyncio.run(run())
