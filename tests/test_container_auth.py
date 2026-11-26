"""Token renewal is centralized; workers never receive renewable credentials."""

import asyncio
import base64
import hashlib
import json
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

import aiohttp
import httpx2
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from civ_agent import container_auth
from civ_agent.container_auth import UNAVAILABLE, LoginBroker, TokenClient
from civ_agent.container_supervisor import authorize_agent
from civ_agent.tls import private_directory, write_private_json


class FakeAccount:
    def __init__(self, state):
        self.state, self.calls = state, []
        self.started, self.closed = 0, 0
        self.fail: Exception | None = None
        self.force_fail: Exception | None = None

    async def start(self):
        self.started += 1

    async def close(self):
        self.closed += 1

    async def tokens(self, *, force_refresh=False):
        self.calls.append(force_refresh)
        if self.fail is not None:
            raise self.fail
        if force_refresh and self.force_fail is not None:
            raise self.force_fail
        if force_refresh or self.state.pop("expired", False):
            await asyncio.sleep(0.01)
            self.state["renewals"] = self.state.get("renewals", 0) + 1
            self.state["accessToken"] = "new-access-" + str(self.state["renewals"])
        return {
            "accessToken": self.state["accessToken"],
            "chatgptAccountId": "account",
            # Even an unexpectedly rich account response cannot cross the wire.
            "refresh_token": "never-return-refresh",
            "id_token": "never-return-identity",
        }


@pytest.mark.parametrize("problem", ["mode", "symlink"])
def test_worker_token_file_must_be_private_before_broker_io(tmp_path, monkeypatch, problem):
    path = tmp_path / "worker.json"
    write_private_json(path, {"token": "private-worker-credential"})
    if problem == "mode":
        path.chmod(0o644)
    else:
        link = tmp_path / "link.json"
        link.symlink_to(path)
        path = link
    session = Mock(side_effect=AssertionError("Invalid worker credentials must not be sent"))
    monkeypatch.setattr(container_auth.aiohttp, "ClientSession", session)
    with pytest.raises(ValueError, match="regular file|owner-only"):
        asyncio.run(TokenClient(tmp_path / "broker.sock", path).fetch())
    session.assert_not_called()


def authorize(root, token="worker-secret"):
    write_private_json(root / "clients.json", {"one": hashlib.sha256(token.encode()).hexdigest()})
    return {"Authorization": "Bearer " + token}


def test_token_renewal_concurrency_restart_and_privacy(tmp_path):
    async def exercise():
        state = {"accessToken": "old-access"}
        headers = authorize(tmp_path)
        accounts = []

        async def check(broker, *, restart=False):
            async with TestClient(TestServer(broker.application())) as client:
                bad = await client.post("/tokens", json={})
                assert bad.status == 401
                initial = await (await client.post("/tokens", headers=headers, json={})).json()
                assert set(initial) == {"accessToken", "chatgptAccountId", "generation"}
                generation = initial["generation"]
                if restart:
                    assert generation == hashlib.sha256(b"new-access-1").hexdigest()
                    stale = await (
                        await client.post(
                            "/tokens",
                            headers=headers,
                            json={"refresh": True, "generation": hashlib.sha256(b"old-access").hexdigest()},
                        )
                    ).json()
                    assert stale == initial
                    return
                responses = await asyncio.gather(
                    *[
                        client.post("/tokens", headers=headers, json={"refresh": True, "generation": generation})
                        for _ in range(8)
                    ]
                )
                results = [await response.json() for response in responses]
                assert all(result["accessToken"] == "new-access-1" for result in results)
                assert all(set(result) == {"accessToken", "chatgptAccountId", "generation"} for result in results)
                assert all("never-return" not in json.dumps(result) for result in results)

        for restart in (False, True):
            account = FakeAccount(state)
            accounts.append(account)
            await check(LoginBroker(tmp_path, account=account), restart=restart)
        assert sum(sum(account.calls) for account in accounts) == state["renewals"] == 1
        assert all(account.started == account.closed == 1 for account in accounts)

    asyncio.run(exercise())


def test_refresh_cooldown_applies_to_new_generation_and_failed_attempts(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(container_auth, "time", SimpleNamespace(monotonic=lambda: clock.now))

    async def exercise():
        account = FakeAccount({"accessToken": "initial"})
        headers = authorize(tmp_path)
        async with TestClient(TestServer(LoginBroker(tmp_path, account=account).application())) as client:
            generation = hashlib.sha256(b"initial").hexdigest()
            first = await (
                await client.post("/tokens", headers=headers, json={"refresh": True, "generation": generation})
            ).json()
            assert sum(account.calls) == 1
            current = {"refresh": True, "generation": first["generation"]}
            assert await (await client.post("/tokens", headers=headers, json=current)).json() == first
            assert sum(account.calls) == 1
            clock.now += 5
            renewed = await (await client.post("/tokens", headers=headers, json=current)).json()
            assert renewed["accessToken"] == "new-access-2" and sum(account.calls) == 2
            clock.now += 5
            account.force_fail = RuntimeError("PRIVATE_REFRESH_FAILURE")
            current = {"refresh": True, "generation": renewed["generation"]}
            failed = await client.post("/tokens", headers=headers, json=current)
            assert failed.status == 503 and await failed.text() == UNAVAILABLE
            assert await (await client.post("/tokens", headers=headers, json=current)).json() == renewed
            assert sum(account.calls) == 3

    asyncio.run(exercise())


def test_proactive_expiry_renewal_makes_old_generation_refresh_stale(tmp_path):
    async def exercise():
        account = FakeAccount({"accessToken": "initial"})
        headers = authorize(tmp_path)
        async with TestClient(TestServer(LoginBroker(tmp_path, account=account).application())) as client:
            account.state["expired"] = True
            old_generation = hashlib.sha256(b"initial").hexdigest()
            result = await (
                await client.post("/tokens", headers=headers, json={"refresh": True, "generation": old_generation})
            ).json()
            assert result["accessToken"] == "new-access-1"
            assert result["generation"] == hashlib.sha256(b"new-access-1").hexdigest()
            assert account.state["renewals"] == 1 and not any(account.calls)

    asyncio.run(exercise())


def test_client_hash_rotation_and_invalid_requests_precede_account_access(tmp_path):
    async def exercise():
        account = FakeAccount({"accessToken": "initial"})
        original = authorize(tmp_path)
        async with TestClient(TestServer(LoginBroker(tmp_path, account=account).application())) as client:
            calls = len(account.calls)
            assert (await client.post("/tokens", json={})).status == 401
            for body in ([], {"refresh": "true"}, {"generation": 1}, {"refresh_token": "PRIVATE_INPUT"}):
                response = await client.post("/tokens", headers=original, json=body)
                assert response.status == 400 and "PRIVATE_INPUT" not in await response.text()
            assert len(account.calls) == calls
            replacement = authorize(tmp_path, "replacement-worker")
            assert (await client.post("/tokens", headers=original, json={})).status == 401
            assert (await client.post("/tokens", headers=replacement, json={})).status == 200
            assert len(account.calls) == calls + 1

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", [OSError, ValueError, RuntimeError, aiohttp.ClientError])
def test_account_errors_are_sanitized_without_credential_logs(tmp_path, caplog, failure):
    async def exercise():
        account = FakeAccount({"accessToken": "initial"})
        headers = authorize(tmp_path)
        async with TestClient(TestServer(LoginBroker(tmp_path, account=account).application())) as client:
            account.fail = failure("PRIVATE_REFRESH_TOKEN PRIVATE_ID_TOKEN PRIVATE_PROVIDER_RESPONSE")
            response = await client.post("/tokens", headers=headers, json={})
            assert response.status == 503 and await response.text() == UNAVAILABLE
        assert "PRIVATE_" not in caplog.text and account.closed == 1

    asyncio.run(exercise())


def test_startup_failure_is_sanitized_and_account_is_closed(tmp_path):
    async def exercise():
        account = FakeAccount({"accessToken": "initial"})
        account.fail = RuntimeError("PRIVATE_REFRESH_TOKEN")
        client = TestClient(TestServer(LoginBroker(tmp_path, account=account).application()))
        try:
            with pytest.raises(RuntimeError, match=UNAVAILABLE) as error:
                await client.start_server()
            assert str(error.value) == UNAVAILABLE
            assert account.started == account.closed == 1
        finally:
            await client.close()

    asyncio.run(exercise())


def test_broker_loads_native_account_only_at_lifecycle_and_never_spawns(tmp_path, monkeypatch):
    account = FakeAccount({"accessToken": "native-access"})
    roots = []

    def create(root):
        roots.append(root)
        return account

    def forbidden(*args, **kwargs):
        raise AssertionError("The native login broker must not launch subprocesses")

    monkeypatch.setitem(sys.modules, "civ_agent.subscription_auth", SimpleNamespace(NativeAccount=create))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    broker = LoginBroker(tmp_path)
    assert not roots

    async def exercise():
        headers = authorize(tmp_path)
        async with TestClient(TestServer(broker.application())) as client:
            result = await (await client.post("/tokens", headers=headers, json={})).json()
            assert result["accessToken"] == "native-access"
        assert roots == [tmp_path] and account.started == account.closed == 1

    asyncio.run(exercise())


def test_token_client_keeps_access_only_wire_and_tracks_generation(tmp_path):
    async def exercise():
        account = FakeAccount({"accessToken": "initial"})
        authorize(tmp_path)
        token_file = tmp_path / "worker.json"
        write_private_json(token_file, {"token": "worker-secret"})
        runner = web.AppRunner(LoginBroker(tmp_path, account=account).application(), access_log=None)
        await runner.setup()
        socket = tmp_path / "broker.sock"
        try:
            await web.UnixSite(runner, str(socket)).start()
            client = TokenClient(socket, token_file)
            assert await client.fetch() == {"accessToken": "initial", "chatgptAccountId": "account"}
            assert client.generation == hashlib.sha256(b"initial").hexdigest()
            assert await client.fetch(refresh=True) == {"accessToken": "new-access-1", "chatgptAccountId": "account"}
            assert client.generation == hashlib.sha256(b"new-access-1").hexdigest()
            write_private_json(token_file, {"token": "wrong-worker"})
            with pytest.raises(RuntimeError, match="Benchmark login unavailable"):
                await client.fetch()
        finally:
            await runner.cleanup()

    asyncio.run(exercise())


def test_importing_token_client_does_not_load_native_sdk_or_oauth_stack():
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from civ_agent.container_auth import TokenClient; "
            "assert not any(name in sys.modules for name in ('civ_agent.subscription_auth', 'pydantic_ai', 'authlib'))",
        ],
        check=True,
        timeout=10,
        capture_output=True,
    )


def test_native_account_expiry_refresh_and_broker_restart_keep_renewable_credentials_private(tmp_path):
    from pydantic_ai.providers.openai_codex import OpenAICodexCredentials

    from civ_agent.subscription_auth import CredentialFile, NativeAccount

    def jwt(marker, expires):
        def part(value):
            return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

        return part({"alg": "RS256", "typ": "JWT"}) + "." + part({"exp": expires, "marker": marker}) + ".c2lnbmF0dXJl"

    async def exercise():
        issued = [jwt("old", int(time.time()) - 60)]
        refreshed = []
        source = CredentialFile(tmp_path)
        await source.save(
            OpenAICodexCredentials(access_token=issued[0], refresh_token="private-refresh-0", account_id="account")
        )
        headers = authorize(tmp_path)

        async def refresh(request):
            assert str(request.url) == "https://auth.openai.com/oauth/token"
            assert request.method == "POST"
            body = request.content.decode()
            assert "grant_type=refresh_token" in body
            assert "private-refresh-" + str(len(refreshed)) in body
            refreshed.append(body)
            issued.append(jwt("fresh-" + str(len(refreshed)), int(time.time()) + 3600))
            return httpx2.Response(
                200,
                json={
                    "access_token": issued[-1],
                    "refresh_token": "private-refresh-" + str(len(refreshed)),
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )

        for restart in (False, True):
            account = NativeAccount(tmp_path, transport=httpx2.MockTransport(refresh))
            async with TestClient(TestServer(LoginBroker(tmp_path, account=account).application())) as client:
                initial = await (await client.post("/tokens", headers=headers, json={})).json()
                assert set(initial) == {"accessToken", "chatgptAccountId", "generation"}
                assert initial["accessToken"] == issued[-1] and initial["chatgptAccountId"] == "account"
                assert len(refreshed) == (2 if restart else 1)
                stale = {"refresh": True, "generation": hashlib.sha256(issued[0].encode()).hexdigest()}
                responses = await asyncio.gather(
                    *(client.post("/tokens", headers=headers, json=stale) for _ in range(8))
                )
                assert [await response.json() for response in responses] == [initial] * 8
                if not restart:
                    current = {"refresh": True, "generation": initial["generation"]}
                    responses = await asyncio.gather(
                        *(client.post("/tokens", headers=headers, json=current) for _ in range(8))
                    )
                    bundles = [await response.json() for response in responses]
                    assert all(bundle["accessToken"] == issued[-1] for bundle in bundles)
                    assert all("private-refresh" not in json.dumps(bundle) for bundle in bundles)
                assert len(refreshed) == 2
            assert account.client is None
        persisted = await source.load()
        assert persisted.access_token == issued[-1] and persisted.refresh_token == "private-refresh-2"
        assert source.path.stat().st_mode & 0o777 == 0o600
        assert not (tmp_path / "codex").exists()

    asyncio.run(exercise())


def test_parallel_game_authorization_retains_all_clients(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    auth = private_directory(tmp_path / "auth")
    directories = [private_directory(tmp_path / f"seat{i}") for i in range(12)]
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda row: authorize_agent(auth, "game", *row), enumerate(directories)))
    assert len(json.loads((auth / "clients.json").read_text())) == 12
