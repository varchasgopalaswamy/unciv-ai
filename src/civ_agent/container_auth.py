"""One renewable benchmark login; workers receive only short-lived access tokens."""

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Protocol

import aiohttp
from aiohttp import web

from civ_agent.protocol import JsonObject
from civ_agent.tls import private_directory, regular_file, write_private_json

UNAVAILABLE = "Benchmark login unavailable; inspect civ-agent auth status"
AUTH_ERRORS = (OSError, ValueError, RuntimeError, TimeoutError, aiohttp.ClientError)


class Account(Protocol):
    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def tokens(self, *, force_refresh: bool = False) -> JsonObject: ...


class LoginBroker:
    def __init__(self, root, account: Account | None = None):
        self.root = Path(root)
        self.account = account
        self.lock = asyncio.Lock()
        self.last_refresh = 0.0

    async def tokens(self, *, force_refresh=False):
        if self.account is None:
            raise RuntimeError(UNAVAILABLE)
        tokens = await self.account.tokens(force_refresh=force_refresh)
        access, account = tokens.get("accessToken"), tokens.get("chatgptAccountId")
        if not isinstance(access, str) or not isinstance(account, str) or not access or not account:
            raise ValueError("Run civ-agent auth login to establish the benchmark login")
        return {
            "accessToken": access,
            "chatgptAccountId": account,
            "generation": hashlib.sha256(access.encode()).hexdigest(),
        }

    async def fetch(self, request):
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
        clients_path = self.root / "clients.json"
        try:
            clients = json.loads(clients_path.read_text()) if clients_path.exists() else {}
            if not isinstance(clients, dict) or any(not isinstance(value, str) for value in clients.values()):
                raise ValueError("Invalid broker clients")
        except AUTH_ERRORS:
            raise web.HTTPServiceUnavailable(text=UNAVAILABLE) from None
        digest = hashlib.sha256(supplied.encode()).hexdigest()
        if not any(hmac.compare_digest(digest, expected) for expected in clients.values()):
            raise web.HTTPUnauthorized()
        try:
            data = await request.json()
            if (
                not isinstance(data, dict)
                or set(data) - {"generation", "refresh"}
                or ("refresh" in data and type(data["refresh"]) is not bool)
                or data.get("generation") is not None
                and not isinstance(data["generation"], str)
            ):
                raise ValueError("Invalid broker request")
        except ValueError:
            raise web.HTTPBadRequest() from None
        try:
            async with self.lock:
                # Concurrent 401s for one token bundle cause a single renewal.
                result = await self.tokens()
                refresh = data.get("refresh") is True and data.get("generation") == result["generation"]
                refresh = refresh and time.monotonic() - self.last_refresh >= 5
                if refresh:
                    self.last_refresh = time.monotonic()
                    result = await self.tokens(force_refresh=True)
                return web.json_response(result)
        except AUTH_ERRORS:
            raise web.HTTPServiceUnavailable(text=UNAVAILABLE) from None

    async def lifecycle(self, app):
        if self.account is None:
            # TokenClient is imported by ordinary startup paths; load the native
            # authentication stack only inside the separate broker service.
            from civ_agent.subscription_auth import NativeAccount

            self.account = NativeAccount(self.root)
        try:
            await self.account.start()
            await self.tokens()
            yield
        except AUTH_ERRORS:
            raise RuntimeError(UNAVAILABLE) from None
        finally:
            try:
                await self.account.close()
            except AUTH_ERRORS:
                raise RuntimeError(UNAVAILABLE) from None

    def application(self):
        app = web.Application(client_max_size=4096)
        app.router.add_post("/tokens", self.fetch)
        app.cleanup_ctx.append(self.lifecycle)
        return app


class TokenBrokerUnavailable(ConnectionError):
    """The local token transport failed before an inference request was sent."""


class TokenClient:
    def __init__(self, socket_path, token_file):
        self.socket_path, self.token_file = str(socket_path), Path(token_file)
        self.generation = None

    async def fetch(self, *, refresh=False, generation=None):
        regular_file(self.token_file, private=True)
        token = json.loads(self.token_file.read_text())["token"]
        try:
            async with aiohttp.ClientSession(
                connector=aiohttp.UnixConnector(path=self.socket_path),
                timeout=aiohttp.ClientTimeout(total=9),
            ) as session:
                async with session.post(
                    "http://localhost/tokens",
                    headers={"Authorization": "Bearer " + token},
                    json={"refresh": refresh, "generation": self.generation if generation is None else generation},
                ) as response:
                    if response.status != 200:
                        raise RuntimeError("Benchmark login unavailable")
                    result = await response.json()
                    self.generation = result.pop("generation")
                    return result
        except (TimeoutError, aiohttp.ClientConnectionError) as error:
            # This client uses only the fixed local Unix transport. Configuration,
            # authentication, malformed replies and TLS failures are not outages.
            if isinstance(error, aiohttp.ClientSSLError):
                raise
            raise TokenBrokerUnavailable("AUTH_BROKER_UNAVAILABLE") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/auth"))
    args = parser.parse_args()
    os.umask(0o077)
    directory = private_directory(args.root / "ipc")
    path = directory / "broker.sock"
    path.unlink(missing_ok=True)
    broker = LoginBroker(args.root)
    web.run_app(
        broker.application(),
        path=str(path),
        access_log=None,
        print=lambda _: write_private_json(args.root / "ready.json", {"ready": True, "at": time.time()}),
    )


if __name__ == "__main__":
    main()
