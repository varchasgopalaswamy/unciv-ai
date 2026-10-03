"""Rootless Podman primitives. Only the trusted bare-metal supervisor uses these."""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from civ_agent.game_config import ContainerResources
from civ_agent.tls import private_directory, write_private_json

LABEL = "local.civ-agent.game"
DEFAULT_AUTH = Path.home() / ".local/state/unciv-host/auth"
IN_CONTAINER = os.environ.get("CIV_AGENT_CONTAINER") == "1"


class ContainerError(RuntimeError):
    pass


class LockBusy(ContainerError):
    pass


def command(arguments, *, check=True, timeout=60, **kwargs):
    result = subprocess.run(arguments, text=True, capture_output=True, timeout=timeout, **kwargs)
    if check and result.returncode:
        raise ContainerError(f"{arguments[0]} failed: {(result.stderr or result.stdout).strip()[-3000:]}")
    return result


def podman(*arguments, **kwargs):
    return command(["podman", *map(str, arguments)], **kwargs)


def read_json(path: Path, default: Any = None) -> Any:
    return json.loads(path.read_text()) if path.exists() else default


@contextlib.contextmanager
def locked(path, *, wait=False):
    private_directory(path.parent)
    with path.open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            raise LockBusy(f"Another supervisor owns {path.parent}") from None
        yield


def identity(root) -> dict[str, Any]:
    # Persisted IDs survive moving the state directory; names never contain user input.
    from secrets import token_hex

    path = root / "container.json"
    record = read_json(path)
    if record is None:
        record = {"id": token_hex(5), "format": 1}
        write_private_json(path, record)
    if record.get("format") != 1 or not re.fullmatch("[a-f0-9]{10}", record.get("id", "")):
        raise ContainerError("Unsupported container manifest; select a new match directory")
    return record


def resolve_image(name):
    result = podman("image", "inspect", name)
    return json.loads(result.stdout)[0]["Id"]


def preflight(*, background=True):
    info = json.loads(podman("info", "--format", "json").stdout)
    host = info["host"]
    if not host["security"]["rootless"] or host["cgroupVersion"] != "v2" or host["networkBackend"] != "netavark":
        raise ContainerError("Rootless Podman with cgroup v2 and Netavark is required")
    # Permission errors are fatal; never silently fall back to a shared network.
    command(["podman", "unshare", "--rootless-netns", "nft", "list", "tables"])
    if background:
        command(["systemctl", "--user", "show-environment"])
        linger = command(["loginctl", "show-user", str(os.getuid()), "-p", "Linger", "--value"])
        if linger.stdout.strip() != "yes":
            raise ContainerError("Run 'civ-agent runtime setup' once to enable runs to survive logout")


def resource_arguments(resources: ContainerResources):
    return [
        "--memory",
        f"{resources.memory_mib}m",
        "--memory-swap",
        f"{resources.memory_mib}m",
        "--cpus",
        str(resources.cpus),
        "--pids-limit",
        str(resources.pids),
    ]


def base_arguments(name, *, game_id, resources):
    return [
        "create",
        "--name",
        name,
        "--label",
        f"{LABEL}={game_id}",
        "--userns=keep-id",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--read-only",
        "--read-only-tmpfs=false",
        "--init",
        "--stop-timeout=90",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=512m,mode=1777",
        "--tmpfs",
        "/run:rw,nosuid,nodev,size=32m,mode=755",
        "--env",
        "CIV_AGENT_CONTAINER=1",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--sysctl",
        "net.ipv6.conf.all.disable_ipv6=1",
        *resource_arguments(resources),
    ]


def mount(source, destination, *, writable=False):
    # Shared SELinux label is needed for controller/agent/TUI access to the same
    # directory. Mount visibility, not a shared filesystem root, limits each role.
    if ":" in str(source) or "\n" in str(source):
        raise ContainerError("Container state paths cannot contain ':' or newlines")
    return ["--volume", f"{source}:{destination}:{'rw' if writable else 'ro'},z"]


def watch_mounts(root):
    """SQLite readers may create WAL coordination files even with mode=ro."""
    from civ_agent.game_config import ResolvedGameConfig
    from civ_agent.turn_archive import civilization_directory_name

    directories = [Path("control"), Path("runner")]
    config = ResolvedGameConfig.model_validate(read_json(root / "resolved.json"))
    for player in config.agent_players:
        base = Path("agent-services") / civilization_directory_name(player.civilization)
        directories += [base / "civilization", base / "telemetry"]
    arguments = mount(root, "/match")
    for directory in directories:
        private_directory(root / directory)
        arguments += mount(root / directory, Path("/match") / directory, writable=True)
    return arguments


def inspected(name):
    result = podman("container", "inspect", name, check=False)
    return json.loads(result.stdout)[0] if result.returncode == 0 else None


def create_network(name, bridge, *, internal=False):
    arguments = [
        "network",
        "create",
        "--label",
        f"{LABEL}={name.split('-')[1]}",
        "--interface-name",
        bridge,
        "--opt",
        "isolate=true",
    ]
    if internal:
        arguments.append("--internal")
    podman(*arguments, name)
    return json.loads(podman("network", "inspect", name).stdout)[0]


def firewall(game_id, bridges, *, remove=False):
    """Scope rules to our bridges inside Podman's user-owned network namespace."""
    table = "civ_" + game_id
    prefix = ["podman", "unshare", "--rootless-netns", "nft"]
    exists = command([*prefix, "list", "table", "inet", table], check=False).returncode == 0
    if remove:
        if exists:
            command([*prefix, "delete", "table", "inet", table])
        return
    addresses = json.loads(command(["ip", "-j", "address", "show"]).stdout)
    blocked = {
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "224.0.0.0/4",
        "240.0.0.0/4",
    }
    # Also block globally routed addresses assigned to the physical host.
    for interface in addresses:
        for addr in interface.get("addr_info", []):
            if addr["family"] == "inet":
                blocked.add(addr["local"])
    names = ", ".join(json.dumps(name) for name in bridges)
    sources = "{ " + names + " }"
    script = (
        (f"delete table inet {table}\n" if exists else "")
        + f"""
table inet {table} {{
    set blocked {{ type ipv4_addr; flags interval; auto-merge; elements = {{ {", ".join(sorted(blocked))} }}; }}
    chain ingress {{ type filter hook input priority -30; policy accept;
        iifname {sources} udp dport 53 accept
        iifname {sources} tcp dport 53 accept
        iifname {sources} drop
    }}
    chain transit {{ type filter hook forward priority -30; policy accept;
        iifname {sources} meta nfproto ipv6 drop
        iifname {sources} ip daddr @blocked drop
    }}
}}
"""
    )
    command([*prefix, "-f", "-"], input=script)


def stop_containers(names):
    for name in reversed(names):
        podman("stop", "--time", "90", name, check=False, timeout=100)


def cleanup(game_id):
    result = podman("ps", "--all", "--filter", f"label={LABEL}={game_id}", "--format", "{{.Names}}")
    names = result.stdout.split()
    stop_containers(names)
    if names:
        podman("rm", *names)
    result = podman("network", "ls", "--filter", f"label={LABEL}={game_id}", "--format", "{{.Name}}")
    for name in result.stdout.split():
        podman("network", "rm", name)
    firewall(game_id, [], remove=True)


def auth_name(root):
    return "civ-auth-" + hashlib.sha256(str(root).encode()).hexdigest()[:10]


def launch_background(root, role):
    import sys

    unit = "civ-" + role + "-" + identity(root)["id"]
    command(
        [
            "systemd-run",
            "--user",
            "--collect",
            "--unit",
            unit,
            "--property=TimeoutStopSec=240",
            "--property=KillMode=mixed",
            "--property=UMask=0077",
            "--property=WorkingDirectory=/",
            f"--property=StandardOutput=append:{root / 'supervisor.log'}",
            f"--property=StandardError=append:{root / 'supervisor.log'}",
            str(Path(sys.executable).absolute()),
            "-m",
            "civ_agent.container_supervisor",
            role,
            "--state-dir",
            str(root),
        ]
    )
    return unit
