"""Native OAuth bootstrap and broker renewal use synthetic HTTP responses only."""

import asyncio
import base64
import json
import socket
import stat
import threading
import time
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from pydantic_ai.providers.openai_codex import OpenAICodexCredentials, OpenAICodexOAuthFlow

from civ_agent import subscription_auth as auth


def jwt(payload):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return encode({"alg": "RS256", "typ": "JWT"}) + "." + encode(payload) + ".c2lnbmF0dXJl"


def credentials(access="access", refresh="private-refresh", account="account"):
    return OpenAICodexCredentials(access_token=access, refresh_token=refresh, account_id=account)


def renewed(access="renewed-access", refresh="rotated-refresh", **extra):
    return {"access_token": access, "refresh_token": refresh, "expires_in": 3600, **extra}


def test_public_credential_source_is_private_atomic_and_reopens_without_cli(tmp_path, monkeypatch):
    async def run():
        async def forbidden(*args, **kwargs):
            pytest.fail("Native auth must never launch an executable")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
        source = auth.CredentialFile(tmp_path)
        original = credentials(jwt({"exp": time.time() + 3600}))
        await source.save(original)
        assert await auth.CredentialFile(tmp_path).load() == original
        path = tmp_path / auth.CREDENTIAL_FILE
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
        assert set(json.loads(path.read_text())) == {"version", "credentials", "expiresAt"}
        assert "private-refresh" not in repr(original) and original.access_token not in repr(original)
        before = path.stat().st_mtime_ns
        await source.save(original)
        assert path.stat().st_mtime_ns == before
        assert not list(tmp_path.glob(".write-*"))

    asyncio.run(run())


def test_force_refresh_uses_public_oauth_client_and_persists_rotated_credentials_before_return(tmp_path):
    async def run():
        source = auth.CredentialFile(tmp_path)
        await source.save(credentials())
        calls = []

        def token(request):
            calls.append(request)
            assert str(request.url) == "https://auth.openai.com/oauth/token" and request.method == "POST"
            form = parse_qs(request.content.decode())
            expected_id = parse_qs(urlsplit(OpenAICodexOAuthFlow().authorization_url()).query)["client_id"]
            assert form == {
                "grant_type": ["refresh_token"],
                "refresh_token": ["private-refresh"],
                "client_id": expected_id,
            }
            assert "authorization" not in request.headers
            return httpx2.Response(200, json=renewed())

        account = auth.NativeAccount(tmp_path, transport=httpx2.MockTransport(token))
        await account.start()
        assert await account.tokens() == {"accessToken": "access", "chatgptAccountId": "account"}
        assert not calls
        result = await account.tokens(force_refresh=True)
        assert result == {"accessToken": "renewed-access", "chatgptAccountId": "account"}
        assert (await source.load()).refresh_token == "rotated-refresh"
        assert "refresh" not in json.dumps(result)
        await account.close()
        reopened = auth.NativeAccount(tmp_path, transport=httpx2.MockTransport(token))
        assert await reopened.tokens() == result and len(calls) == 1
        await reopened.close()

    asyncio.run(run())


def test_expired_native_token_refreshes_once_for_concurrent_access_reads(tmp_path):
    async def run():
        await auth.CredentialFile(tmp_path).save(credentials(jwt({"exp": 0, "other": "ignored hint"})))
        calls = []

        async def token(request):
            calls.append(request)
            await asyncio.sleep(0.02)
            return httpx2.Response(200, json=renewed(jwt({"exp": time.time() + 3600})))

        account = auth.NativeAccount(tmp_path, transport=httpx2.MockTransport(token))
        results = await asyncio.gather(*(account.tokens() for _ in range(12)))
        assert len(calls) == 1 and all(result == results[0] for result in results)
        assert set(results[0]) == {"accessToken", "chatgptAccountId"}
        await account.close()

    asyncio.run(run())


@pytest.mark.parametrize("exp", ["0", True, float("nan"), float("inf"), None])
def test_unverified_malformed_expiry_is_not_treated_as_account_authority(tmp_path, exp):
    async def run():
        original = credentials(jwt({"exp": exp, "account_id": "forged-hint-account"}))
        await auth.CredentialFile(tmp_path).save(original)

        def forbidden(request):
            pytest.fail("Malformed expiry hint must not cause an unrequested refresh")

        account = auth.NativeAccount(tmp_path, transport=httpx2.MockTransport(forbidden))
        assert await account.tokens() == {"accessToken": original.access_token, "chatgptAccountId": "account"}
        await account.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "status,body",
    [
        (400, {"error": "invalid_grant", "error_description": "private-refresh leaked by provider"}),
        (200, {"refresh_token": "private-refresh"}),
        (200, renewed(access="")),
        (200, renewed(access=42)),
        (200, renewed(account_id="other-account")),
        (302, {"error": "private-refresh redirect"}),
    ],
)
def test_refresh_errors_are_sanitized_and_do_not_publish_incomplete_or_foreign_credentials(tmp_path, status, body):
    async def run():
        source = auth.CredentialFile(tmp_path)
        await source.save(credentials())
        before = source.path.read_bytes()
        account = auth.NativeAccount(
            tmp_path, transport=httpx2.MockTransport(lambda request: httpx2.Response(status, json=body))
        )
        with pytest.raises(auth.NativeAuthError) as error:
            await account.tokens(force_refresh=True)
        assert str(error.value) == auth._ERROR
        assert "private-refresh" not in str(error.value) and source.path.read_bytes() == before
        await account.close()

    asyncio.run(run())


@pytest.mark.parametrize("problem", ["missing", "malformed", "mode", "symlink"])
def test_bad_credential_files_are_rejected_without_secrets(tmp_path, problem):
    async def run():
        source = auth.CredentialFile(tmp_path)
        if problem != "missing":
            await source.save(credentials())
        if problem == "malformed":
            source.path.write_text('{"credentials":"private-refresh"}')
        elif problem == "mode":
            source.path.chmod(0o644)
        elif problem == "symlink":
            real = source.path.with_suffix(".real")
            source.path.rename(real)
            source.path.symlink_to(real)
        with pytest.raises(auth.NativeAuthError) as error:
            await source.load()
        assert "private-refresh" not in str(error.value)

    asyncio.run(run())


def test_constructor_failure_is_sanitized_and_does_not_leave_partial_started_state(tmp_path, monkeypatch):
    async def run():
        await auth.CredentialFile(tmp_path).save(credentials())
        factory = auth.AsyncOAuth2Client

        def fail(*args, **kwargs):
            raise RuntimeError("private-refresh constructor failure")

        monkeypatch.setattr(auth, "AsyncOAuth2Client", fail)
        account = auth.NativeAccount(tmp_path)
        with pytest.raises(auth.NativeAuthError, match="^Subscription authentication unavailable"):
            await account.start()
        assert account.record is None and account.client is None
        monkeypatch.setattr(auth, "AsyncOAuth2Client", factory)
        assert (await account.tokens())["accessToken"] == "access"
        await account.close()

    asyncio.run(run())


def test_failed_rotation_save_survives_failed_close_and_retries_without_spending_another_grant(tmp_path, monkeypatch):
    async def run():
        source = auth.CredentialFile(tmp_path)
        await source.save(credentials())
        original = auth.write_private_json
        calls = []

        def token(request):
            calls.append(request)
            return httpx2.Response(200, json=renewed())

        def denied(*args, **kwargs):
            raise OSError("private-refresh save failed")

        account = auth.NativeAccount(tmp_path, transport=httpx2.MockTransport(token))
        monkeypatch.setattr(auth, "write_private_json", denied)
        with pytest.raises(auth.NativeAuthError):
            await account.tokens(force_refresh=True)
        assert (await source.load()).refresh_token == "private-refresh"
        with pytest.raises(auth.NativeAuthError):
            await account.close()
        monkeypatch.setattr(auth, "write_private_json", original)
        assert (await account.tokens())["accessToken"] == "renewed-access"
        assert len(calls) == 1 and (await source.load()).refresh_token == "rotated-refresh"
        await account.close()

    asyncio.run(run())


def test_repeated_cancellation_waits_for_accepted_rotation_save_before_next_caller_or_close(tmp_path, monkeypatch):
    async def run():
        source = auth.CredentialFile(tmp_path)
        await source.save(credentials())
        accepted, release = threading.Event(), threading.Event()
        original = auth.write_private_json
        grants = []

        def blocked(*args, **kwargs):
            accepted.set()
            assert release.wait(3)
            return original(*args, **kwargs)

        def token(request):
            grants.append(request)
            return httpx2.Response(200, json=renewed())

        account = auth.NativeAccount(tmp_path, transport=httpx2.MockTransport(token))
        monkeypatch.setattr(auth, "write_private_json", blocked)
        task = asyncio.create_task(account.tokens(force_refresh=True))
        try:
            assert await asyncio.to_thread(accepted.wait, 3)
            task.cancel()
            await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done()
            waiting = asyncio.create_task(account.close())
            await asyncio.sleep(0.01)
            assert not waiting.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            await waiting
            assert (await source.load()).refresh_token == "rotated-refresh"
            assert (await account.tokens())["accessToken"] == "renewed-access"
            assert len(grants) == 1
            await account.close()
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize(
    "id_claims", [{"https://api.openai.com/auth": {"chatgpt_account_id": "account"}}, {"account_id": "account"}]
)
def test_login_reuses_native_code_exchange_and_id_claims_without_cli(tmp_path, monkeypatch, id_claims):
    async def run():
        native_client = httpx2.AsyncClient
        calls = []

        def token(request):
            calls.append(request)
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["authorization_code"] and form["code"] == ["synthetic-code"]
            assert form["redirect_uri"] == ["http://localhost:1455/auth/callback"]
            assert form["code_verifier"]
            return httpx2.Response(200, json=renewed(access="native-access", id_token=jwt(id_claims)))

        class Client(native_client):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, transport=httpx2.MockTransport(token), **kwargs)

        class Flow(OpenAICodexOAuthFlow):
            async def exchange_code_from_callback(self):
                return await self.exchange_code("synthetic-code")

        monkeypatch.setattr(httpx2, "AsyncClient", Client)
        monkeypatch.setattr(auth, "OpenAICodexOAuthFlow", Flow)
        urls = []
        await auth.login(tmp_path, authorization_callback=urls.append)
        loaded = await auth.CredentialFile(tmp_path).load()
        assert loaded.account_id == "account" and loaded.access_token == "native-access"
        assert len(calls) == 1 and len(urls) == 1
        query = parse_qs(urlsplit(urls[0]).query)
        assert query["state"] and query["code_challenge_method"] == ["S256"]
        assert "native-access" not in urls[0] and "rotated-refresh" not in urls[0]

    asyncio.run(run())


def test_login_sdk_error_and_authorization_callback_failure_are_sanitized(tmp_path, monkeypatch):
    async def run():
        class Flow(OpenAICodexOAuthFlow):
            async def exchange_code_from_callback(self):
                raise RuntimeError("private-refresh provider error")

        monkeypatch.setattr(auth, "OpenAICodexOAuthFlow", Flow)
        for callback in (lambda url: None, lambda url: (_ for _ in ()).throw(RuntimeError("private-refresh callback"))):
            with pytest.raises(auth.NativeAuthError) as error:
                await auth.login(tmp_path, authorization_callback=callback)
            assert "private-refresh" not in str(error.value)
            assert not (tmp_path / auth.CREDENTIAL_FILE).exists()

    asyncio.run(run())


@pytest.mark.parametrize("id_token", [None, "private-invalid-token", jwt({"unrelated": "private-claim"})])
def test_native_login_missing_account_claims_fail_without_persisting_or_echoing_tokens(tmp_path, monkeypatch, id_token):
    async def run():
        native_client = httpx2.AsyncClient

        class Client(native_client):
            def __init__(self, *args, **kwargs):
                super().__init__(
                    *args,
                    transport=httpx2.MockTransport(
                        lambda request: httpx2.Response(200, json=renewed(id_token=id_token))
                    ),
                    **kwargs,
                )

        class Flow(OpenAICodexOAuthFlow):
            async def exchange_code_from_callback(self):
                return await self.exchange_code("private-code")

        monkeypatch.setattr(httpx2, "AsyncClient", Client)
        monkeypatch.setattr(auth, "OpenAICodexOAuthFlow", Flow)
        with pytest.raises(auth.NativeAuthError) as error:
            await auth.login(tmp_path, authorization_callback=lambda url: None)
        assert "private" not in str(error.value) and "rotated-refresh" not in str(error.value)
        assert not (tmp_path / auth.CREDENTIAL_FILE).exists()

    asyncio.run(run())


def test_native_callback_rejects_wrong_state_and_path_and_cleans_up_without_query_logs(tmp_path, monkeypatch, capsys):
    async def run():
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        codes = []

        class Flow(OpenAICodexOAuthFlow):
            def __init__(self):
                super().__init__(redirect_uri=f"http://127.0.0.1:{port}/auth/callback", state="expected-state")

            async def exchange_code(self, code):
                codes.append(code)
                return credentials()

        monkeypatch.setattr(auth, "OpenAICodexOAuthFlow", Flow)
        task = asyncio.create_task(auth.login(tmp_path, authorization_callback=lambda url: None))
        try:
            async with httpx2.AsyncClient(trust_env=False) as client:
                async with asyncio.timeout(3):
                    while True:
                        try:
                            await client.get(f"http://127.0.0.1:{port}/auth/callback?state=wrong&code=private-code")
                            break
                        except httpx2.ConnectError:
                            await asyncio.sleep(0.01)
                assert not codes and not task.done()
                await client.get(f"http://127.0.0.1:{port}/wrong?state=expected-state&code=private-code")
                assert not codes and not task.done()
                await client.get(f"http://127.0.0.1:{port}/auth/callback?state=expected-state&code=accepted-code")
            await task
            assert codes == ["accepted-code"]
            with socket.socket() as check:
                check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                check.bind(("127.0.0.1", port))
            assert "private-code" not in str(capsys.readouterr())
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["cancel", "deny"])
def test_native_callback_cleans_up_on_cancellation_or_denial_and_never_persists_credentials(
    tmp_path, monkeypatch, capsys, outcome
):
    async def run():
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]

        class Flow(OpenAICodexOAuthFlow):
            def __init__(self):
                super().__init__(redirect_uri=f"http://127.0.0.1:{port}/auth/callback", state="expected")

            async def exchange_code(self, code):
                pytest.fail("Denied or canceled authorization cannot exchange a grant")

        monkeypatch.setattr(auth, "OpenAICodexOAuthFlow", Flow)
        task = asyncio.create_task(auth.login(tmp_path, authorization_callback=lambda url: None))
        try:
            async with httpx2.AsyncClient(trust_env=False) as client:
                async with asyncio.timeout(3):
                    while True:
                        try:
                            await client.get(f"http://127.0.0.1:{port}/auth/callback?state=wrong")
                            break
                        except httpx2.ConnectError:
                            await asyncio.sleep(0.01)
                if outcome == "deny":
                    await client.get(
                        f"http://127.0.0.1:{port}/auth/callback?state=expected&error=private-refresh-denial"
                    )
                    with pytest.raises(auth.NativeAuthError) as error:
                        await task
                    assert "private-refresh" not in str(error.value)
                else:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                async with asyncio.timeout(3):
                    while True:
                        try:
                            await client.get(f"http://127.0.0.1:{port}/auth/callback?state=wrong")
                        except httpx2.ConnectError:
                            break
                        await asyncio.sleep(0.01)
            assert not (tmp_path / auth.CREDENTIAL_FILE).exists()
            assert "private-refresh" not in str(capsys.readouterr())
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
