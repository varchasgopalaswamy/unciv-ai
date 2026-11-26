"""Synthetic native auth in the Python-only image; no account or network calls."""

import asyncio
import base64
import hashlib
import json
import platform
import shutil
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs

import authlib
import httpx2
from aiohttp import web
from pydantic_ai.providers.openai_codex import OpenAICodexCredentials

from civ_agent.container_auth import LoginBroker, TokenClient
from civ_agent.subscription_auth import CredentialFile, NativeAccount
from civ_agent.tls import write_private_json


def access_token(sequence, expiry):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return encode({"alg": "HS256"}) + "." + encode({"exp": expiry, "sequence": sequence}) + ".c2lnbmF0dXJl"


async def run():
    assert shutil.which("codex") is None
    assert shutil.which("codex-code-mode-host") is None
    grants = []

    async def renew(request):
        assert str(request.url) == "https://auth.openai.com/oauth/token"
        fields = parse_qs(request.content.decode())
        assert fields["grant_type"] == ["refresh_token"]
        assert fields["client_id"] == ["app_EMoamEEZ73f0CkXaXp7hrann"]
        assert fields["refresh_token"] == ["refresh-" + str(len(grants))]
        assert "Authorization" not in request.headers
        grants.append(request.url.path)
        return httpx2.Response(
            200,
            json={
                "access_token": access_token(len(grants), int(time.time()) + 3600),
                "refresh_token": "refresh-" + str(len(grants)),
                "token_type": "Bearer",
                "expires_in": 3600,
            },
        )

    with TemporaryDirectory(prefix="native-auth-smoke-") as directory:
        root = Path(directory)
        await CredentialFile(root).save(
            OpenAICodexCredentials(access_token=access_token(0, 1), refresh_token="refresh-0", account_id="synthetic")
        )
        worker_secret = "synthetic-worker-secret"
        write_private_json(root / "clients.json", {"worker": hashlib.sha256(worker_secret.encode()).hexdigest()})
        write_private_json(root / "worker.json", {"token": worker_secret})

        async def serve(account):
            runner = web.AppRunner(LoginBroker(root, account=account).application(), access_log=None)
            await runner.setup()
            await web.UnixSite(runner, str(root / "broker.sock")).start()
            return runner

        runner = await serve(NativeAccount(root, transport=httpx2.MockTransport(renew)))
        client = TokenClient(root / "broker.sock", root / "worker.json")
        try:
            initial = await client.fetch()
            assert len(grants) == 1
            assert set(initial) == {"accessToken", "chatgptAccountId"}
            generation = hashlib.sha256(initial["accessToken"].encode()).hexdigest()
            responses = await asyncio.gather(*[client.fetch(refresh=True, generation=generation) for _ in range(8)])
            assert len(grants) == 2 and all(response == responses[0] for response in responses)
            assert all(set(response) == {"accessToken", "chatgptAccountId"} for response in responses)
            current = responses[0]
        finally:
            await runner.cleanup()
        (root / "broker.sock").unlink(missing_ok=True)
        runner = await serve(NativeAccount(root, transport=httpx2.MockTransport(renew)))
        try:
            assert await client.fetch() == current and len(grants) == 2
        finally:
            await runner.cleanup()
    print(
        json.dumps(
            {
                "nativeAuth": "passed",
                "python": platform.python_version(),
                "syntheticRefreshes": len(grants),
                "codexExecutables": 0,
                "authlib": authlib.__version__,
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(run())
