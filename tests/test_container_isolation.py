"""Opt-in real rootless network/filesystem probes; no model requests.

CIV_CONTAINER_TESTS=1 .venv/bin/pytest tests/test_container_isolation.py -q
"""

import ipaddress
import json
import os
import secrets
import socket

import pytest

from civ_agent.container_support import base_arguments, cleanup, command, create_network, firewall, mount, podman
from civ_agent.game_config import ContainerResources

pytestmark = pytest.mark.skipif(os.environ.get("CIV_CONTAINER_TESTS") != "1", reason="requires rootless Podman")


def test_player_boundaries_public_egress_and_no_host_mounts(tmp_path):
    game_ids = [secrets.token_hex(5) for _ in range(2)]
    containers = []
    addresses = []
    image = os.environ.get("CIV_TEST_IMAGE", "localhost/civ-agent:runtime")
    host_addresses = json.loads(command(["ip", "-j", "address", "show"]).stdout)
    physical_ip = next(
        address["local"]
        for interface in host_addresses
        for address in interface.get("addr_info", [])
        if address["family"] == "inet" and address["local"] != "127.0.0.1"
    )
    listener = socket.socket()
    listener.bind(("0.0.0.0", 0))
    listener.listen()
    with socket.create_connection((physical_ip, listener.getsockname()[1]), timeout=1):
        pass  # Establish the host target is listening before the negative probe.
    try:
        for game_id in game_ids:
            bridges = []
            for seat in range(2):
                name = f"civ-{game_id}-p{seat}"
                bridge = f"ct{game_id[:8]}{seat}"
                network = create_network(name, bridge)
                bridges.append(bridge)
                subnet = ipaddress.ip_network(network["subnets"][0]["subnet"])
                address = str(subnet[3])
                addresses.append((address, str(subnet[1])))
                own = tmp_path / name
                own.mkdir()
                (own / "private").write_text(name)
                podman(
                    *base_arguments(name, game_id=game_id, resources=ContainerResources(memory_mib=256)),
                    "--network",
                    f"{name}:ip={address}",
                    *mount(own, "/private", writable=True),
                    "--entrypoint",
                    "/opt/app/.venv/bin/python",
                    image,
                    "-m",
                    "http.server",
                    "8080",
                    "--bind",
                    "0.0.0.0",
                    "--directory",
                    "/private",
                )
                podman("start", name)
                containers.append(name)
            firewall(game_id, bridges)
        # Place the trusted controller on both player bridges. Players can reach
        # this service, but routing through its other interface is disabled.
        host = f"civ-{game_ids[0]}-host"
        host_address = str(ipaddress.ip_address(addresses[0][0]) - 1)
        podman(
            *base_arguments(host, game_id=game_ids[0], resources=ContainerResources(memory_mib=256)),
            "--network",
            f"{containers[0]}:ip={host_address},alias=game",
            "--network",
            f"{containers[1]}:alias=game",
            "--sysctl",
            "net.ipv4.ip_forward=0",
            "--entrypoint",
            "/opt/app/.venv/bin/python",
            image,
            "-m",
            "http.server",
            "8765",
        )
        podman("start", host)
        probe = """
import json, os, pathlib, socket, urllib.request
out = {}
for label, host, port in TARGETS:
    try:
        with socket.create_connection((host, port), timeout=1): pass
        out[label] = True
    except OSError: out[label] = False
try:
    out['internet'] = urllib.request.urlopen('https://example.com', timeout=20).status == 200
except Exception as e: out['internet'] = str(e)
out['own_file'] = pathlib.Path('/private/private').read_text()
out['host_path_visible'] = pathlib.Path(HOST_PATH).exists()
out['sibling_mount'] = pathlib.Path('/match').exists()
try:
    pathlib.Path('/opt/should-not-write').touch()
    out['root_writable'] = True
except OSError: out['root_writable'] = False
print(json.dumps(out))
"""
        targets = [
            ("controller", "game", 8765),
            ("same_game_sibling", addresses[1][0], 8080),
            ("other_game", addresses[2][0], 8080),
            ("bridge_host", addresses[0][1], 22),
            ("host_loopback", "169.254.1.2", 22),
            ("physical_host", physical_ip, listener.getsockname()[1]),
        ]
        code = f"TARGETS={targets!r}\nHOST_PATH={str(tmp_path)!r}\n" + probe
        output = podman("exec", containers[0], "/opt/app/.venv/bin/python", "-c", code).stdout
        result = json.loads(output)
        assert result == {
            "controller": True,
            "same_game_sibling": False,
            "other_game": False,
            "bridge_host": False,
            "host_loopback": False,
            "physical_host": False,
            "internet": True,
            "own_file": containers[0],
            "host_path_visible": False,
            "sibling_mount": False,
            "root_writable": False,
        }
        details = json.loads(podman("inspect", containers[0]).stdout)[0]
        assert details["HostConfig"]["ReadonlyRootfs"]
        assert not details["EffectiveCaps"]
        assert details["HostConfig"]["PidMode"] != "host"
    finally:
        listener.close()
        for game_id in game_ids:
            cleanup(game_id)


def test_dashboard_reads_stopped_snapshots_without_source_database_writes(tmp_path):
    from civ_agent.container_support import watch_mounts
    from civ_agent.game_config import GameConfig
    from civ_agent.monitoring import MonitorStore, path_for
    from civ_agent.tls import write_private_json

    config = GameConfig(game={"randomize": True, "assignment_seed": 7}).resolved()
    write_private_json(tmp_path / "resolved.json", config.model_dump())
    MonitorStore(path_for(tmp_path)).put("state", "run", {"status": "running"})
    # Coordination files belong only in the private stopped capture. The source
    # directory is mounted read-only and must keep exactly the same file set.
    before = {str(path.relative_to(tmp_path)): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    result = podman(
        "run",
        "--rm",
        "--network=none",
        "--userns=keep-id",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=128m,mode=1777",
        *watch_mounts(tmp_path),
        "--entrypoint",
        "/opt/app/.venv/bin/python",
        os.environ.get("CIV_TEST_IMAGE", "localhost/civ-agent:runtime"),
        "-c",
        """
from pathlib import Path
from civ_agent.monitoring import read_record
from civ_agent.operator_views import passive_root
from civ_agent.game_config import ResolvedGameConfig
config = ResolvedGameConfig.model_validate_json(Path('/match/resolved.json').read_text())
assert len(config.agent_players) == 3 and config.turn_order[-1].name == 'Unciv'
with passive_root(Path('/match')) as captured:
    assert captured != Path('/match')
    assert read_record(captured / 'runner/monitoring.sqlite3', 'state', 'run')['status'] == 'running'
print('passive snapshot query succeeded')
""",
    )
    assert result.stdout.strip() == "passive snapshot query succeeded"
    assert {
        str(path.relative_to(tmp_path)): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    } == before


@pytest.mark.parametrize("provider", ["openai", "openrouter"])
def test_worker_mounts_only_its_selected_provider_credential(tmp_path, provider):
    from civ_agent.container_supervisor import agent_auth_arguments
    from civ_agent.tls import private_directory, write_private

    private_directory(tmp_path / "ipc")
    write_private(tmp_path / "openrouter.key", b"synthetic-secret\n")
    result = podman(
        "run",
        "--rm",
        "--network=none",
        "--env",
        "OPENAI_API_KEY=ambient-openai-must-not-be-used",
        "--env",
        "OPENROUTER_API_KEY=ambient-router-must-not-be-used",
        "--userns=keep-id",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=128m,mode=1777",
        *agent_auth_arguments(tmp_path, "Rome", provider),
        "--entrypoint",
        "/opt/app/.venv/bin/python",
        os.environ.get("CIV_TEST_IMAGE", "localhost/civ-agent:runtime"),
        "-c",
        """
import asyncio
from pathlib import Path
from civ_agent.orchestration.contracts import ModelSelection
from civ_agent.orchestration.worker_configuration import WorkerProviderConfig, provider_clients

async def probe():
    assert Path('/run/secrets/openrouter.key').is_file() == (PROVIDER == 'openrouter')
    assert Path('/auth-ipc').is_dir() == (PROVIDER == 'openai')
    config = (WorkerProviderConfig(openrouter_key_file=Path('/run/secrets/openrouter.key'))
              if PROVIDER == 'openrouter' else
              WorkerProviderConfig(broker_socket=Path('/auth-ipc/broker.sock'),
                                   broker_token_file=Path('/auth-ipc/not-read-during-preflight.json')))
    selection = (ModelSelection(provider='openrouter', authentication='api_key', model='vendor/test')
                 if PROVIDER == 'openrouter' else ModelSelection(model='gpt-6.1-sol'))
    clients = provider_clients(config)
    try:
        model = await clients.model(selection)
        assert model.provider.client.api_key == ('synthetic-secret' if PROVIDER == 'openrouter'
                                                else 'broker-managed')
        assert (clients.broker is not None) == (PROVIDER == 'openai')
    finally:
        await clients.close()
    print('provider isolation verified')

asyncio.run(probe())
""".replace("PROVIDER", repr(provider)),
    )
    assert result.stdout.strip() == "provider isolation verified"
