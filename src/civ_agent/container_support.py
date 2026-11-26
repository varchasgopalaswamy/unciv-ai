"""Rootless Podman primitives. Only the trusted bare-metal supervisor uses these."""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from civ_agent.game_config import ContainerResources
from civ_agent.tls import private_directory, regular_file, write_private_json

LABEL = "local.civ-agent.game"
SERVICE_LABEL = "local.civ-agent.service"
DEFAULT_AUTH = Path.home() / ".local/state/unciv-host/auth"
IN_CONTAINER = os.environ.get("CIV_AGENT_CONTAINER") == "1"
USER_RUNTIME_ROOT = Path("/run/user")


class ContainerError(RuntimeError):
    pass


class LockBusy(ContainerError):
    pass


@dataclass(frozen=True)
class ContainerService:
    """Physical process ownership, independent of native game/workflow state."""

    name: str
    cid: str
    image: str
    owner: str
    unit: str

    @classmethod
    def load(cls, value):
        result = None
        try:
            result = cls(**value)
            valid = (
                re.fullmatch(r"[a-zA-Z0-9_.-]{1,180}", result.name)
                and re.fullmatch(r"[a-f0-9]{64}", result.cid)
                and re.fullmatch(r"(?:sha256:)?[a-f0-9]{64}", result.image)
                and re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", result.owner)
                and re.fullmatch(r"civ-[a-zA-Z0-9_.-]{1,220}\.service", result.unit)
            )
        except (TypeError, KeyError):
            valid = False
        if not valid or result is None:
            raise ContainerError("Invalid container process-owner descriptor")
        return result

    def record(self):
        return asdict(self)


def require_restart_systemd():
    lines = command(["systemctl", "--version"]).stdout.splitlines()
    version = lines[0] if lines else ""
    match = re.match(r"systemd (\d+)", version)
    if match is None or int(match[1]) < 254:
        raise ContainerError("Container restart ownership requires systemd 254 or newer (bounded restart backoff)")


def restart_properties():
    # Keep failed units loaded: collecting them would reset the start allowance.
    return [
        "--property=Restart=on-failure",
        "--property=RestartSec=2s",
        "--property=RestartSteps=2",
        "--property=RestartMaxDelaySec=8s",
        "--property=StartLimitIntervalSec=infinity",
        "--property=StartLimitBurst=4",
        "--property=CollectMode=inactive",
    ]


def unit_state(unit):
    if not re.fullmatch(r"civ-[a-zA-Z0-9_.-]{1,220}(?:\.service)?", unit):
        raise ContainerError("Invalid owned service unit")
    result = command(
        [
            "systemctl",
            "--user",
            "show",
            unit,
            "--no-pager",
            "--property="
            + ",".join(
                (
                    "LoadState",
                    "ActiveState",
                    "SubState",
                    "Result",
                    "NRestarts",
                    "ExecMainStatus",
                    "Description",
                    "ExecStart",
                    "ExecStop",
                )
            ),
        ],
        check=False,
    )
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if values.get("LoadState") == "not-found":
        return values
    if result.returncode or values.get("LoadState") != "loaded":
        raise ContainerError("Could not inspect owned systemd service")
    return values


def _unit_argv(value):
    matched = re.fullmatch(r"\{ path=([^;]+) ; argv\[\]=(.*?) ; .*\}", value)
    return shlex.split(matched[2]) if matched is not None else None


def verify_container_service(service: ContainerService, *, unit_required=True):
    service = ContainerService.load(service.record())
    current = inspected(service.cid)
    if (
        current is None
        or current.get("Id") != service.cid
        or current.get("Name", "").lstrip("/") != service.name
        or current.get("Image", "").removeprefix("sha256:") != service.image.removeprefix("sha256:")
        or current.get("Config", {}).get("Labels", {}).get(LABEL) != service.owner
        or current.get("Config", {}).get("Labels", {}).get(SERVICE_LABEL) != service.unit
        or current.get("HostConfig", {}).get("RestartPolicy", {}).get("Name", "no") not in {"", "no", "never"}
    ):
        raise ContainerError("Container process ownership differs; explicit operator replacement required")
    return current, verify_container_unit(service, required=unit_required)


def verify_container_unit(service: ContainerService, *, required=True):
    """Verify the recorded process owner even when its container was removed."""
    service = ContainerService.load(service.record())
    state = unit_state(service.unit)
    if state.get("LoadState") == "not-found" and not required:
        return state
    expected_start = ["/usr/bin/podman", "start", "--attach", "--sig-proxy=false", service.cid]
    expected_stop = ["/usr/bin/podman", "stop", "--ignore", "--time", "90", service.cid]
    if (
        state.get("Description") != "Civ container " + service.owner + " " + service.cid
        or _unit_argv(state.get("ExecStart", "")) != expected_start
        or _unit_argv(state.get("ExecStop", "")) != expected_stop
    ):
        raise ContainerError("Systemd process ownership differs; refusing to adopt service")
    return state


def start_container_service(service: ContainerService, *, create=False, log_path: Path | None = None):
    _, state = verify_container_service(service, unit_required=not create)
    if state.get("LoadState") != "not-found":
        # Observation/rejoin never restarts a stopped/failed unit or resets its
        # allowance. Only the already installed systemd policy owns retries.
        if state.get("ActiveState") not in {"active", "activating"}:
            raise ContainerError("Owned container service stopped or exhausted its restart allowance")
        return
    require_restart_systemd()
    output = []
    if log_path is not None:
        if not log_path.is_absolute():
            raise ContainerError("Container service log must have an absolute private path")
        private_directory(log_path.parent)
        log_path.touch(mode=0o600, exist_ok=True)
        regular_file(log_path, private=True)
        escaped = systemd_path(log_path)
        output = ["--property=StandardOutput=append:" + escaped, "--property=StandardError=append:" + escaped]
    command(
        [
            "systemd-run",
            "--user",
            "--unit",
            service.unit,
            "--expand-environment=no",
            "--property=Type=exec",
            "--property=UMask=0077",
            "--property=TimeoutStopSec=100",
            "--property=Description=Civ container " + service.owner + " " + service.cid,
            "--property=ExecStop=/usr/bin/podman stop --ignore --time 90 " + service.cid,
            *restart_properties(),
            *output,
            "/usr/bin/podman",
            "start",
            "--attach",
            "--sig-proxy=false",
            service.cid,
        ]
    )


def stop_container_service(service: ContainerService, *, collect=False):
    current, state = verify_container_service(service, unit_required=False)
    if state.get("LoadState") == "not-found":
        if not current["State"]["Running"]:
            return
        raise ContainerError("Owned running container lost its systemd owner")
    command(["systemctl", "--user", "stop", service.unit], timeout=110)
    current = inspected(service.cid)
    if current is None or current["State"]["Running"]:
        raise ContainerError("Could not confirm owned container service stopped")
    if collect:
        wait_unit_collected(service.unit, reset_failed=True)


def wait_unit_collected(unit, *, reset_failed=False):
    """An explicit replacement must not race collection of its stopped unit."""
    deadline = time.monotonic() + 5
    while True:
        state = unit_state(unit)
        if state.get("LoadState") == "not-found":
            return
        if reset_failed and state.get("ActiveState") == "failed":
            # Only an explicitly authorized replacement reaches this branch,
            # after the caller verified ownership and stopped the exact unit.
            command(["systemctl", "--user", "reset-failed", unit])
            reset_failed = False
        if time.monotonic() >= deadline:
            raise ContainerError("Stopped unit is still loaded; retry explicit restart after references close")
        time.sleep(0.05)


def host_environment():
    """Recover an existing user session when a non-login shell omits its address."""
    environment = dict(os.environ)
    if environment.get("CIV_AGENT_CONTAINER") == "1":
        return environment
    runtime = Path(environment.get("XDG_RUNTIME_DIR") or USER_RUNTIME_ROOT / str(os.getuid()))
    try:
        directory = runtime.stat()
        if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid():
            return environment
        if not environment.get("XDG_RUNTIME_DIR"):
            environment["XDG_RUNTIME_DIR"] = str(runtime)
        bus = (runtime / "bus").stat()
        if stat.S_ISSOCK(bus.st_mode) and bus.st_uid == os.getuid() and not environment.get("DBUS_SESSION_BUS_ADDRESS"):
            environment["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + str(runtime / "bus")
    except OSError:
        pass  # No existing session to recover; retain the native diagnostic.
    return environment


def command(arguments, *, check=True, timeout=60, **kwargs):
    if "env" not in kwargs:
        kwargs["env"] = host_environment()
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
        from civ_agent.container_identity import LOCAL_RUNTIME

        record = {"id": token_hex(5), "format": 2, "runtime": LOCAL_RUNTIME}
        write_private_json(path, record)
    if record.get("format") not in {1, 2} or not re.fullmatch("[a-f0-9]{10}", record.get("id", "")):
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
        require_restart_systemd()
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
    # Shared SELinux label lets the controller and the operator reader open
    # the same private SQLite files.
    if ":" in str(source) or "\n" in str(source):
        raise ContainerError("Container state paths cannot contain ':' or newlines")
    return ["--volume", f"{source}:{destination}:{'rw' if writable else 'ro'},z"]


def watch_mounts(root):
    """Observers consume runtime publications through a read-only mount."""
    return mount(root, "/match")


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
    if names:
        for name in names:
            current = inspected(name)
            if current is None:
                raise ContainerError("Could not inspect container before cleanup")
            unit = current.get("Config", {}).get("Labels", {}).get(SERVICE_LABEL)
            if unit:
                stop_container_service(
                    ContainerService.load(
                        {
                            "name": name,
                            "cid": current["Id"],
                            "image": current["Image"],
                            "owner": game_id,
                            "unit": unit,
                        }
                    )
                )
        # Normal shutdown has already stopped these. Avoid another stop process
        # for every exited player, while still draining live orphaned services.
        running = podman("ps", "--filter", f"label={LABEL}={game_id}", "--format", "{{.Names}}")
        stop_containers(running.stdout.split())
        podman("rm", *names)
    result = podman("network", "ls", "--filter", f"label={LABEL}={game_id}", "--format", "{{.Name}}")
    for name in result.stdout.split():
        podman("network", "rm", name)
    firewall(game_id, [], remove=True)


def auth_name(root):
    return "civ-auth-" + hashlib.sha256(str(root).encode()).hexdigest()[:10]


def launch_background(root, role):
    import sys

    from civ_agent.container_identity import deployment_identity

    unit = "civ-" + role + "-" + deployment_identity(identity(root)).id
    require_restart_systemd()
    record = identity(root)
    expected = {"backend": "systemd-existing-cid-v1", "unit": unit}
    previous = unit_state(unit)
    if previous.get("LoadState") != "not-found":
        # This function is called only by an explicit CLI launch. Never replace
        # an unknown unit, nor silently reset an automatically exhausted owner.
        if record.get("supervision") != expected:
            raise ContainerError("Refusing to replace an unowned existing supervisor unit")
        if previous.get("ActiveState") not in {"inactive", "failed"}:
            raise ContainerError("The deployment already has a live systemd supervisor")
        command(["systemctl", "--user", "stop", unit], timeout=250)
        wait_unit_collected(unit, reset_failed=True)
    record["supervision"] = expected
    write_private_json(root / "container.json", record)
    command(
        [
            "systemd-run",
            "--user",
            "--unit",
            unit,
            "--expand-environment=no",
            "--property=Type=exec",
            "--property=Environment=CIV_SUPERVISOR_UNIT=" + unit,
            "--property=RestartPreventExitStatus=78",
            *restart_properties(),
            "--property=TimeoutStopSec=240",
            "--property=KillMode=mixed",
            "--property=UMask=0077",
            "--property=WorkingDirectory=/",
            f"--property=StandardOutput=append:{systemd_path(root / 'supervisor.log')}",
            f"--property=StandardError=append:{systemd_path(root / 'supervisor.log')}",
            str(Path(sys.executable).absolute()),
            "-m",
            "civ_agent.container_supervisor",
            role,
            "--state-dir",
            str(root),
        ]
    )
    return unit


def systemd_path(path: Path) -> str:
    return str(path).replace("%", "%%")
