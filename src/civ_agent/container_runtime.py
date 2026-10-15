"""Entrypoints inside disposable containers; all authoritative state is bind-mounted."""

import argparse
import hashlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from civ_agent.game_config import ResolvedGameConfig
from civ_agent.tls import (
    ensure_host_tls,
    ensure_server_certificate,
    private_directory,
    write_private,
    write_private_json,
)
from civ_agent.turn_archive import civilization_directory_name

ROOT = Path("/match")
ASSETS = Path("/opt/unciv/engine/android/assets")


def native(arguments, assets=ASSETS):
    return [
        "java",
        "-XX:MaxRAMPercentage=65",
        "-Djava.awt.headless=true",
        f"-Duncivai.assetsDir={assets}",
        "-cp",
        "/opt/unciv/lib/*",
        "uncivai.MainKt",
        *arguments,
    ]


def prepare(config):
    from civ_agent.commands.host import ensure_players

    private_directory(ROOT)
    private_directory(ROOT / "control")
    resume = (ROOT / "exploration.json").exists()
    controlled = ([config.human] if config.human else []) + [p.civilization for p in config.agent_players]
    ensure_players(ROOT / "players.json", resume, controlled, [config.human] if config.human else [])
    operator = ROOT / "operator.json"
    if not operator.exists():
        if resume:
            raise ValueError("Restore the original operator credentials before resuming")
        write_private_json(operator, {"token": secrets.token_urlsafe(32)})
    tls, ca = ensure_host_tls(ROOT, "0.0.0.0", "game")
    write_private_json(ROOT / "endpoints.json", {"hostUrl": "https://game:8765", "caFile": str(ca)})
    for index, player in enumerate(config.agent_players):
        name = civilization_directory_name(player.civilization)
        directory = private_directory(ROOT / "agent-services" / name)
        security = private_directory(directory / "security")
        material = ensure_server_certificate(ROOT, f"p{index}", [f"p{index}", "localhost", "127.0.0.1"])
        for key, target in (("cert_file", "cert.pem"), ("key_file", "key.pem"), ("ca_file", "ca.pem")):
            write_private(security / target, material[key].read_bytes())
        if not (directory / "token.json").exists():
            write_private_json(directory / "token.json", {"token": secrets.token_urlsafe(32)})
        write_private_json(
            directory / "container-settings.json",
            {
                "civilization": player.civilization,
                "player": player.name,
                "hostname": f"p{index}",
                "settings": config.settings(player),
            },
        )
    return tls


def stop(process):
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def wait_for(predicate, process, timeout):
    deadline = time.monotonic() + timeout
    while not predicate():
        if process.poll() is not None:
            raise RuntimeError(f"Native service exited with code {process.returncode}")
        if time.monotonic() > deadline:
            raise TimeoutError("Service startup timed out")
        time.sleep(0.2)


def desktop(config, processes, logs):
    """Stream only the game window; no host display, desktop shell or file server."""
    os.environ["DISPLAY"] = ":99"
    path = ROOT / "desktop"
    private_directory(path)
    secret_path = path / "password.txt"
    if not secret_path.exists():
        # Standard VNC authentication uses exactly eight password characters.
        write_private(
            secret_path,
            (
                "".join(
                    secrets.choice("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") for _ in range(8)
                )
                + "\n"
            ).encode(),
        )
    password = secret_path.read_text().strip()
    subprocess.run(
        ["x11vnc", "-storepasswd", password, str(path / "vnc.pass")],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    (path / "vnc.pass").chmod(0o600)
    arguments = [
        ["Xvfb", ":99", "-screen", "0", "1280x800x24", "-nolisten", "tcp", "-ac"],
        [
            "x11vnc",
            "-display",
            ":99",
            "-rfbauth",
            str(path / "vnc.pass"),
            "-localhost",
            "-rfbport",
            "5900",
            "-forever",
            "-shared",
            "-noxdamage",
            "-quiet",
        ],
        ["websockify", "--web", "/usr/share/novnc", f"{os.environ['CIV_BROWSER_BIND']}:6080", "localhost:5900"],
    ]
    for index, argv in enumerate(arguments):
        log = (path / f"service-{index}.log").open("a")
        logs.append(log)
        process = subprocess.Popen(argv, stdout=log, stderr=log)
        processes.append(process)
        if index == 0:
            wait_for(lambda: Path("/tmp/.X11-unix/X99").exists(), process, 20)


def controller():
    from civ_agent import scheduler
    from civ_agent.monitoring import path_for, read_record

    config = ResolvedGameConfig.model_validate_json((ROOT / "resolved.json").read_text())
    manifest = json.loads((ROOT / "container.json").read_text())
    provenance = json.loads(Path("/opt/unciv/provenance.json").read_text())
    os.environ["UNCIV_HOST_REVISION"] = provenance["source"]["revision"]
    os.environ["UNCIV_ENGINE_REVISION"] = provenance["engine"]["revision"]
    processes, logs = [], []
    original_handlers = {}

    def interrupted(*_):
        raise KeyboardInterrupt

    for sig in (signal.SIGINT, signal.SIGTERM):
        original_handlers[sig] = signal.signal(sig, interrupted)
    try:
        tls = prepare(config)
        assets = ASSETS
        if config.human:
            # Unciv's desktop packs texture atlases beside its assets. Keep this
            # derived cache writable and tied to the exact selected image.
            cache_id = hashlib.sha256(manifest["images"]["desktop"].encode()).hexdigest()[:12]
            engine = ROOT / "desktop" / ("engine-" + cache_id)
            if not engine.exists():
                staging = engine.with_name(engine.name + ".copy")
                if staging.exists():
                    shutil.rmtree(staging)
                shutil.copytree(ASSETS.parents[1], staging)
                staging.rename(engine)
            assets = engine / "android/assets"
            desktop(config, processes, logs)
        arguments = [
            "--data-dir",
            str(ROOT / "data"),
            "--mcp-config",
            str(ROOT / "players.json"),
            "--operator-config",
            str(ROOT / "operator.json"),
            "--tls-config",
            str(tls),
            "--port",
            "8765",
        ]
        if (ROOT / "exploration.json").exists():
            arguments += ["--resume-checkpoint", str(ROOT / "exploration.json")]
        else:
            arguments += ["--new-game", "--checkpoint", str(ROOT / "exploration.json"), *config.engine_arguments()]
        if config.human:
            arguments += ["--human", config.human]
        log = (ROOT / "host.log").open("a")
        logs.append(log)
        offset = log.tell()
        process = subprocess.Popen(native(arguments, assets), cwd=assets, stdout=log, stderr=log)
        processes.append(process)
        with (ROOT / "host.log").open() as reader:
            reader.seek(offset)
            wait_for(
                lambda: any(line.startswith("MCP-READY:") for line in reader.readlines()),
                process,
                config.limits.startup_timeout,
            )
        write_private_json(ROOT / "runtime.json", {"phase": "host_ready", "launch": manifest["launch"]})
        wait_for(lambda: (ROOT / "control/start.json").exists(), process, config.limits.startup_timeout)
        args = scheduler.parser().parse_args(["--state-dir", str(ROOT)])
        args.action = "resume" if read_record(path_for(ROOT), "state", "run") else "start"
        # Per-seat service settings own the model choice, including opening reviews.
        args.model = args.reasoning_effort = None
        for key, value in config.limits.model_dump().items():
            if key != "startup_timeout":
                setattr(args, key, value)
        write_private_json(ROOT / "runtime.json", {"phase": "running", "launch": manifest["launch"]})
        code = scheduler.run(args)
        state = read_record(path_for(ROOT), "state", "run") or {}
        write_private_json(
            ROOT / "runtime.json",
            {
                "phase": "stopped",
                "launch": manifest["launch"],
                "exit_code": code,
                "runner_status": state.get("status"),
                "reason": state.get("reason") or (f"Scheduler exited {code}; inspect controller log" if code else None),
            },
        )
        return code
    except KeyboardInterrupt:
        return 130
    finally:
        # A second stop request must not interrupt flushing/child cleanup.
        for sig in original_handlers:
            signal.signal(sig, signal.SIG_IGN)
        for process in reversed(processes):
            stop(process)
        for log in logs:
            log.close()
        for sig, handler in original_handlers.items():
            signal.signal(sig, handler)


def agent(civilization):
    from civ_agent.commands import agent_server

    name = civilization_directory_name(civilization)
    directory = ROOT / "agent-services" / name
    launch = json.loads((directory / "container-settings.json").read_text())
    if launch["civilization"] != civilization:
        raise ValueError("Agent identity differs from its private configuration")
    security = directory / "security"
    arguments = [
        civilization,
        "--state-dir",
        str(ROOT),
        "--port",
        "8766",
        "--listen-host",
        "0.0.0.0",
        "--public-host",
        launch["hostname"],
        "--mcp-url",
        "https://game:8765",
        "--mcp-ca-file",
        str(security / "ca.pem"),
        "--cert-file",
        str(security / "cert.pem"),
        "--key-file",
        str(security / "key.pem"),
        "--ca-file",
        str(security / "ca.pem"),
    ]
    for key, value in launch["settings"].items():
        if isinstance(value, bool):
            if value:
                arguments.append("--" + key.replace("_", "-"))
        else:
            arguments += ["--" + key.replace("_", "-"), str(value)]
    return agent_server.run(agent_server.parser().parse_args(arguments))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("controller", "agent", "auth", "decisions", "watch", "validate"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    os.umask(0o077)
    Path(os.environ.get("HOME", "/tmp/home")).mkdir(parents=True, exist_ok=True)
    if args.role == "controller":
        return controller()
    if args.role == "agent":
        return agent(*args.arguments)
    if args.role == "decisions":
        from civ_agent.decisions_broker import main as decisions_main

        decisions_main()
        return 0
    if args.role == "validate":
        return subprocess.call(
            native(["--validate-setup", "--data-dir", "/tmp/validation", *args.arguments]), cwd=ASSETS
        )
    if args.role == "watch":
        from civ_agent import match_watch

        return match_watch.run(match_watch.parser().parse_args(["--state-dir", str(ROOT), *args.arguments]))
    from civ_agent.container_auth import main as auth_main

    sys.argv = [sys.argv[0]]
    auth_main()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Container failed: {error}", file=sys.stderr)
        raise SystemExit(2) from error
