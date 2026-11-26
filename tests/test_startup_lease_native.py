import asyncio
import tempfile
from pathlib import Path

from civ_agent.runtime import controller as module
from civ_agent.tls import write_private_json
from tests.test_local_controller_native import local_config, native_game


def test_delayed_provider_preparation_preserves_the_native_lease(monkeypatch):
    original = module.build_players
    checked = []

    async def delayed(root, config, manifest, operator, *args, **kwargs):
        before = (await asyncio.to_thread(operator.call, "status"))["status"]
        await asyncio.sleep(35)  # The selected engine's lease lifetime is 30 seconds.
        after = (await asyncio.to_thread(operator.call, "status"))["status"]
        assert before["leaseId"] == after["leaseId"] == manifest["native"]["leaseId"]
        assert before["leaseGeneration"] == after["leaseGeneration"]
        assert after["leaseExpiresInMs"] > 0
        checked.append(after["leaseGeneration"])
        return await original(root, config, manifest, operator, *args, **kwargs)

    monkeypatch.setattr(module, "build_players", delayed)
    # A short owned path keeps native Unix sockets within their path bound.
    with tempfile.TemporaryDirectory(prefix="civ-lease-") as temporary:
        root = Path(temporary)
        with native_game(root) as (url, ca, _token):
            config = local_config()
            config.limits.max_seconds = 180
            write_private_json(root / "resolved.json", config.model_dump(mode="json"))
            write_private_json(
                root / "container.json",
                {
                    "id": "delayed-startup-test",
                    "runtime": "pydantic-local-v1",
                    "launch": "initial",
                    "native": {"leaseId": "delayed-startup-test-lease"},
                },
            )
            state = asyncio.run(module.run_controller(root, resume=False, host_url=url, host_ca=ca))
            assert state.mode in {"STOPPED", "FINISHED"}, state.reason
            assert checked and state.handoff >= 1
