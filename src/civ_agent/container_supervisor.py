"""Trusted background supervision of independent, disposable game containers."""

import argparse
import asyncio
import concurrent.futures
import contextlib
import csv
import hashlib
import json
import logging
import os
import secrets
import signal
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

from sqlalchemy.exc import OperationalError

from civ_agent.container_support import (
    ContainerError,
    auth_name,
    base_arguments,
    cleanup,
    create_network,
    firewall,
    identity,
    inspected,
    locked,
    mount,
    podman,
    read_json,
    stop_containers,
)
from civ_agent.game_config import ContainerResources, ResolvedGameConfig
from civ_agent.monitoring import path_for, read_record
from civ_agent.protocol import JsonObject
from civ_agent.providers import read_api_key
from civ_agent.tls import private_directory, write_private_json
from civ_agent.turn_archive import civilization_directory_name

RECOVERABLE_MODEL_FAILURES = frozenset({"serverOverloaded", "internalServerError"})
logger = logging.getLogger(__name__)


def recoverable_model_failure(root: Path) -> str | None:
    """Return a machine-classified transient failure, never infer it from prose."""
    path = path_for(root)
    if not path.exists():
        return None
    state = read_record(path, "state", "run") or {}
    flight = state.get("inFlight") or {}
    model = flight.get("modelStatus") or {}
    reason = model.get("reason")
    if (
        state.get("status") == "paused"
        and flight.get("agentPhase") == "FAILED"
        and model.get("state") == "failed"
        and reason in RECOVERABLE_MODEL_FAILURES
    ):
        return reason
    return None


def ensure_auth(root, image, *, restart=False):
    private_directory(root)
    private_directory(root / "ipc")
    if not (root / "codex/auth.json").is_file():
        raise ContainerError("Run civ-agent auth login before launching games")
    with locked(root / ".launch.lock", wait=True):
        name = auth_name(root)
        current = inspected(name)
        if restart and current:
            if current["State"]["Running"]:
                podman("stop", "--time", "90", name, timeout=100)
            podman("rm", name)
            current = None
        if not current or not current["State"]["Running"]:
            if current:
                podman("rm", name)
            (root / "ready.json").unlink(missing_ok=True)
            arguments = base_arguments(name, game_id="auth", resources=ContainerResources(memory_mib=512, cpus=0.5))
            podman(
                *arguments,
                "--network=pasta:-4",
                *mount(root, "/auth", writable=True),
                "--env",
                "HOME=/auth",
                "--env",
                "CODEX_HOME=/auth/codex",
                image,
                "auth",
            )
            podman("start", name)
        deadline = time.monotonic() + 30
        while not (root / "ready.json").exists():
            current = inspected(name)
            if not current or not current["State"]["Running"] or time.monotonic() > deadline:
                raise ContainerError("Benchmark login manager failed; run civ-agent auth status")
            time.sleep(0.2)
    return name


def stop_auth(root):
    """Stop the login manager under its launch lock, preserving durable state."""
    name = auth_name(root)
    with locked(root / ".launch.lock", wait=True):
        current = inspected(name)
        if current is None:
            # Inspection also returns None on Podman errors. Only a confirmed
            # missing container is an idempotent success.
            exists = podman("container", "exists", name, check=False)
            if exists.returncode != 1:
                raise ContainerError(f"Could not inspect login manager {name}")
        elif current["State"]["Running"]:
            podman("stop", "--time", "90", name, timeout=100)
            current = inspected(name)
            if current is None or current["State"]["Running"]:
                raise ContainerError(f"Could not confirm login manager {name} stopped")
        (root / "ready.json").unlink(missing_ok=True)
    return {"auth_directory": str(root), "container": name, "manager_running": False}


def agent_auth_arguments(auth_root, name, provider):
    """Only the selected credential is mounted; keys never appear in Podman argv/env."""
    if provider == "openrouter":
        return [
            *mount(auth_root / "openrouter.key", "/run/secrets/openrouter.key"),
            "--env",
            "CIV_OPENROUTER_KEY_FILE=/run/secrets/openrouter.key",
        ]
    return [
        *mount(auth_root / "ipc", "/auth-ipc"),
        "--env",
        "CIV_AUTH_SOCKET=/auth-ipc/broker.sock",
        "--env",
        f"CIV_AUTH_TOKEN_FILE=/match/agent-services/{name}/auth-client.json",
    ]


def player_providers(config, player):
    """Provision credentials for the configured main and subordinate models."""
    settings = config.settings(player)
    return {settings["provider"], settings["economy_model"]["provider"], settings["military_model"]["provider"]}


def validate_routing_credentials(config, auth_root):
    if config.agents.decisions_enabled and config.agents.decisions_backend == "luna":
        key = auth_root / "decisions.key"
        if not key.is_file():
            raise ContainerError("Decisions requires a separate OpenAI API key in AUTH_DIR/decisions.key (mode 600)")
        from civ_agent.decisions_broker import read_decisions_key

        read_decisions_key(key)


def _decisions_preflight_error(context, health):
    from civ_agent.decisions_broker import health_failure, version_contract

    reason = health.get("reason")
    if reason not in {
        "broker_version_mismatch",
        "invalid_health_contract",
        "broker_timeout",
        "broker_access_denied",
        "broker_rate_limited",
        "broker_http_error",
        "broker_unavailable",
    }:
        reason = "invalid_health_contract"
    message = f"{context}: {reason}"
    mismatches = health.get("versionMismatches")
    if isinstance(mismatches, dict):
        # Revalidate even internal diagnostics before including values in an error.
        actual = {
            field: mismatch.get("actual")
            for field, mismatch in mismatches.items()
            if field in version_contract() and isinstance(mismatch, dict)
        }
        safe = health_failure(actual).get("versionMismatches", {})
        details = [
            f"{field} expected {mismatch['expected']}, found "
            f"{mismatch['actual'] if mismatch['actual'] is not None else 'invalid or missing'}"
            for field, mismatch in safe.items()
        ]
        if details:
            message += "; " + "; ".join(details)
    if health.get("status") == "incompatible":
        message += ". Run 'civ-agent image build', then 'civ-agent auth restart'."
    return ContainerError(message)


def _check_decisions_image(image):
    """Read only the selected image's protocol constants, without credentials or networking."""
    from civ_agent.decisions_broker import health_failure, version_contract

    script = (
        "import json; "
        "from civ_agent.decisions_broker import BROKER_PROTOCOL_VERSION; "
        "from civ_agent.diplomacy_decisions import MODEL, POLICY_VERSION; "
        "from civ_agent.decisions_tasks import TASK_VERSION; "
        "from civ_agent.decisions_ledger import RATE_VERSION; "
        "print(json.dumps(dict(protocolVersion=BROKER_PROTOCOL_VERSION,classifierModel=MODEL,"
        "routingPolicyVersion=POLICY_VERSION,advisoryPolicyVersion=TASK_VERSION,accountingRateVersion=RATE_VERSION)))"
    )
    try:
        result = podman(
            "run",
            "--rm",
            "--pull=never",
            "--network=none",
            "--read-only",
            "--cap-drop=all",
            "--security-opt=no-new-privileges",
            "--entrypoint",
            "/opt/app/.venv/bin/python",
            image,
            "-B",
            "-c",
            script,
            timeout=15,
            check=False,
        )
        if result.returncode != 0 or len(result.stdout) > 8192:
            raise ValueError("Unusable image contract")
        actual = json.loads(result.stdout)
    except (ContainerError, OSError, ValueError, subprocess.TimeoutExpired):
        raise ContainerError(
            "Could not inspect the selected runtime image's Decisions protocol. "
            "Run 'civ-agent image build', then 'civ-agent auth restart'."
        ) from None
    expected = version_contract()
    if (
        not isinstance(actual, dict)
        or set(actual) != set(expected)
        or any(actual[field] != value or type(actual[field]) is not type(value) for field, value in expected.items())
    ):
        raise _decisions_preflight_error("Selected runtime image is incompatible", health_failure(actual))


def ensure_decisions(root, image, *, restart=False):
    private_directory(root / "decisions-ipc")
    with locked(root / ".decisions-launch.lock", wait=True):
        name = auth_name(root) + "-decisions"
        current = inspected(name)
        if restart and current:
            if current["State"]["Running"]:
                podman("stop", "--time", "90", name, timeout=100)
            podman("rm", name)
            current = None
        if current and current["State"]["Running"] and current.get("Image") != image:
            raise ContainerError(
                "The shared Decisions broker uses a different runtime image. "
                "Rebuild with 'civ-agent image build' and replace it with 'civ-agent auth restart'."
            )
        if not current or not current["State"]["Running"]:
            if current:
                podman("rm", name)
            (root / "decisions-ready.json").unlink(missing_ok=True)
            arguments = base_arguments(name, game_id="auth", resources=ContainerResources(memory_mib=512, cpus=0.5))
            endpoint = os.environ.get("CIV_JEV_ENDPOINT", "http://169.254.1.3:8091/v1/systemone")
            podman(
                *arguments,
                "--network=pasta:-4,--map-host-loopback,169.254.1.3",
                "--env",
                "CIV_JEV_ENDPOINT=" + endpoint,
                *mount(root, "/auth", writable=True),
                image,
                "decisions",
            )
            podman("start", name)
        deadline = time.monotonic() + 30
        while not (root / "decisions-ready.json").exists():
            current = inspected(name)
            if not current or not current["State"]["Running"] or time.monotonic() > deadline:
                raise ContainerError("Decisions broker failed to start")
            time.sleep(0.2)
        # The ready file is not evidence that the player socket is reachable or
        # speaks this protocol. Authenticate with a trusted operator credential.
        authorize_agent(root, "operator", "health", root / "operator", decisions=True)
        health = routing_broker_health(root, timeout=5)
        if health["status"] != "ready":
            raise _decisions_preflight_error("Decisions broker preflight failed", health)
        report = {**health, "checkedAtMs": int(time.time() * 1000), "image": image}
        write_private_json(root / "decisions-preflight.json", report)
        return report


def restart_brokers(root, image):
    """Recreate configured shared services without replacing their durable state."""
    name = auth_name(root)
    auth = (root / "codex/auth.json").is_file()
    decisions = (
        (root / "decisions-clients.json").is_file()
        or (root / "decisions.key").is_file()
        or bool(inspected(name + "-decisions"))
    )
    # Check both services before interrupting either one. A missing credential
    # must not silently skip a previously configured, still-running broker.
    if not auth and inspected(name):
        raise ContainerError("Login credentials missing; restore AUTH_DIR/codex/auth.json before restarting")
    if not auth and not decisions:
        raise ContainerError("No auth or Decisions credentials configured in this auth directory")
    luna_configured = (root / "decisions.key").exists()
    if not luna_configured and (root / "decisions.sqlite3").is_file():
        from civ_agent.decisions_ledger import DecisionsLedger, accounts

        ledger = DecisionsLedger(root)
        try:
            with ledger.engine.connect() as db:
                luna_configured = luna_configured or any(
                    json.loads(row[0]).get("decisions_backend", "luna") == "luna"
                    for row in db.execute(accounts.select().with_only_columns(accounts.c.settings))
                )
        finally:
            ledger.engine.dispose()
    if luna_configured:
        from civ_agent.decisions_broker import read_decisions_key

        read_decisions_key(root / "decisions.key")
    private_directory(root)
    for directory, configured in (("ipc", auth), ("decisions-ipc", decisions)):
        if configured:
            private_directory(root / directory)
    if decisions:
        _check_decisions_image(image)
    result: JsonObject = {
        "auth_directory": str(root),
        "image": image,
        "auth": {"status": "not_configured"},
        "decisions": {"status": "not_configured"},
    }
    if auth:
        result["auth"] = {"status": "ready", "container": ensure_auth(root, image, restart=True)}
    if decisions:
        result["decisions"] = {
            "status": "ready",
            "container": name + "-decisions",
            "health": ensure_decisions(root, image, restart=True),
        }
    return result


def routing_broker_health(root, *, timeout=1):
    """Inspect the existing broker without starting it or calling the provider."""
    from civ_agent.decisions_broker import RoutingClient

    return asyncio.run(
        RoutingClient(root / "decisions-ipc/broker.sock", root / "operator/decisions-client.json").health(
            timeout=timeout
        )
    )


def decisions_arguments(auth_root, name, *, backend="jev"):
    return [
        *mount(auth_root / "decisions-ipc", "/decisions-ipc"),
        "--env",
        "CIV_DECISIONS_SOCKET=/decisions-ipc/broker.sock",
        "--env",
        f"CIV_DECISIONS_TOKEN_FILE=/match/agent-services/{name}/decisions-client.json",
        "--env",
        f"CIV_DECISIONS_BACKEND={backend}",
    ]


def authorize_agent(auth_root, game_id, seat, directory, *, decisions=False, settings=None, player=None):
    path = directory / ("decisions-client.json" if decisions else "auth-client.json")
    if not path.exists():
        write_private_json(path, {"token": secrets.token_urlsafe(32)})
    token = read_json(path)["token"]
    with locked(auth_root / ".clients.lock", wait=True):
        registry = auth_root / ("decisions-clients.json" if decisions else "clients.json")
        clients = read_json(registry, {})
        clients[f"{game_id}-{seat}"] = hashlib.sha256(token.encode()).hexdigest()
        if decisions and settings is not None:
            from civ_agent.decisions_ledger import DecisionsLedger

            DecisionsLedger(auth_root).register(f"{game_id}-{seat}", game_id, player or str(seat), settings)
        write_private_json(registry, clients)


def validate_setup(config, image):
    # Validate with the same packaged engine used for gameplay, without model credentials.
    result = podman(
        "run",
        "--rm",
        "--network=none",
        "--userns=keep-id",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=512m,mode=1777",
        "--memory=4g",
        "--pids-limit=512",
        image,
        "validate",
        *config.engine_arguments(),
        timeout=300,
    )
    lines = [
        line.removeprefix("SETUP-READY:") for line in result.stdout.splitlines() if line.startswith("SETUP-READY:")
    ]
    if len(lines) != 1:
        raise ContainerError("Native setup validation did not return a result")
    return json.loads(lines[0])


class GameRun:
    def __init__(self, root, stop_event=None):
        self.root = Path(root)
        self.stop_event = stop_event or threading.Event()
        self.config = ResolvedGameConfig.model_validate(read_json(root / "resolved.json"))
        self.record = identity(root)
        self.game_id = self.record["id"]
        self.prefix = "civ-" + self.game_id
        self.names = []
        self.log_processes = []
        self.log_files = []
        self.log_paths = {}
        self.auth_root = Path(self.record["auth_root"]).resolve()
        if self.auth_root.is_relative_to(self.root.resolve()):
            raise ContainerError("The provider credential directory must be outside the match directory")
        self.controller = self.prefix + "-host"
        self.native_failure = None

    def status(self, status, **extra):
        write_private_json(
            self.root / "container-status.json",
            {
                "id": self.game_id,
                "status": status,
                "updated_at": time.time(),
                **extra,
            },
        )

    def start(self, arguments, name, image, *entry):
        podman(*arguments, image, *entry)
        self.names.append(name)
        podman("start", name)
        path = self.launch_dir / (name + ".log")
        log = path.open("a")
        self.log_files.append(log)
        self.log_paths[name] = path
        self.log_processes.append(subprocess.Popen(["podman", "logs", "--follow", name], stdout=log, stderr=log))

    def await_ready(self, predicate):
        deadline = time.monotonic() + self.config.limits.startup_timeout
        while not predicate():
            if self.stop_event.is_set():
                raise InterruptedError("Operator requested stop")
            self.check_services()
            if time.monotonic() > deadline:
                raise ContainerError("Container startup timed out; inspect launch logs")
            time.sleep(0.2)

    def check_services(self):
        if not self.names:
            return []
        records = json.loads(podman("container", "inspect", *self.names).stdout)
        for record in records:
            if not record["State"]["Running"]:
                self.native_failure = {"container": record["Name"], "state": record["State"]}
                raise ContainerError(
                    f"{record['Name']} exited: {record['State'].get('ExitCode')}; "
                    f"OOM killed: {record['State'].get('OOMKilled', False)}"
                )
        return records

    def setup(self):
        validate_routing_credentials(self.config, self.auth_root)
        if self.config.agents.decisions_enabled:
            from civ_agent.decisions_ledger import DecisionsLedger

            authority = DecisionsLedger(self.auth_root).authority
            if self.record.get("decisionsAuthority", authority) != authority:
                raise ContainerError("Decisions accounting authority differs from this game's pinned ledger")
            self.record["decisionsAuthority"] = authority
        cleanup(self.game_id)
        self.record["launch"] = secrets.token_hex(6)
        write_private_json(self.root / "container.json", self.record)
        self.launch_dir = private_directory(self.root / "launches" / self.record["launch"])
        private_directory(self.root / "control")
        for name in ("start.json", "request.json"):
            (self.root / "control" / name).unlink(missing_ok=True)
        (self.root / "runtime.json").unlink(missing_ok=True)
        providers = set().union(*(player_providers(self.config, p) for p in self.config.agent_players))
        if self.config.agents.decisions_enabled:
            self.record["routingPreflight"] = ensure_decisions(self.auth_root, self.record["images"]["runtime"])
            write_private_json(self.root / "container.json", self.record)
        if "openai" in providers:
            ensure_auth(self.auth_root, self.record["images"]["runtime"])
        if "openrouter" in providers:
            key = self.auth_root / "openrouter.key"
            if not key.is_file():
                raise ContainerError(
                    "Run civ-agent auth login --provider openrouter before launching OpenRouter players"
                )
            read_api_key(key)
        self.control_net = self.prefix + "-control"
        control = create_network(self.control_net, "cc" + self.game_id, internal=True)
        # A dedicated interface carries the browser; it never listens on player networks.
        from ipaddress import ip_network

        control_ip = str(ip_network(control["subnets"][0]["subnet"])[2])
        networks, bridges = [], []
        for index, player in enumerate(self.config.agent_players):
            network = f"{self.prefix}-p{index}"
            bridge = f"ca{self.game_id[:8]}{index}"
            create_network(network, bridge)
            networks.append(network)
            bridges.append(bridge)
            name = civilization_directory_name(player.civilization)
            directory = private_directory(self.root / "agent-services" / name)
            private_directory(self.root / "agents" / name)
            codex_home = private_directory(self.root / "codex" / name)
            # No personal home configuration, skills, login cache or history is imported.
            from civ_agent.tls import write_private

            write_private(codex_home / "config.toml", b'cli_auth_credentials_store = "ephemeral"\n')
            if "openai" in player_providers(self.config, player):
                authorize_agent(self.auth_root, self.game_id, index, directory)
            if self.config.agents.decisions_enabled:
                from civ_agent.decisions_policy import decisions_values

                authorize_agent(
                    self.auth_root,
                    self.game_id,
                    index,
                    directory,
                    decisions=True,
                    settings=decisions_values(self.config.agents),
                    player=player.civilization,
                )
        firewall(self.game_id, bridges)
        resources = self.config.resources.desktop if self.config.human else self.config.resources.controller
        args = base_arguments(self.controller, game_id=self.game_id, resources=resources)
        args += [
            "--network",
            f"{self.control_net}:ip={control_ip},alias=game",
            "--sysctl",
            "net.ipv4.ip_forward=0",
            *mount(self.root, "/match", writable=True),
        ]
        for network in networks:
            args += ["--network", f"{network}:alias=game"]
        if self.config.human:
            args += [
                "--publish",
                f"127.0.0.1:{self.config.runtime.browser_port or ''}:6080",
                "--env",
                f"CIV_BROWSER_BIND={control_ip}",
            ]
        image = self.record["images"]["desktop" if self.config.human else "runtime"]
        self.start(args, self.controller, image, "controller")
        self.await_ready(lambda: read_json(self.root / "runtime.json", {}).get("phase") == "host_ready")
        if self.config.human:
            port = podman("port", self.controller, "6080/tcp").stdout.strip().rsplit(":", 1)[1]
            write_private_json(
                self.root / "browser.json",
                {"url": f"http://127.0.0.1:{port}/vnc.html?resize=scale", "password_file": "desktop/password.txt"},
            )
        for index, (player, network) in enumerate(zip(self.config.agent_players, networks, strict=True)):
            name = civilization_directory_name(player.civilization)
            container = f"{self.prefix}-p{index}"
            args = base_arguments(container, game_id=self.game_id, resources=self.config.resources.agent)
            args += ["--network", f"{network}:alias=p{index}"]
            for folder in ("agent-services", "agents", "codex"):
                args += mount(self.root / folder / name, f"/match/{folder}/{name}", writable=True)
            for provider in sorted(player_providers(self.config, player)):
                args += agent_auth_arguments(self.auth_root, name, provider)
            if self.config.agents.decisions_enabled:
                args += decisions_arguments(self.auth_root, name, backend=self.config.agents.decisions_backend)
            args += [
                "--env",
                f"HOME=/match/codex/{name}",
                "--env",
                f"CODEX_HOME=/match/codex/{name}",
            ]
            self.start(args, container, self.record["images"]["runtime"], "agent", player.civilization)
        for container in self.names[1:]:
            with self.log_paths[container].open() as log:
                self.await_ready(lambda: any(line.startswith("AGENT-READY:") for line in log.readlines()))
        write_private_json(self.root / "control/start.json", {"launch": self.record["launch"]})

    def stop(self):
        if self.controller in self.names:
            write_private_json(self.root / "control/request.json", {"action": "stop"})
            # Scheduler first: it cancels/quiesces model work and flushes accounting.
            podman("stop", "--time", "90", self.controller, timeout=100, check=False)
        stop_containers([name for name in self.names if name != self.controller])
        for process in self.log_processes:
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=3)
            if process.poll() is None:
                process.terminate()
                process.wait()
        for log in self.log_files:
            log.close()

    def run_once(self):
        status, error = "failed", None
        self.names, self.log_processes, self.log_files, self.log_paths = [], [], [], {}
        self.native_failure = None
        self.status("starting")
        try:
            self.setup()
            self.status("running", browser=read_json(self.root / "browser.json"))
            sampled = 0.0
            while not self.stop_event.wait(1):
                runtime = read_json(self.root / "runtime.json", {})
                if runtime.get("phase") == "stopped":
                    status = {
                        "complete": "completed",
                        "budget_exhausted": "budget_exhausted",
                        "paused": "paused",
                        "stopped": "paused",
                    }.get(runtime.get("runner_status"), "failed")
                    error = runtime.get("reason")
                    break
                self.check_services()
                if time.monotonic() - sampled > 10:
                    result = podman("stats", "--no-stream", "--format", "json", *self.names, check=False)
                    if result.returncode == 0:
                        sample = {
                            "at": time.time(),
                            "launch": self.record["launch"],
                            "containers": json.loads(result.stdout),
                        }
                        with (self.root / "resources.jsonl").open("a") as stream:
                            stream.write(json.dumps(sample) + "\n")
                        write_private_json(self.root / "resources.json", sample)
                    sampled = time.monotonic()
            else:
                status = "paused"
        except InterruptedError:
            status = "paused"
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as failure:
            error = str(failure)
        finally:
            try:
                self.stop()
            except (OSError, RuntimeError, subprocess.SubprocessError) as failure:
                status, error = "failed", f"{error or ''} Cleanup failed: {failure}".strip()
            self.status(
                status, reason=error, failure=self.native_failure, browser=read_json(self.root / "browser.json")
            )
        return status

    def run(self):
        with locked(self.root / ".supervisor.lock"):
            retries = 0
            while True:
                status = self.run_once()
                reason = recoverable_model_failure(self.root) if status == "paused" else None
                if reason is None:
                    return status
                if retries >= self.config.limits.model_capacity_retries:
                    self.status(
                        "paused",
                        reason=f"Model capacity remained unavailable after {retries} automatic retries.",
                        recovery={"reason": reason, "attempts": retries, "exhausted": True},
                    )
                    return status
                retries += 1
                delay = self.config.limits.model_capacity_retry_delay
                self.status(
                    "retrying",
                    reason=f"Model capacity is unavailable; retrying in {delay} seconds.",
                    recovery={
                        "reason": reason,
                        "attempt": retries,
                        "maximum": self.config.limits.model_capacity_retries,
                    },
                )
                if self.stop_event.wait(delay):
                    self.status("paused", reason="Operator stopped automatic model-capacity recovery.")
                    return "paused"


BATCH_SUMMARY_INTERVAL = 10.0


def _previous_batch_results(root):
    """Best-effort fallback from derived output, including summaries predating freshness fields."""
    try:
        summary = read_json(root / "results.json", {})
    except (OSError, ValueError):
        return {}
    if not isinstance(summary, dict) or not isinstance(summary.get("runs", []), list):
        return {}
    return {
        row["id"]: {"result_updated_at": summary.get("updated_at"), **row}
        for row in summary.get("runs", [])
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }


def batch_summary(root, manifest):
    rows = []
    previous = None
    for run in manifest["runs"]:
        directory = root / "games" / run["id"]
        saved = read_json(directory / "resolved.json")
        if saved is None:
            raise ContainerError(f"Saved configuration is missing for game {run['id']}")
        config = ResolvedGameConfig.model_validate(saved)
        path = path_for(directory)
        result_stale = False
        try:
            available = path.exists()
            result = read_record(path, "state", "result") if available else None
            result_updated_at = time.time() if result is not None else None
        except OperationalError as error:
            # SQLite's extended codes retain the primary code in the low byte.
            # Only transient read contention is optional; do not hide other database failures.
            if getattr(error.orig, "sqlite_errorcode", 0) & 0xFF not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                raise
            if previous is None:
                previous = _previous_batch_results(root)
            cached = previous.get(run["id"], {})
            result = cached.get("result")
            result_updated_at = cached.get("result_updated_at")
            result_stale = True
            if not cached.get("result_stale"):
                logger.warning(
                    "Game %s monitoring database is busy: %s. Retaining the last result; "
                    "retrying at the next summary refresh. Gameplay continues.",
                    run["id"],
                    path,
                )
        rows.append(
            {
                **{k: v for k, v in run.items() if k != "config"},
                "master_seed": config.game.master_seed,
                "map_seed": config.game.map_seed,
                "starting_position_seed": config.game.starting_position_seed,
                "assignment_seed": config.game.assignment_seed,
                "players": config.player_records(),
                "result": result,
                "result_stale": result_stale,
                "result_updated_at": result_updated_at,
                "resources": config.resources.model_dump(),
                "resource_usage": read_json(directory / "resources.json"),
                "failure": read_json(directory / "container-status.json", {}).get("reason"),
            }
        )
    write_private_json(
        root / "results.json", {"parallel": manifest["parallel"], "updated_at": time.time(), "runs": rows}
    )
    with (root / "results.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=(
                "id",
                "master_seed",
                "map_seed",
                "starting_position_seed",
                "assignment_seed",
                "status",
                "players",
                "result",
                "result_stale",
                "result_updated_at",
                "failure",
            ),
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(row[key]) if isinstance(row.get(key), (dict, list)) else row.get(key)
                    for key in writer.fieldnames
                }
            )


def run_batch(root, stop_event):
    with locked(root / ".supervisor.lock"):
        manifest = read_json(root / "batch.json")
        if not isinstance(manifest, dict) or manifest.get("format") != 2:
            raise ContainerError("Unsupported batch manifest; start a new batch")
        active = {}
        last_summary = 0.0
        with concurrent.futures.ThreadPoolExecutor(max_workers=manifest["parallel"]) as pool:
            try:
                batch_summary(root, manifest)
                last_summary = time.monotonic()
                while True:
                    for future, run in list(active.items()):
                        if future.done():
                            try:
                                run["status"] = future.result()
                            except Exception as failure:
                                run["status"], run["error"] = "failed", str(failure)
                            del active[future]
                            write_private_json(root / "batch.json", manifest)
                            batch_summary(root, manifest)
                            last_summary = time.monotonic()
                    queued = [r for r in manifest["runs"] if r["status"] == "pending"]
                    started = False
                    if not stop_event.is_set():
                        for run in queued[: manifest["parallel"] - len(active)]:
                            run["status"] = "running"
                            write_private_json(root / "batch.json", manifest)
                            active[pool.submit(GameRun(root / "games" / run["id"], stop_event).run)] = run
                            started = True
                    if started or time.monotonic() - last_summary >= BATCH_SUMMARY_INTERVAL:
                        batch_summary(root, manifest)
                        last_summary = time.monotonic()
                    if not active and (stop_event.is_set() or not queued):
                        break
                    time.sleep(0.5)
            finally:
                stop_event.set()
        batch_summary(root, manifest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("game", "batch"))
    parser.add_argument("--state-dir", required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    stop_event = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop_event.set())
    if args.role == "game":
        GameRun(args.state_dir, stop_event).run()
    else:
        run_batch(args.state_dir, stop_event)


if __name__ == "__main__":
    main()
