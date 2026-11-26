"""Managed workers select native providers; fixture execution is explicit and isolated."""

import asyncio
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import httpx2
import pytest
from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel

from civ_agent import container_auth
from civ_agent.orchestration.contracts import (
    ModelSelection,
    PeacePhase,
    PeaceRequest,
    Reference,
    ReviewRequest,
    RoleRequest,
    Scope,
    digest,
)
from civ_agent.orchestration.providers import model_settings
from civ_agent.orchestration.worker_configuration import WorkerProviderConfig, model_factories, provider_clients
from civ_agent.tls import write_private


def test_configuration_import_does_not_load_sdk_or_credential_owner():
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from civ_agent.orchestration.worker_configuration import WorkerProviderConfig; "
            "WorkerProviderConfig(scripted=True); "
            "assert not any(n in sys.modules for n in ('pydantic_ai', 'authlib', 'civ_agent.subscription_auth'))",
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        timeout=10,
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"scripted": "true"},
        {"scripted": True, "refresh_token": "PRIVATE_REFRESH_TOKEN"},
        {"scripted": True, "access_token": "PRIVATE_ACCESS_TOKEN"},
        {"scripted": True, "base_url": "https://untrusted.example"},
        {"scripted": True, "openai_key_file": "/run/secrets/openai.key"},
        {"broker_socket": "/auth-ipc/broker.sock"},
        {"broker_token_file": "/run/secrets/client.json"},
        {"openai_key_file": "relative.key"},
        {"openrouter_key_file": "https://untrusted.example/key"},
        {"openai_key_file": "/run/secrets/key\nother"},
        {"openai_key_file": "/run/secrets/key\x00other"},
    ],
)
def test_worker_configuration_rejects_ambiguous_auth_and_raw_credentials(payload):
    with pytest.raises(ValidationError) as raised:
        WorkerProviderConfig.model_validate(payload)
    assert "PRIVATE_" not in str(raised.value)


def test_real_factories_use_the_existing_native_provider_resolution():
    config = WorkerProviderConfig.model_validate({"openrouter_key_file": "/not-mounted-yet/router.key"})
    factories = model_factories(config)
    assert factories.role is factories.review is factories.peace is None


@pytest.mark.parametrize("kind", ["role", "review", "peace"])
def test_fixture_factories_require_the_exact_fixture_selection(kind):
    scope = Scope(game="game", player="p0", epoch="epoch", activation="fixture")
    reference = Reference(sha256=digest("frozen input"))

    def request(model):
        selection = ModelSelection(model=model)
        if kind == "role":
            return RoleRequest(
                scope=scope,
                job="job",
                purpose="gameplay",
                startup=reference,
                catalogue=reference,
                selection=selection,
            )
        if kind == "review":
            return ReviewRequest(scope=scope, inputs=reference, selection=selection)
        return PeacePhase(
            request=PeaceRequest(scope=scope, basis=digest("basis"), inputs=reference, selection=selection),
            phase="military",
        )

    factory = getattr(model_factories(WorkerProviderConfig(scripted=True)), kind)
    assert isinstance(factory(request("scripted-v1")), FunctionModel)
    with pytest.raises(ValueError, match="SCRIPTED_MODEL_REQUIRED"):
        factory(request("gpt-6.1-sol"))


def response_body(model):
    return {
        "id": "resp_worker",
        "object": "response",
        "created_at": 1,
        "model": model,
        "status": "completed",
        "output": [
            {
                "type": "message",
                "id": "msg_worker",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "Worker configured.", "annotations": []}],
            }
        ],
        "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
    }


def responses_stream(model):
    response = response_body(model)
    message = response["output"][0]
    events = [
        {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {**message, "status": "in_progress", "content": []},
        },
        {
            "type": "response.content_part.added",
            "item_id": message["id"],
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        },
        {
            "type": "response.output_text.delta",
            "item_id": message["id"],
            "output_index": 0,
            "content_index": 0,
            "delta": "Worker configured.",
        },
        {"type": "response.output_item.done", "output_index": 0, "item": message},
        {"type": "response.completed", "response": response},
    ]
    return "".join("data: " + json.dumps(event) + "\n\n" for event in events)


@pytest.mark.parametrize("provider", ["subscription", "openai", "openrouter"])
def test_configured_worker_calls_native_sdk_with_selected_auth_effort_and_priority(tmp_path, monkeypatch, provider):
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-openai-must-not-be-used")
    monkeypatch.setenv("OPENROUTER_API_KEY", "ambient-router-must-not-be-used")
    monkeypatch.setenv("HTTPS_PROXY", "https://ambient-proxy-must-not-be-used.invalid")
    wire, fetches, http_options, broker_paths = [], [], [], []
    if provider == "subscription":
        payload = {
            "broker_socket": str(tmp_path / "mounted auth/broker.sock"),
            "broker_token_file": str(tmp_path / "mounted auth/client.json"),
        }
        selection = ModelSelection(model="gpt-6.1-sol", reasoning_effort="high", fast=True)
    else:
        key = tmp_path / (provider + ".key")
        write_private(key, ("synthetic-" + provider + "-key\n").encode())
        payload = {provider + "_key_file": str(key)}
        selection = ModelSelection(
            provider=provider,
            authentication="api_key",
            model="vendor/test" if provider == "openrouter" else "gpt-6.1-sol",
            reasoning_effort="high",
            fast=provider == "openai",
        )
    config = WorkerProviderConfig.model_validate(payload)

    class Broker:
        def __init__(self, socket_path, token_file):
            broker_paths.append((socket_path, token_file))

        async def fetch(self, *, refresh=False, generation=None):
            fetches.append(refresh)
            if refresh:
                assert generation == hashlib.sha256(b"synthetic-initial").hexdigest()
            return {
                "accessToken": "synthetic-renewed" if refresh else "synthetic-initial",
                "chatgptAccountId": "synthetic-account",
            }

    monkeypatch.setattr(container_auth, "TokenClient", Broker)

    def respond(request):
        wire.append((request.url, dict(request.headers), json.loads(request.content)))
        if provider == "subscription" and len(wire) == 1:
            return httpx2.Response(401)
        if provider == "subscription":
            return httpx2.Response(
                200,
                text=responses_stream(selection.model),
                headers={"content-type": "text/event-stream"},
            )
        return httpx2.Response(200, json=response_body(selection.model))

    native_http = httpx2.AsyncClient

    class MockHTTP(native_http):
        def __init__(self, *args, **kwargs):
            http_options.append(kwargs)
            super().__init__(*args, **kwargs, transport=httpx2.MockTransport(respond))

    monkeypatch.setattr(httpx2, "AsyncClient", MockHTTP)

    async def run():
        clients = provider_clients(config)
        try:
            assert model_factories(config).role is None
            model = await clients.model(selection)
            result = await Agent(model, retries=0).run("Say ready.", model_settings=model_settings(selection))
            assert result.output == "Worker configured."
            assert await clients.model(selection) is model
        finally:
            await clients.close()

    asyncio.run(run())
    assert len(http_options) == 1
    assert http_options[0]["trust_env"] is False and http_options[0]["follow_redirects"] is False
    url, headers, body = wire[-1]
    assert body["model"] == selection.model and body["reasoning"]["effort"] == "high"
    assert (
        url.host == {"subscription": "chatgpt.com", "openai": "api.openai.com", "openrouter": "openrouter.ai"}[provider]
    )
    if provider == "subscription":
        assert broker_paths == [(config.broker_socket, config.broker_token_file)]
        assert fetches == [False, True] and len(wire) == 2
        assert headers["authorization"] == "Bearer synthetic-renewed"
        assert headers["chatgpt-account-id"] == "synthetic-account"
        assert wire[0][2] == body and body["stream"] is True and body["store"] is False
    else:
        assert not broker_paths and not fetches and len(wire) == 1
        assert headers["authorization"] == "Bearer synthetic-" + provider + "-key"
        assert "chatgpt-account-id" not in headers
    if provider == "openrouter":
        assert "service_tier" not in body and url.path == "/api/v1/responses"
        assert body["provider"] == {"require_parameters": True}
    else:
        assert body["service_tier"] == "priority"


@pytest.mark.parametrize(
    ("selection", "error"),
    [
        (ModelSelection(model="gpt-6.1-sol"), "WORKFLOW_BROKER_REQUIRED"),
        (ModelSelection(model="gpt-6.1-sol", authentication="api_key"), "WORKFLOW_API_KEY_REQUIRED"),
        (ModelSelection(provider="openrouter", model="vendor/test"), "OPENROUTER_API_KEY_REQUIRED"),
        (
            ModelSelection(provider="openrouter", authentication="api_key", model="vendor/test", fast=True),
            "WORKFLOW_FAST_UNSUPPORTED",
        ),
    ],
)
def test_another_providers_credential_never_authorizes_the_selected_model(tmp_path, monkeypatch, selection, error):
    config = WorkerProviderConfig(openrouter_key_file=tmp_path / "must-not-read.key")

    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid selected authentication must fail before HTTP construction")

    monkeypatch.setattr(httpx2, "AsyncClient", forbidden)

    async def run():
        clients = provider_clients(config)
        try:
            with pytest.raises(ValueError, match=error):
                await clients.model(selection)
        finally:
            await clients.close()

    asyncio.run(run())
