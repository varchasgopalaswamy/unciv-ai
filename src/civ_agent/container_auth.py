"""One renewable benchmark login; workers receive only short-lived access tokens."""

import argparse
import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import time
from pathlib import Path

import aiohttp
from aiohttp import web

from civ_agent.tls import private_directory, write_private_json


class AccountRPC:
    """The login manager never creates model threads or performs inference."""

    def __init__(self, home, executable="codex"):
        self.home, self.executable = Path(home), executable
        self.sequence = 0
        self.process: asyncio.subprocess.Process | None = None
        self.session: aiohttp.ClientSession | None = None
        self.socket: aiohttp.ClientWebSocketResponse | None = None

    async def start(self):
        socket_path = Path("/tmp/codex-account.sock")
        socket_path.unlink(missing_ok=True)
        self.process = await asyncio.create_subprocess_exec(
            self.executable,
            "-c",
            'cli_auth_credentials_store="file"',
            "app-server",
            "--listen",
            "unix://" + str(socket_path),
            env={**os.environ, "CODEX_HOME": str(self.home)},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 20
        while not socket_path.exists():
            if self.process.returncode is not None or time.monotonic() > deadline:
                raise RuntimeError("Benchmark login process failed to start")
            await asyncio.sleep(0.05)
        self.session = aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(socket_path)))
        self.socket = await self.session.ws_connect("http://localhost/")
        await self.request(
            "initialize",
            {"clientInfo": {"name": "civ-login", "version": "1"}, "capabilities": {"experimentalApi": True}},
        )
        await self.socket.send_json({"method": "initialized", "params": {}})

    async def request(self, method, params):
        assert self.socket is not None
        self.sequence += 1
        request_id = self.sequence
        await self.socket.send_json({"id": request_id, "method": method, "params": params})
        async with asyncio.timeout(8):
            while True:
                message = await self.socket.receive_json()
                if message.get("id") == request_id and "method" not in message:
                    if "error" in message:
                        raise RuntimeError("Benchmark login requires operator attention")
                    return message.get("result", {})

    async def close(self):
        if self.socket:
            await self.socket.close()
        if self.session:
            await self.session.close()
        if self.process and self.process.returncode is None:
            self.process.terminate()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.process.wait(), 3)
            if self.process.returncode is None:
                self.process.kill()
                await self.process.wait()


class LoginBroker:
    def __init__(self, root, rpc=None):
        self.root = Path(root)
        self.rpc = rpc or AccountRPC(self.root / "codex")
        self.lock = asyncio.Lock()
        self.last_refresh = 0.0

    def tokens(self):
        # This is the manager's private cache, never mounted in a game/agent.
        record = json.loads((self.root / "codex/auth.json").read_text())
        tokens = record.get("tokens", {})
        access, account = tokens.get("access_token"), tokens.get("account_id")
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
        clients = json.loads(clients_path.read_text()) if clients_path.exists() else {}
        digest = hashlib.sha256(supplied.encode()).hexdigest()
        if not any(hmac.compare_digest(digest, expected) for expected in clients.values()):
            raise web.HTTPUnauthorized()
        data = await request.json()
        if not isinstance(data, dict) or set(data) - {"generation", "refresh"}:
            raise web.HTTPBadRequest()
        try:
            async with self.lock:
                # Concurrent 401s for one token bundle cause a single renewal.
                result = self.tokens()
                refresh = data.get("refresh") is True and data.get("generation") == result["generation"]
                refresh = refresh and time.monotonic() - self.last_refresh >= 5
                if refresh:
                    await self.rpc.request("account/read", {"refreshToken": True})
                    self.last_refresh = time.monotonic()
                    result = self.tokens()
                return web.json_response(result)
        except (OSError, ValueError, RuntimeError, TimeoutError, aiohttp.ClientError):
            # Auth responses/errors must never echo provider credentials or raw RPC payloads.
            raise web.HTTPServiceUnavailable(
                text="Benchmark login unavailable; inspect civ-agent auth status"
            ) from None

    async def lifecycle(self, app):
        await self.rpc.start()
        try:
            account = await self.rpc.request("account/read", {"refreshToken": False})
            if not account.get("account"):
                raise RuntimeError("Run civ-agent auth login before launching games")
            self.tokens()
            yield
        finally:
            await self.rpc.close()

    def application(self):
        app = web.Application(client_max_size=4096)
        app.router.add_post("/tokens", self.fetch)
        app.cleanup_ctx.append(self.lifecycle)
        return app


class TokenClient:
    def __init__(self, socket_path, token_file):
        self.socket_path, self.token_file = str(socket_path), Path(token_file)
        self.generation = None

    async def fetch(self, *, refresh=False):
        token = json.loads(self.token_file.read_text())["token"]
        async with aiohttp.ClientSession(
            connector=aiohttp.UnixConnector(path=self.socket_path),
            timeout=aiohttp.ClientTimeout(total=9),
        ) as session:
            async with session.post(
                "http://localhost/tokens",
                headers={"Authorization": "Bearer " + token},
                json={"refresh": refresh, "generation": self.generation},
            ) as response:
                if response.status != 200:
                    raise RuntimeError("Benchmark login unavailable")
                result = await response.json()
                self.generation = result.pop("generation")
                return result


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
