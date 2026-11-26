"""Local token outages retain the native, definitely-unsent provider category."""

import asyncio

import httpx2
import pytest
from aiohttp import web
from openai import AsyncOpenAI
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelConnectionError
from pydantic_ai.models.openai_codex import OpenAICodexModel
from pydantic_ai.providers.openai_codex import OpenAICodexProvider

from civ_agent.container_auth import TokenBrokerUnavailable, TokenClient
from civ_agent.orchestration.providers import BrokerAuth
from civ_agent.runtime.model_policy import SanitizeProviderErrors
from civ_agent.tls import write_private_json


@pytest.mark.parametrize("renewal", [False, True])
def test_broker_disconnect_before_send_or_after_definite_401_is_native_unsent(tmp_path, renewal):
    async def run():
        token_file = tmp_path / "worker.json"
        write_private_json(token_file, {"token": "synthetic-local-token"})
        disconnected = TokenClient(tmp_path / "missing.sock", token_file)
        fetched, sent = [], []

        class Tokens:
            async def fetch(self, *, refresh=False, generation=None):
                fetched.append((refresh, generation))
                if renewal and not refresh:
                    return {"accessToken": "synthetic-initial", "chatgptAccountId": "synthetic-account"}
                return await disconnected.fetch(refresh=refresh, generation=generation)

        def respond(request):
            sent.append(request.url.path)
            return httpx2.Response(401, json={"error": {"message": "synthetic rejected token"}})

        async with httpx2.AsyncClient(
            auth=BrokerAuth(Tokens()), transport=httpx2.MockTransport(respond), trust_env=False
        ) as http:
            async with AsyncOpenAI(
                api_key="synthetic",
                base_url="https://chatgpt.com/backend-api/codex",
                http_client=http,
                max_retries=0,
            ) as sdk:
                sdk.platform_headers()
                native = OpenAICodexModel("synthetic-model", provider=OpenAICodexProvider(openai_client=sdk))
                model = Agent(native, capabilities=[SanitizeProviderErrors()], retries=0)
                with pytest.raises(ModelConnectionError) as caught:
                    await model.run("Synthetic only.")
        assert caught.value.phase == "connect" and not caught.value.in_stream
        assert len(sent) == int(renewal) and [item[0] for item in fetched] == ([False, True] if renewal else [False])

    asyncio.run(run())


@pytest.mark.parametrize("status,body", [(503, {"error": "synthetic unavailable"}), (200, {"bad": "schema"})])
def test_broker_authentication_or_invalid_reply_is_not_a_transport_outage(tmp_path, status, body):
    async def run():
        token_file = tmp_path / "worker.json"
        write_private_json(token_file, {"token": "synthetic-local-token"})

        async def reply(request):
            return web.json_response(body, status=status)

        app = web.Application()
        app.router.add_post("/tokens", reply)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            socket = tmp_path / "broker.sock"
            await web.UnixSite(runner, str(socket)).start()
            with pytest.raises((RuntimeError, KeyError)) as caught:
                await TokenClient(socket, token_file).fetch()
            assert not isinstance(caught.value, TokenBrokerUnavailable)
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_broker_configuration_failure_does_not_gain_a_safe_connect_classification():
    async def run():
        class BrokenConfiguration:
            async def fetch(self, *, refresh=False, generation=None):
                raise RuntimeError("SYNTHETIC_AUTH_CONFIGURATION")

        sent = []
        async with httpx2.AsyncClient(
            auth=BrokerAuth(BrokenConfiguration()),
            transport=httpx2.MockTransport(lambda request: sent.append(request) or httpx2.Response(200)),
            trust_env=False,
        ) as http:
            with pytest.raises(RuntimeError, match="SYNTHETIC_AUTH_CONFIGURATION"):
                await http.post("https://chatgpt.com/backend-api/codex/responses")
        assert sent == []

    asyncio.run(run())


def test_local_token_timeout_is_definitely_before_any_inference_send(tmp_path):
    async def run():
        token_file = tmp_path / "worker.json"
        write_private_json(token_file, {"token": "synthetic-local-token"})
        release, entered = asyncio.Event(), asyncio.Event()

        async def reply(request):
            entered.set()
            await release.wait()
            return web.json_response({})

        app = web.Application()
        app.router.add_post("/tokens", reply)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        sent = []
        try:
            socket = tmp_path / "broker.sock"
            await web.UnixSite(runner, str(socket)).start()
            async with httpx2.AsyncClient(
                auth=BrokerAuth(TokenClient(socket, token_file)),
                transport=httpx2.MockTransport(lambda request: sent.append(request) or httpx2.Response(200)),
                trust_env=False,
            ) as http:
                with pytest.raises(httpx2.ConnectError, match="AUTH_BROKER_UNAVAILABLE"):
                    await asyncio.wait_for(http.post("https://chatgpt.com/backend-api/codex/responses"), timeout=12)
            assert entered.is_set() and sent == []
        finally:
            release.set()
            await runner.cleanup()

    asyncio.run(run())
