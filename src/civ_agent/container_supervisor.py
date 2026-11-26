"""Trusted background supervision of independent, disposable game containers."""

import argparse
import asyncio
import concurrent.futures
import contextlib
import csv
import hashlib
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from civ_agent.container_identity import deployment_identity, require_local_runtime
from civ_agent.container_support import (
    LABEL,
    SERVICE_LABEL,
    ContainerError,
    ContainerService,
    auth_name,
    base_arguments,
    cleanup,
    command,
    create_network,
    firewall,
    identity,
    inspected,
    locked,
    mount,
    podman,
    read_json,
    start_container_service,
    stop_container_service,
    unit_state,
    verify_container_service,
    verify_container_unit,
    wait_unit_collected,
)
from civ_agent.game_config import ContainerResources, ResolvedGameConfig
from civ_agent.protocol import JsonObject
from civ_agent.provider_credentials import read_api_key
from civ_agent.tls import private_directory, regular_file, write_private_json


def require_local_tracing_network():
    """A fixed name alone does not prove this optional worker interface is private."""
    from civ_agent.orchestration.worker_configuration import LOCAL_TRACING_NETWORK

    result = podman("network", "inspect", LOCAL_TRACING_NETWORK, check=False)
    try:
        records = json.loads(result.stdout) if result.returncode == 0 else []
        network = records[0] if len(records) == 1 else {}
        valid = network.get("name") == LOCAL_TRACING_NETWORK and network.get("internal") is True
    except (TypeError, ValueError, KeyError):
        valid = False
    if not valid:
        raise ContainerError("Local tracing requires the internal civ-local-telemetry network")


def _broker_manifest(root):
    saved = read_json(root / "container.json", {"format": 1, "services": {}})
    if saved.get("format") != 1 or not isinstance(saved.get("services", {}), dict):
        raise ContainerError("Invalid broker process-owner manifest")
    saved.setdefault("services", {})
    return saved


def _broker_process(root, image, name, arguments, entry, *, restart):
    """The shared broker's existing systemd unit is its only restart owner."""
    saved = _broker_manifest(root)
    raw = saved["services"].get(name)
    current = inspected(name)
    if restart and current is None and raw is not None:
        service = ContainerService.load(raw)
        if service.name != name or service.owner != "auth" or service.unit != name + ".service":
            raise ContainerError("Refusing to replace an unowned missing broker")
        # An explicit image change is supported, but both old and requested
        # images must be pinned descriptors before retiring any authority.
        ContainerService.load({**service.record(), "image": image})

        def require_absent():
            for target in (service.cid, service.name):
                if podman("container", "exists", target, check=False).returncode != 1:
                    raise ContainerError("Missing broker container absence could not be confirmed")

        require_absent()
        state = verify_container_unit(service, required=False)
        if state.get("LoadState") != "not-found":
            command(["systemctl", "--user", "stop", service.unit], timeout=110)
            wait_unit_collected(service.unit, reset_failed=True)
        require_absent()
        saved = _broker_manifest(root)
        if saved["services"].get(name) != raw:
            raise ContainerError("Broker process-owner descriptor changed during explicit restart")
        saved["services"].pop(name)
        write_private_json(root / "container.json", saved)
        raw = None
    if restart and current is not None:
        if raw is not None:
            stop_container_service(ContainerService.load(raw), collect=True)
        else:
            # Explicit auth restart may replace a pre-systemd broker, but never
            # silently adopt one on a normal game launch.
            if (
                current.get("Name", "").lstrip("/") != name
                or current.get("Config", {}).get("Labels", {}).get(LABEL) != "auth"
                or not any(
                    Path(item["Source"]).resolve() == root.resolve() and item["Destination"] == "/auth"
                    for item in current.get("Mounts", [])
                )
            ):
                raise ContainerError("Refusing to replace an unowned broker")
            podman("stop", "--time", "90", current["Id"], timeout=100)
        podman("rm", current["Id"])
        saved["services"].pop(name, None)
        write_private_json(root / "container.json", saved)
        raw, current = None, None
    if raw is None and current is not None:
        raise ContainerError("Existing broker has no verified systemd owner; explicit auth restart required")
    if raw is not None:
        service = ContainerService.load(raw)
        if (
            service.name != name
            or service.owner != "auth"
            or service.image.removeprefix("sha256:") != image.removeprefix("sha256:")
        ):
            raise ContainerError("Broker image or owner changed; explicit auth restart required")
        start_container_service(service)
        return service
    (root / ("ready.json" if entry == "auth" else "decisions-ready.json")).unlink(missing_ok=True)
    unit = name + ".service"
    podman(*arguments, "--label", SERVICE_LABEL + "=" + unit, image, entry)
    current = inspected(name)
    if current is None:
        raise ContainerError("Created broker could not be inspected")
    service = ContainerService.load({"name": name, "cid": current["Id"], "image": image, "owner": "auth", "unit": unit})
    saved["services"][name] = service.record()
    write_private_json(root / "container.json", saved)
    start_container_service(service, create=True)
    return service


def _await_broker(service, ready, message):
    deadline = time.monotonic() + 30
    while True:
        current, state = verify_container_service(service)
        if current["State"]["Running"] and ready.exists():
            started = datetime.fromisoformat(current["State"]["StartedAt"]).timestamp()
            if ready.stat().st_mtime >= started:
                return
        if state.get("ActiveState") not in {"active", "activating"} or time.monotonic() > deadline:
            raise ContainerError(message)
        time.sleep(0.2)


def ensure_auth(root, image, *, restart=False):
    private_directory(root)
    private_directory(root / "ipc")
    if not (root / "openai-subscription.json").is_file():
        raise ContainerError("Run civ-agent auth login before launching games")
    with locked(root / ".launch.lock", wait=True):
        name = auth_name(root)
        arguments = [
            *base_arguments(name, game_id="auth", resources=ContainerResources(memory_mib=512, cpus=0.5)),
            "--network=pasta:-4",
            *mount(root, "/auth", writable=True),
            "--env",
            "HOME=/auth",
        ]
        service = _broker_process(root, image, name, arguments, "auth", restart=restart)
        _await_broker(service, root / "ready.json", "Login manager restart allowance exhausted or startup timed out")
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
            raw = _broker_manifest(root)["services"].get(name)
            if raw is None:
                raise ContainerError(
                    "Login manager has no verified systemd owner; stop it explicitly before replacement"
                )
            stop_container_service(ContainerService.load(raw))
            current = inspected(name)
            if current is None or current["State"]["Running"]:
                raise ContainerError(f"Could not confirm login manager {name} stopped")
        (root / "ready.json").unlink(missing_ok=True)
    return {"auth_directory": str(root), "container": name, "manager_running": False}


def agent_auth_arguments(auth_root, name, provider):
    """Only the selected credential is mounted; keys never appear in Podman argv/env."""
    if provider == "google-cloud":
        from civ_agent.google_cloud_auth import (
            CONFIG_NAME,
            CONTAINER_CONFIG,
            CONTAINER_CREDENTIALS,
            CREDENTIALS_NAME,
        )

        return [
            *mount(auth_root / CONFIG_NAME, str(CONTAINER_CONFIG)),
            *mount(auth_root / CREDENTIALS_NAME, str(CONTAINER_CREDENTIALS)),
        ]
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
        endpoint = os.environ.get("CIV_JEV_ENDPOINT", "http://169.254.1.3:8091/v1/systemone")
        arguments = [
            *base_arguments(name, game_id="auth", resources=ContainerResources(memory_mib=512, cpus=0.5)),
            "--network=pasta:-4,--map-host-loopback,169.254.1.3",
            "--env",
            "CIV_JEV_ENDPOINT=" + endpoint,
            *mount(root, "/auth", writable=True),
        ]
        service = _broker_process(root, image, name, arguments, "decisions", restart=restart)
        _await_broker(
            service, root / "decisions-ready.json", "Decisions broker restart allowance exhausted or startup timed out"
        )
        # The ready file is not evidence that the player socket is reachable or
        # speaks this protocol. Authenticate with a trusted operator credential.
        authorize_agent(root, "operator", "health", root / "operator", decisions=True)
        health = routing_broker_health(root, timeout=5)
        if health["status"] != "ready":
            raise _decisions_preflight_error("Decisions broker preflight failed", health)
        running = inspected(name)
        report = {
            **health,
            "checkedAtMs": int(time.time() * 1000),
            "image": running.get("Image", image) if running else image,
            "requestedImage": image,
        }
        write_private_json(root / "decisions-preflight.json", report)
        return report


def restart_brokers(root, image):
    """Recreate configured shared services without replacing their durable state."""
    name = auth_name(root)
    auth = (root / "openai-subscription.json").is_file()
    decisions = (
        (root / "decisions-clients.json").is_file()
        or (root / "decisions.key").is_file()
        or bool(inspected(name + "-decisions"))
    )
    # Check both services before interrupting either one. A missing credential
    # must not silently skip a previously configured, still-running broker.
    if not auth and inspected(name):
        raise ContainerError("Login credentials missing; restore AUTH_DIR/openai-subscription.json before restarting")
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


def authorize_agent(
    auth_root, game_id, seat, directory, *, decisions=False, settings=None, player=None, native_player=None
):
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

            DecisionsLedger(auth_root).register(
                f"{game_id}-{seat}", game_id, player or str(seat), settings, native_player=native_player
            )
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
    def __init__(self, root, stop_event=None, *, rejoin=False):
        self.root = Path(root)
        self.stop_event = stop_event or threading.Event()
        self.config = ResolvedGameConfig.model_validate(read_json(root / "resolved.json"))
        self.record = identity(root)
        self.game_id = self.record["id"]
        self.deployment_id = require_local_runtime(self.record).id
        self.prefix = "civ-" + self.deployment_id
        self.names = []
        self.log_processes = []
        self.log_files = []
        self.log_paths = {}
        self.auth_root = Path(self.record["auth_root"]).resolve()
        if self.auth_root.is_relative_to(self.root.resolve()):
            raise ContainerError("The provider credential directory must be outside the match directory")
        self.controller = self.prefix + "-host"
        self.native_failure = None
        self.rejoin = rejoin
        self.services: list[ContainerService] = []

    def status(self, status, **extra):
        write_private_json(
            self.root / "container-status.json",
            {
                "id": self.game_id,
                "deployment_id": self.deployment_id,
                "status": status,
                "updated_at": time.time(),
                **extra,
            },
        )

    def start(self, arguments, name, image, *entry):
        unit = name + "-" + self.record["launch"] + ".service"
        podman(*arguments, "--label", SERVICE_LABEL + "=" + unit, image, *entry)
        current = inspected(name)
        if current is None:
            raise ContainerError("Created container could not be inspected")
        service = ContainerService.load(
            {"name": name, "cid": current["Id"], "image": image, "owner": self.deployment_id, "unit": unit}
        )
        self.services.append(service)
        self.record["services"] = [entry.record() for entry in self.services]
        write_private_json(self.root / "container.json", self.record)
        self.names.append(name)
        log_path = self.follow_log(name)
        start_container_service(service, create=True, log_path=log_path)

    def follow_log(self, name):
        path = self.launch_dir / (name + ".log")
        path.touch(mode=0o600, exist_ok=True)
        regular_file(path, private=True)
        self.log_paths[name] = path
        return path

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
        records = []
        stopped = []
        for service in self.services:
            record, state = verify_container_service(service)
            records.append(record)
            if state.get("ActiveState") not in {"active", "activating"}:
                stopped.append((record, state))
        terminal = self.current_runtime(records) if stopped else None
        if stopped and (
            terminal is None
            or terminal.get("runner_status") not in {"complete", "budget_exhausted", "paused", "stopped", "failed"}
        ):
            record, state = stopped[0]
            self.native_failure = {"container": record["Name"], "state": record["State"], "unit": state}
            raise ContainerError(
                f"{record['Name']} exited: {record['State'].get('ExitCode')}; "
                f"OOM killed: {record['State'].get('OOMKilled', False)}; restart allowance exhausted or service stopped"
            )
        return records

    def reattach(self):
        """Process replacement observes the same physical launch; never setup/resume."""
        raw = self.record.get("services")
        launch = self.record.get("launch")
        if not isinstance(raw, list) or not raw or not isinstance(launch, str) or len(launch) != 12:
            raise ContainerError("Supervisor replacement requires an existing owned launch")
        self.launch_dir = self.root / "launches" / launch
        if not self.launch_dir.is_dir():
            raise ContainerError("Supervisor replacement lost its original launch directory")
        self.services = [ContainerService.load(value) for value in raw]
        if len({entry.cid for entry in self.services}) != len(self.services) or len(
            {entry.unit for entry in self.services}
        ) != len(self.services):
            raise ContainerError("Ambiguous supervisor process ownership")
        for service in self.services:
            if service.owner != self.deployment_id or service.unit != service.name + "-" + launch + ".service":
                raise ContainerError("Supervisor replacement owner or launch changed")
            self.names.append(service.name)
        if self.record.get("servicesReady") is not True:
            raise ContainerError(
                "Supervisor startup was interrupted before all services were ready; explicit resume required"
            )
        for service in self.services:
            current, state = verify_container_service(service, unit_required=False)
            terminal = self.current_runtime([current])
            if state.get("ActiveState") not in {"active", "activating"} and terminal is None:
                raise ContainerError("Owned container service stopped or exhausted its restart allowance")
            self.follow_log(service.name)

    def current_runtime(self, records):
        """Reject a previous container process's retained terminal projection."""
        try:
            with (self.root / "runtime.json").open("rb") as stream:
                runtime = json.load(stream)
                written_at = os.fstat(stream.fileno()).st_mtime
        except FileNotFoundError:
            return None
        if runtime.get("phase") != "stopped" or runtime.get("launch") != self.record.get("launch"):
            return None
        controller = next((value for value in records if value.get("Name", "").lstrip("/") == self.controller), None)
        if controller is None:
            controller = next((inspected(value.cid) for value in self.services if value.name == self.controller), None)
        if controller is None:
            return None
        service = next(value for value in self.services if value.name == self.controller)
        if unit_state(service.unit).get("ActiveState") == "activating":
            return None
        try:
            started = datetime.fromisoformat(controller["State"]["StartedAt"]).timestamp()
        except (KeyError, TypeError, ValueError):
            raise ContainerError("Controller process start time could not be verified") from None
        if written_at < started:
            return None
        return runtime

    def setup(self):
        from ipaddress import ip_network

        from civ_agent.player_paths import PlayerPaths

        require_local_runtime(self.record)
        resume = self.record.get("launchResumeRequested", False)
        if type(resume) is not bool:
            raise ContainerError("Explicit launch intent is missing")
        if self.config.runtime.tracing is not None and self.config.runtime.tracing.local:
            require_local_tracing_network()
        validate_routing_credentials(self.config, self.auth_root)
        if self.config.agents.decisions_enabled:
            from civ_agent.decisions_ledger import DecisionsLedger

            authority = DecisionsLedger(self.auth_root).authority
            if self.record.get("decisionsAuthority", authority) != authority:
                raise ContainerError("Decisions accounting authority differs from this game's pinned ledger")
            self.record["decisionsAuthority"] = authority
        cleanup(self.deployment_id)
        self.record.update(
            launch=secrets.token_hex(6),
            services=[],
            servicesReady=False,
            launchStartedAtMs=int(time.time() * 1000),
            launchResumeRequested=resume,
        )
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
        if not self.config.runtime.scripted_models and "openai" in providers:
            ensure_auth(self.auth_root, self.record["images"]["runtime"])
        if not self.config.runtime.scripted_models and "openrouter" in providers:
            key = self.auth_root / "openrouter.key"
            if not key.is_file():
                raise ContainerError(
                    "Run civ-agent auth login --provider openrouter before launching OpenRouter players"
                )
            read_api_key(key)
        if not self.config.runtime.scripted_models and "google-cloud" in providers:
            from civ_agent.google_cloud_auth import CONFIG_NAME, saved_config

            if not (self.auth_root / CONFIG_NAME).is_file():
                raise ContainerError(
                    "Run civ-agent auth login --provider google-cloud --project PROJECT_ID before launching Gemini players"
                )
            saved_config(self.auth_root)
        for index, player in enumerate(self.config.agent_players):
            paths = PlayerPaths(self.root, index, player.civilization)
            paths.prepare()
            if not self.config.runtime.scripted_models and "openai" in player_providers(self.config, player):
                authorize_agent(self.auth_root, self.game_id, index, paths.security)
            if self.config.agents.decisions_enabled:
                from civ_agent.decisions_policy import decisions_values

                authorize_agent(
                    self.auth_root,
                    self.game_id,
                    index,
                    paths.coordinator_security,
                    decisions=True,
                    settings=decisions_values(self.config.agents),
                    player=player.civilization,
                    native_player=paths.player,
                )
        self.control_net = self.prefix + "-control"
        control = create_network(self.control_net, "cc" + self.deployment_id, internal=True)
        control_ip = str(ip_network(control["subnets"][0]["subnet"])[-2])
        provider_net = self.prefix + "-provider"
        provider_bridge = "ca" + self.deployment_id
        create_network(provider_net, provider_bridge)
        firewall(self.deployment_id, [provider_bridge])
        resources = self.config.resources.desktop if self.config.human else self.config.resources.controller
        args = base_arguments(self.controller, game_id=self.deployment_id, resources=resources)
        args += [
            "--network",
            f"{self.control_net}:ip={control_ip},alias=game",
            "--network",
            provider_net,
            "--sysctl",
            "net.ipv4.ip_forward=0",
            *mount(self.root, "/match", writable=True),
        ]
        if not self.config.runtime.scripted_models:
            if "openai" in providers:
                args += mount(self.auth_root / "ipc", "/auth-ipc")
            if "openrouter" in providers:
                args += mount(self.auth_root / "openrouter.key", "/run/secrets/openrouter.key")
            if "google-cloud" in providers:
                args += agent_auth_arguments(self.auth_root, self.controller, "google-cloud")
        if self.config.agents.decisions_enabled:
            args += mount(self.auth_root / "decisions-ipc", "/advisory-ipc")
        if self.config.runtime.tracing is not None:
            from civ_agent.orchestration.worker_configuration import LOCAL_TRACING_NETWORK, TRACING_TOKEN_PATH

            if self.config.runtime.tracing.local:
                args += ["--network", LOCAL_TRACING_NETWORK]
            elif self.config.runtime.tracing.token_file is not None:
                regular_file(self.config.runtime.tracing.token_file, private=True)
                args += mount(self.config.runtime.tracing.token_file, str(TRACING_TOKEN_PATH))
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
        write_private_json(self.root / "control/start.json", {"launch": self.record["launch"]})
        self.record["servicesReady"] = True
        write_private_json(self.root / "container.json", self.record)

    def stop(self):
        errors = []
        if self.controller in self.names:
            write_private_json(self.root / "control/request.json", {"action": "stop"})
            # The controller fences active roles and saves its paused state before exiting.
            service = next(value for value in self.services if value.name == self.controller)
            try:
                stop_container_service(service)
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                errors.append(error)
        for service in reversed(self.services):
            if service.name == self.controller:
                continue
            try:
                stop_container_service(service)
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                errors.append(error)
        for process in self.log_processes:
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=3)
            if process.poll() is None:
                process.terminate()
                process.wait()
        for log in self.log_files:
            log.close()
        if errors:
            raise ContainerError("Container service shutdown failed") from ExceptionGroup("Shutdown failures", errors)

    def run_once(self):
        status, error = "failed", None
        self.names, self.log_processes, self.log_files, self.log_paths = [], [], [], {}
        self.services = []
        self.native_failure = None
        self.status("starting")
        try:
            if self.rejoin:
                self.reattach()
            else:
                self.setup()
            self.status("running", browser=read_json(self.root / "browser.json"))
            sampled = 0.0
            while not self.stop_event.wait(1):
                records = self.check_services()
                runtime = self.current_runtime(records)
                if runtime is not None:
                    status = {
                        "complete": "completed",
                        "budget_exhausted": "budget_exhausted",
                        "paused": "paused",
                        "stopped": "paused",
                    }.get(runtime.get("runner_status"), "failed")
                    error = runtime.get("reason")
                    break
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
            return self.run_once()


BATCH_SUMMARY_INTERVAL = 10.0


def batch_summary(root, manifest):
    from civ_agent.operator_state import is_local, projection

    rows = []
    for run in manifest["runs"]:
        directory = root / "games" / run["id"]
        saved = read_json(directory / "resolved.json")
        if saved is None:
            raise ContainerError(f"Saved configuration is missing for game {run['id']}")
        config = ResolvedGameConfig.model_validate(saved)
        observed = projection(directory)
        rows.append(
            {
                **{k: v for k, v in run.items() if k != "config"},
                "master_seed": config.game.master_seed,
                "map_seed": config.game.map_seed,
                "starting_position_seed": config.game.starting_position_seed,
                "assignment_seed": config.game.assignment_seed,
                "players": config.player_records(),
                "runtime_status": observed.status.model_dump(mode="json") if observed.status is not None else None,
                "runtime_result": observed.completion.model_dump(mode="json")
                if observed.completion is not None
                else None,
                "read_only_archive": not is_local(directory),
                "result": observed.result.model_dump(mode="json") if observed.result is not None else None,
                "result_stale": False,
                "result_updated_at": observed.result_updated_at,
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


def run_batch(root, stop_event, *, rejoin=False):
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
                    active_ids = {run["id"] for run in active.values()}
                    queued = [
                        r
                        for r in manifest["runs"]
                        if r["id"] not in active_ids
                        and (r["status"] == "pending" or (rejoin and r["status"] == "running"))
                    ]
                    started = False
                    if not stop_event.is_set():
                        for run in queued[: manifest["parallel"] - len(active)]:
                            returning = rejoin and run["status"] == "running"
                            run["status"] = "running"
                            write_private_json(root / "batch.json", manifest)
                            game = (
                                GameRun(root / "games" / run["id"], stop_event, rejoin=True)
                                if returning
                                else GameRun(root / "games" / run["id"], stop_event)
                            )
                            active[pool.submit(game.run)] = run
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
    try:
        record = identity(args.state_dir)
        unit = os.environ.get("CIV_SUPERVISOR_UNIT")
        rejoin = False
        if unit is not None:
            expected = "civ-" + args.role + "-" + deployment_identity(record).id
            if unit != expected or record.get("supervision") != {
                "backend": "systemd-existing-cid-v1",
                "unit": expected,
            }:
                raise ContainerError("Supervisor process owner does not match this deployment")
            state = unit_state(unit)
            rejoin = int(state.get("NRestarts", "0")) > 0
        if rejoin and read_json(args.state_dir / "control/request.json", {}).get("action") == "stop":
            stop_event.set()
        if args.role == "game":
            result = GameRun(args.state_dir, stop_event, rejoin=rejoin).run()
            return 78 if result == "failed" else 0
        run_batch(args.state_dir, stop_event, rejoin=rejoin)
        return 78 if any(run["status"] == "failed" for run in read_json(args.state_dir / "batch.json")["runs"]) else 0
    except ContainerError as error:
        print("Supervisor refused continuation: " + str(error), file=sys.stderr)
        return 78
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        # A handled configuration/ownership/integrity failure is a permanent
        # exit. An abrupt supervisor death remains systemd's bounded retry.
        print("Supervisor configuration or I/O failed: " + type(error).__name__, file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
