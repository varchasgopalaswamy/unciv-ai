import asyncio
import hashlib

import httpx2
import pytest
from aiohttp import web

from civ_agent.container_auth import LoginBroker, TokenClient
from civ_agent.orchestration.contracts import ModelSelection
from civ_agent.orchestration.providers import BrokerAuth, ProviderClients, model_settings
from civ_agent.tls import write_private_json
from tests.test_container_auth import FakeAccount, authorize


class Broker:
    def __init__(self):
        self.refreshes = []

    async def fetch(self, *, refresh=False, generation=None):
        if refresh:
            assert generation == hashlib.sha256(b"initial").hexdigest()
        self.refreshes.append(refresh)
        return {"accessToken": "renewed" if refresh else "initial", "chatgptAccountId": "account"}


def test_subscription_auth_renews_once_after_definite_401_and_never_follows_redirects():
    async def run():
        broker = Broker()
        seen = []

        async def respond(request):
            seen.append(request.headers["Authorization"])
            assert request.headers["chatgpt-account-id"] == "account"
            return httpx2.Response(401)

        async with httpx2.AsyncClient(
            auth=BrokerAuth(broker), transport=httpx2.MockTransport(respond), follow_redirects=False
        ) as client:
            result = await client.post("https://chatgpt.com/backend-api/codex/responses", json={})
            assert result.status_code == 401
        assert seen == ["Bearer initial", "Bearer renewed"]
        assert broker.refreshes == [False, True]

    asyncio.run(run())


def test_delayed_401_refresh_uses_its_requests_generation_after_another_request_renews(tmp_path):
    async def run():
        account = FakeAccount({"accessToken": "initial"})
        authorize(tmp_path)
        token_file = tmp_path / "worker.json"
        write_private_json(token_file, {"token": "worker-secret"})
        runner = web.AppRunner(LoginBroker(tmp_path, account=account).application(), access_log=None)
        await runner.setup()
        socket = tmp_path / "broker.sock"
        sent, release = asyncio.Event(), asyncio.Event()
        observed = []
        try:
            await web.UnixSite(runner, str(socket)).start()
            broker = TokenClient(socket, token_file)

            async def respond(request):
                authorization = request.headers["Authorization"]
                observed.append((request.url.path, authorization))
                if request.url.path.endswith("/delayed") and authorization == "Bearer initial":
                    sent.set()
                    await release.wait()
                    return httpx2.Response(401)
                return httpx2.Response(200)

            async with httpx2.AsyncClient(
                auth=BrokerAuth(broker), transport=httpx2.MockTransport(respond), follow_redirects=False
            ) as client:
                delayed = asyncio.create_task(client.post("https://chatgpt.com/backend-api/codex/delayed"))
                try:
                    await asyncio.wait_for(sent.wait(), timeout=5)
                    account.state["expired"] = True
                    assert (await client.post("https://chatgpt.com/backend-api/codex/current")).status_code == 200
                    assert broker.generation == hashlib.sha256(b"new-access-1").hexdigest()
                finally:
                    release.set()
                assert (await asyncio.wait_for(delayed, timeout=5)).status_code == 200
            assert observed == [
                ("/backend-api/codex/delayed", "Bearer initial"),
                ("/backend-api/codex/current", "Bearer new-access-1"),
                ("/backend-api/codex/delayed", "Bearer new-access-1"),
            ]
            assert account.state["renewals"] == 1 and not any(account.calls)
        finally:
            release.set()
            await runner.cleanup()

    asyncio.run(run())


def test_subscription_auth_does_not_follow_redirect_after_its_one_401_retry():
    async def run():
        broker, seen = Broker(), []

        async def respond(request):
            seen.append((str(request.url), request.headers["Authorization"]))
            return (
                httpx2.Response(401)
                if len(seen) == 1
                else httpx2.Response(302, headers={"location": "https://other.test/responses"})
            )

        async with httpx2.AsyncClient(
            auth=BrokerAuth(broker), transport=httpx2.MockTransport(respond), follow_redirects=False
        ) as client:
            response = await client.post("https://chatgpt.com/backend-api/codex/responses", json={})
            assert response.status_code == 302
        assert seen == [
            ("https://chatgpt.com/backend-api/codex/responses", "Bearer initial"),
            ("https://chatgpt.com/backend-api/codex/responses", "Bearer renewed"),
        ]
        assert broker.refreshes == [False, True]

    asyncio.run(run())


@pytest.mark.parametrize(
    "url",
    ["http://chatgpt.com/backend-api/codex/responses", "https://evil.test/responses", "https://chatgpt.com/other"],
)
def test_subscription_credentials_cannot_be_sent_to_another_destination(url):
    async def run():
        broker = Broker()
        async with httpx2.AsyncClient(
            auth=BrokerAuth(broker), transport=httpx2.MockTransport(lambda request: httpx2.Response(200))
        ) as client:
            with pytest.raises(ValueError, match="AUTH_DESTINATION"):
                await client.post(url)
        assert broker.refreshes == []

    asyncio.run(run())


def test_provider_auth_is_explicit_and_does_not_fall_back_to_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "not-allowed")

    async def run():
        clients = ProviderClients()
        try:
            with pytest.raises(ValueError, match="BROKER_REQUIRED"):
                await clients.model(ModelSelection(model="test"))
            with pytest.raises(ValueError, match="API_KEY_REQUIRED"):
                await clients.model(ModelSelection(model="test", authentication="api_key"))
            with pytest.raises(ValueError, match="OPENROUTER_API_KEY_REQUIRED"):
                await clients.model(ModelSelection(provider="openrouter", model="vendor/test"))
        finally:
            await clients.close()

    asyncio.run(run())


def test_reasoning_and_tier_settings_are_preserved_without_nested_retries():
    assert model_settings(ModelSelection(model="test", reasoning_effort="high", fast=True)) == {
        "thinking": "high",
        "max_retries": 0,
        "extra_body": {"service_tier": "priority"},
    }
    assert model_settings(ModelSelection(model="test", reasoning_effort="none"))["thinking"] is False


def test_an_openai_key_is_never_used_for_openrouter(tmp_path):
    async def run():
        # The wrong provider's file must not even be read.
        clients = ProviderClients(openai_key_file=tmp_path / "must-not-read")
        try:
            with pytest.raises(ValueError, match="WORKFLOW_API_KEY_REQUIRED"):
                await clients.model(
                    ModelSelection(provider="openrouter", authentication="api_key", model="vendor/test")
                )
        finally:
            await clients.close()

    asyncio.run(run())
