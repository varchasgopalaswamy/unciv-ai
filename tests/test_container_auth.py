"""Token renewal is centralized; workers never receive renewable credentials."""

import asyncio
import hashlib
import json
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer

from civ_agent.container_auth import LoginBroker
from civ_agent.container_supervisor import authorize_agent
from civ_agent.tls import private_directory, write_private_json


def test_token_renewal_concurrency_restart_and_privacy(tmp_path):
    async def exercise():
        cache = tmp_path / "codex/auth.json"

        def save(token):
            write_private_json(
                cache,
                {"tokens": {"access_token": token, "account_id": "account", "refresh_token": "never-return-this"}},
            )

        save("old-access")
        write_private_json(tmp_path / "clients.json", {"one": hashlib.sha256(b"worker-secret").hexdigest()})
        rpc = AsyncMock()

        async def request(method, params):
            if params.get("refreshToken"):
                await asyncio.sleep(0.02)
                save("new-access")
            return {"account": {"id": "account"}}

        rpc.request.side_effect = request

        async def check(broker, *, restart=False):
            async with TestClient(TestServer(broker.application())) as client:
                bad = await client.post("/tokens", json={})
                assert bad.status == 401
                headers = {"Authorization": "Bearer worker-secret"}
                initial = await (await client.post("/tokens", headers=headers, json={})).json()
                assert set(initial) == {"accessToken", "chatgptAccountId", "generation"}
                generation = initial["generation"]
                if restart:
                    assert generation == hashlib.sha256(b"new-access").hexdigest()
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
                assert all(result["accessToken"] == "new-access" for result in results)
                assert all("never-return-this" not in json.dumps(result) for result in results)

        await check(LoginBroker(tmp_path, rpc))
        await check(LoginBroker(tmp_path, rpc), restart=True)
        assert sum(call.args[1].get("refreshToken", False) for call in rpc.request.call_args_list) == 1

    asyncio.run(exercise())


def test_parallel_game_authorization_retains_all_clients(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    auth = private_directory(tmp_path / "auth")
    directories = [private_directory(tmp_path / f"seat{i}") for i in range(12)]
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda row: authorize_agent(auth, "game", *row), enumerate(directories)))
    assert len(json.loads((auth / "clients.json").read_text())) == 12


def test_workspace_rewind_preserves_mount_root_and_retries(tmp_path):
    from civ_agent.service_recovery import archive_workspace

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "old").mkdir()
    (workspace / "old/file").write_text("discarded turn")
    (workspace / "link").symlink_to("old/file")
    inode = workspace.stat().st_ino
    destination = tmp_path / "archive"
    archive_workspace(workspace, destination)
    assert workspace.stat().st_ino == inode and not list(workspace.iterdir())
    assert (destination / "old/file").read_text() == "discarded turn"
    assert (destination / "link").is_symlink()
    # A stopped restore can repeat after the archive has committed.
    (workspace / "leftover").write_text("partial restore")
    archive_workspace(workspace, destination)
    assert not list(workspace.iterdir()) and workspace.stat().st_ino == inode
