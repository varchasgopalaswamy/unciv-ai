"""Opt-in packaged Unix-socket probe with a fake upstream and no network access.

CIV_CONTAINER_TESTS=1 CIV_TEST_IMAGE=localhost/civ-agent-trial-checks:runtime \
    uv run --no-sync pytest tests/test_decisions_container.py -q
"""

import json
import os
import secrets
import time

import pytest

from civ_agent.container_supervisor import authorize_agent, decisions_arguments
from civ_agent.container_support import base_arguments, mount, podman
from civ_agent.decisions_broker import BROKER_PROTOCOL_VERSION
from civ_agent.decisions_policy import DecisionsSettings
from civ_agent.diplomacy_decisions import POLICY_VERSION
from civ_agent.game_config import ContainerResources
from civ_agent.tls import private_directory, write_private_json

pytestmark = pytest.mark.skipif(os.environ.get("CIV_CONTAINER_TESTS") != "1", reason="requires rootless Podman")

BROKER = """
from civ_agent import decisions_broker
from civ_agent.decisions_policy import DecisionsSettings, routing_thresholds
from civ_agent.diplomacy_decisions import POLICY_VERSION

class FakeDecisions:
    def __init__(self, key=None, *, backend):
        assert key is None and backend == 'jev'

    def health_snapshot(self):
        return {
            'upstreamStatus': 'unchecked',
            'circuit': {'state': 'closed', 'consecutiveFailures': 0, 'retryAfterSeconds': 0},
            'lastFailure': None,
        }

    async def classify(self, direction, bundle, *, timeout, thresholds, policy_version):
        settings = DecisionsSettings(decisions_enabled=True)
        assert policy_version == POLICY_VERSION == settings.decisions_routing_policy
        assert timeout == settings.decisions_timeout_seconds
        assert thresholds == routing_thresholds(settings, direction)
        return {
            'choice': 'batch', 'confidence': 0.99, 'originalBundle': bundle,
            'direction': direction, 'policyVersion': policy_version,
        }

decisions_broker.DecisionsClient = FakeDecisions
decisions_broker.main()
"""

PLAYER = """
import asyncio
import json
from pathlib import Path
from civ_agent.decisions_broker import RoutingClient

async def main():
    client = RoutingClient.from_environment()
    bundle = {
        'audience': ['Greece', 'Rome'],
        'text': 'Please send the offer next turn.',
        'history': [{'speaker': 'Greece', 'text': 'I intend to send it.'}],
    }
    denied = RoutingClient(client.socket_path, Path(client.token_file).with_name('wrong.json'))
    print(json.dumps({
        'health': await client.health(),
        'classification': await client.classify('outgoing', bundle),
        'wrongHealth': await denied.health(),
        'wrongClassification': await denied.classify('outgoing', bundle),
        'bundle': bundle,
        'keyVisible': any(Path(path).exists() for path in (
            '/auth/decisions.key', '/decisions-ipc/decisions.key', '/run/secrets/decisions.key',
        )),
        'registryVisible': Path('/auth/decisions-clients.json').exists(),
    }))

asyncio.run(main())
"""


def test_packaged_broker_authenticated_socket_mount_and_restart(tmp_path):
    image = os.environ.get("CIV_TEST_IMAGE")
    if not image:
        pytest.skip("set CIV_TEST_IMAGE to the runtime image built from this checkout")
    rootless = podman("info", "--format", "{{.Host.Security.Rootless}}")
    assert rootless.stdout.strip() == "true", "requires a rootless Podman runtime"
    if podman("image", "exists", image, check=False).returncode:
        pytest.skip(f"configured runtime image is not present locally: {image}")
    game_id = secrets.token_hex(5)
    broker_name, player_name = f"civ-{game_id}-routing-broker", f"civ-{game_id}-routing-player"
    auth = private_directory(tmp_path / "auth")
    private_directory(auth / "decisions-ipc")
    player = private_directory(tmp_path / "player")
    authorize_agent(
        auth,
        game_id,
        0,
        player,
        decisions=True,
        settings=DecisionsSettings(decisions_enabled=True).model_dump(),
        player="Rome",
    )
    write_private_json(player / "wrong.json", {"token": "unregistered-container-test-token"})
    names = []

    def wait_ready():
        deadline = time.monotonic() + 20
        while not (auth / "decisions-ready.json").exists():
            details = json.loads(podman("inspect", broker_name).stdout)[0]
            assert details["State"]["Running"], podman("logs", broker_name).stderr
            assert time.monotonic() < deadline, "packaged routing broker did not become ready"
            time.sleep(0.1)
        ready = json.loads((auth / "decisions-ready.json").read_text())
        assert ready["ready"] and ready["protocolVersion"] == BROKER_PROTOCOL_VERSION

    def probe():
        result = json.loads(podman("exec", player_name, "/opt/app/.venv/bin/python", "-c", PLAYER).stdout)
        assert result["health"]["status"] == "ready"
        assert result["health"]["protocolVersion"] == BROKER_PROTOCOL_VERSION
        assert result["health"]["upstreamStatus"] == "unchecked"
        assert result["classification"] == {
            "choice": "batch",
            "confidence": 0.99,
            "originalBundle": result["bundle"],
            "direction": "outgoing",
            "policyVersion": POLICY_VERSION,
        }
        assert result["wrongHealth"]["reason"] == "broker_access_denied"
        assert result["wrongClassification"]["fallback"] == "broker_access_denied"
        assert result["wrongClassification"]["choice"] == "send_now"
        assert result["keyVisible"] is False and result["registryVisible"] is False

    try:
        podman(
            *base_arguments(broker_name, game_id=game_id, resources=ContainerResources(memory_mib=256)),
            "--network=none",
            *mount(auth, "/auth", writable=True),
            "--entrypoint",
            "/opt/app/.venv/bin/python",
            image,
            "-c",
            BROKER,
        )
        names.append(broker_name)
        podman("start", broker_name)
        wait_ready()
        podman(
            *base_arguments(player_name, game_id=game_id, resources=ContainerResources(memory_mib=256)),
            "--network=none",
            *mount(player, "/match/agent-services/Rome"),
            *decisions_arguments(auth, "Rome"),
            "--entrypoint",
            "/opt/app/.venv/bin/python",
            image,
            "-c",
            "import time; time.sleep(3600)",
        )
        names.append(player_name)
        podman("start", player_name)
        probe()
        details = json.loads(podman("inspect", player_name).stdout)[0]
        mounts = {entry["Destination"]: entry for entry in details["Mounts"]}
        assert set(mounts) == {"/match/agent-services/Rome", "/decisions-ipc"}
        assert mounts["/decisions-ipc"]["RW"] is False
        assert details["HostConfig"]["ReadonlyRootfs"] and not details["EffectiveCaps"]
        assert details["HostConfig"]["NetworkMode"] == "none"
        podman("stop", "--time", "5", broker_name)
        (auth / "decisions-ready.json").unlink()
        podman("start", broker_name)
        wait_ready()
        probe()  # The existing player reconnects through the replaced socket inode.
    finally:
        for name in reversed(names):
            podman("stop", "--time", "5", name, check=False)
            podman("rm", name, check=False)
