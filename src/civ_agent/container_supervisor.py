"""Trusted background supervision of independent, disposable game containers."""

import argparse
import concurrent.futures
import contextlib
import csv
import hashlib
import json
import os
import secrets
import signal
import subprocess
import threading
import time
from pathlib import Path

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
from civ_agent.providers import read_api_key
from civ_agent.tls import private_directory, write_private_json
from civ_agent.turn_archive import civilization_directory_name


def ensure_auth(root, image):
    private_directory(root)
    private_directory(root / "ipc")
    if not (root / "codex/auth.json").is_file():
        raise ContainerError("Run civ-agent auth login before launching games")
    with locked(root / ".launch.lock", wait=True):
        name = auth_name(root)
        current = inspected(name)
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


def authorize_agent(auth_root, game_id, seat, directory):
    path = directory / "auth-client.json"
    if not path.exists():
        write_private_json(path, {"token": secrets.token_urlsafe(32)})
    token = read_json(path)["token"]
    with locked(auth_root / ".clients.lock", wait=True):
        clients = read_json(auth_root / "clients.json", {})
        clients[f"{game_id}-{seat}"] = hashlib.sha256(token.encode()).hexdigest()
        write_private_json(auth_root / "clients.json", clients)


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
        cleanup(self.game_id)
        self.record["launch"] = secrets.token_hex(6)
        write_private_json(self.root / "container.json", self.record)
        self.launch_dir = private_directory(self.root / "launches" / self.record["launch"])
        private_directory(self.root / "control")
        for name in ("start.json", "request.json"):
            (self.root / "control" / name).unlink(missing_ok=True)
        (self.root / "runtime.json").unlink(missing_ok=True)
        providers = {self.config.settings(p)["provider"] for p in self.config.agent_players}
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
            if self.config.settings(player)["provider"] == "openai":
                authorize_agent(self.auth_root, self.game_id, index, directory)
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
            args += agent_auth_arguments(self.auth_root, name, self.config.settings(player)["provider"])
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

    def run(self):
        with locked(self.root / ".supervisor.lock"):
            status, error = "failed", None
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
                            write_private_json(
                                self.root / "resources.json",
                                sample,
                            )
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


def batch_summary(root, manifest):
    rows = []
    for run in manifest["runs"]:
        directory = root / "games" / run["id"]
        saved = read_json(directory / "resolved.json")
        if saved is None:
            raise ContainerError(f"Saved configuration is missing for game {run['id']}")
        config = ResolvedGameConfig.model_validate(saved)
        result = read_record(path_for(directory), "state", "result") if path_for(directory).exists() else None
        rows.append(
            {
                **{k: v for k, v in run.items() if k != "config"},
                "master_seed": config.game.master_seed,
                "map_seed": config.game.map_seed,
                "starting_position_seed": config.game.starting_position_seed,
                "assignment_seed": config.game.assignment_seed,
                "players": config.player_records(),
                "result": result,
                "resources": config.resources.model_dump(),
                "resource_usage": read_json(directory / "resources.json"),
                "failure": read_json(directory / "container-status.json", {}).get("reason"),
            }
        )
    write_private_json(root / "results.json", {"parallel": manifest["parallel"], "runs": rows})
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
        with concurrent.futures.ThreadPoolExecutor(max_workers=manifest["parallel"]) as pool:
            try:
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
                    queued = [r for r in manifest["runs"] if r["status"] == "pending"]
                    if not stop_event.is_set():
                        for run in queued[: manifest["parallel"] - len(active)]:
                            run["status"] = "running"
                            write_private_json(root / "batch.json", manifest)
                            active[pool.submit(GameRun(root / "games" / run["id"], stop_event).run)] = run
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
