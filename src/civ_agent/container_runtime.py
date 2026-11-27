"""Entrypoints inside disposable containers; all authoritative state is bind-mounted."""

import argparse
import asyncio
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

from civ_agent.container_identity import require_local_runtime
from civ_agent.game_config import ResolvedGameConfig
from civ_agent.tls import (
    ensure_host_tls,
    private_directory,
    write_private,
    write_private_json,
)

ROOT = Path("/match")
ASSETS = Path("/opt/unciv/engine/android/assets")
PROVENANCE = Path("/opt/unciv/provenance.json")


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


async def run_with_native_host(controller, process):
    """An unexpectedly exited JVM must fail its owning container, not orphan it."""

    async def monitor():
        while process.poll() is None:
            await asyncio.sleep(0.2)
        raise RuntimeError(f"NATIVE_HOST_PROCESS_EXITED:{process.returncode}")

    running = asyncio.create_task(controller.run() if hasattr(controller, "run") else controller)
    watching = asyncio.create_task(monitor())
    try:
        done, _ = await asyncio.wait({running, watching}, return_when=asyncio.FIRST_COMPLETED)
        if watching in done:
            watching.result()
        return await running
    finally:
        for task in (running, watching):
            if not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.gather(running, watching, return_exceptions=True)


async def run_interruptible_controller(controller, process):
    """One signal owner cancels and joins the async controller before JVM cleanup."""
    loop = asyncio.get_running_loop()
    owner = asyncio.current_task()
    if owner is None:
        raise RuntimeError("LOCAL_CONTROLLER_TASK_REQUIRED")

    def interrupted():
        if not owner.done() and not owner.cancelling():
            owner.cancel()

    signals = (signal.SIGINT, signal.SIGTERM)
    for sig in signals:
        loop.add_signal_handler(sig, interrupted)
    try:
        return await run_with_native_host(controller, process)
    finally:
        for sig in signals:
            loop.remove_signal_handler(sig)
            # The synchronous wrapper now owns child cleanup. A second signal
            # must not interrupt its stop/join or asyncio's remaining teardown.
            signal.signal(sig, signal.SIG_IGN)


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
    from civ_agent.runtime.controller import run_controller

    config = ResolvedGameConfig.model_validate_json((ROOT / "resolved.json").read_text())
    manifest = json.loads((ROOT / "container.json").read_text())
    require_local_runtime(manifest)
    resume_requested = manifest.get("launchResumeRequested")
    if type(resume_requested) is not bool:
        raise ValueError("LOCAL_LAUNCH_INTENT_REQUIRED")
    provenance = json.loads(PROVENANCE.read_text())
    os.environ["UNCIV_HOST_REVISION"] = provenance["source"]["revision"]
    os.environ["UNCIV_ENGINE_REVISION"] = provenance["engine"]["revision"]
    if engine_hash := provenance.get("engineHash"):
        os.environ["UNCIV_ENGINE_HASH"] = engine_hash
    if image_id := manifest.get("images", {}).get("desktop" if config.human else "runtime"):
        os.environ["UNCIV_RUNTIME_IMAGE"] = image_id
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
            (ROOT / "native-runtime" / "desktop-progress.json").unlink(missing_ok=True)
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
            if config.game.scenario:
                arguments += ["--scenario", config.game.scenario]
        else:
            arguments += ["--new-game", "--checkpoint", str(ROOT / "exploration.json"), *config.engine_arguments()]
        if config.human:
            arguments += ["--human", config.human]
        if config.runtime.record_replay:
            arguments += ["--record-replay"]
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
        host_ca = Path(json.loads((ROOT / "endpoints.json").read_text())["caFile"])
        write_private_json(ROOT / "runtime.json", {"phase": "host_ready", "launch": manifest["launch"]})
        wait_for(lambda: (ROOT / "control/start.json").exists(), process, config.limits.startup_timeout)
        if json.loads((ROOT / "control/start.json").read_text()).get("launch") != manifest["launch"]:
            raise ValueError("LOCAL_LAUNCH_CONTROL_CONFLICT")
        state = asyncio.run(
            run_interruptible_controller(
                run_controller(ROOT, resume=resume_requested, host_url="https://game:8765", host_ca=host_ca),
                process,
            )
        )
        runner_status = {"FINISHED": "complete", "PAUSED": "paused", "FAILED": "failed", "STOPPED": "stopped"}.get(
            state.mode, "failed"
        )
        if state.mode == "STOPPED" and (
            (state.reason or "").startswith(("MAX_", "BUDGET"))
            or state.reason in {"MODEL_INVOCATION_LIMIT", "MODEL_REQUEST_LIMIT", "TURN_TIMEOUT"}
        ):
            runner_status = "budget_exhausted"
        write_private_json(
            ROOT / "runtime.json",
            {"phase": "stopped", "launch": manifest["launch"], "runner_status": runner_status, "reason": state.reason},
        )
        # A handled game failure is a terminal result for the supervisor. A
        # process retry would erase its original diagnostic without fixing it.
        return 0
    except (KeyboardInterrupt, asyncio.CancelledError):
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("controller", "auth", "decisions", "watch", "validate"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    os.umask(0o077)
    Path(os.environ.get("HOME", "/tmp/home")).mkdir(parents=True, exist_ok=True)
    if args.role == "controller":
        return controller()
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
