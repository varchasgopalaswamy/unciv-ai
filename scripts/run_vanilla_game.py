#!/usr/bin/env python3
"""Run the ordinary Vanilla match locally, retaining logs when it finishes or stops."""

import argparse
import contextlib
import datetime as dt
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
DEFAULT_STATE = Path.home() / ".local/state/unciv-host/vanilla-full"
LIMITS = {
    "max_turns": ("maxTurns", 331),
    "max_invocations": ("maxInvocations", 10000),
    "max_seconds": ("maxSeconds", 7 * 24 * 60 * 60),
    "turn_timeout": ("activationSeconds", 60 * 60),
}


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def read_json(path) -> dict | None:
    if not path.exists():
        return None
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")
    path.chmod(0o600)


def provenance(repository):
    try:
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repository, text=True, stderr=subprocess.DEVNULL, timeout=5
        ).strip()
        dirty = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=repository, text=True, stderr=subprocess.DEVNULL, timeout=5
            ).strip()
        )
        return {"revision": revision, "dirty": dirty}
    except (OSError, subprocess.SubprocessError):
        return None


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-dir", type=Path, default=DEFAULT_STATE, help=f"Persistent match directory (default: {DEFAULT_STATE})"
    )
    parser.add_argument("--resume", action="store_true", help="Resume the saved match and retain all previous usage")
    parser.add_argument(
        "--port", type=positive, default=8971, help="Host port; the next three ports serve agents (default: 8971)"
    )
    parser.add_argument("--seed", type=int, help="New-game terrain seed; defaults to 20260920")
    parser.add_argument(
        "--startup-timeout",
        type=positive,
        default=900,
        help="Seconds per service startup, including build (default: 900)",
    )
    parser.add_argument("--max-turns", type=positive, help="Round limit from original start; default 331")
    parser.add_argument("--max-invocations", type=positive, help="Total gameplay/chat invocation limit; default 10000")
    parser.add_argument(
        "--max-seconds", type=positive, help="Wall-time limit from original start, including pauses; default 604800"
    )
    parser.add_argument("--turn-timeout", type=positive, help="Seconds per agent turn; default 3600")
    parser.add_argument("--verbose", action="store_true", help="Also print tool activity; full history is always saved")
    args = parser.parse_args(argv)
    args.state_dir = args.state_dir.expanduser().resolve()
    if args.port > 65532:
        parser.error("--port must leave room for three consecutive agent ports (maximum 65532)")
    if args.resume and args.seed is not None:
        parser.error("--seed applies only to a new game")
    return args


def launch_command(args, python):
    root = args.state_dir
    checkpoint, runner = root / "exploration.json", root / "runner/run.json"
    previous = None
    if args.resume:
        if not checkpoint.is_file() or not runner.is_file():
            raise ValueError(
                "Resume requires exploration.json and runner/run.json; inspect the saved startup logs if either is missing."
            )
        previous = read_json(runner) or {}
        if (previous.get("model"), previous.get("reasoningEffort")) != ("gpt-5.6-luna", "medium"):
            raise ValueError("The saved match must use gpt-5.6-luna with medium reasoning.")
    elif checkpoint.exists() or runner.exists():
        raise ValueError("A saved match already exists. Use --resume or choose a new --state-dir.")
    command = [
        str(python),
        "-u",
        "-m",
        "tests.live_vanilla",
        "--state-dir",
        str(root),
        "--port",
        str(args.port),
        "--startup-timeout",
        str(args.startup_timeout),
    ]
    if args.resume:
        command.append("--resume")
    else:
        command += ["--seed", str(args.seed if args.seed is not None else 20260920)]
    for option, (saved_key, default) in LIMITS.items():
        saved = previous["limits"][saved_key] if previous else 0
        explicit = getattr(args, option)
        value = explicit if explicit is not None else max(default, saved)
        if option == "turn_timeout" and value > 86400:
            raise ValueError("--turn-timeout cannot exceed the service limit of 86400 seconds.")
        if value < saved:
            raise ValueError(f"--{option.replace('_', '-')} cannot decrease the saved limit of {saved}.")
        if option == "max_seconds" and previous and previous["startedAt"] + value <= time.time():
            raise ValueError(
                "The original wall-time budget has expired. Increase --max-seconds; it is measured from the original start, including pauses."
            )
        command += ["--" + option.replace("_", "-"), str(value)]
    if args.verbose:
        command.append("--verbose")
    return command


def run_logged(command, repository, announce):
    """Keep child services out of the terminal's signal group; let the scheduler stop them."""
    interrupted = False
    child = None

    def stop(_signum, _frame):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            if child is not None:
                with contextlib.suppress(ProcessLookupError):
                    # SIGINT also unwinds the harness's finally block during startup.
                    child.send_signal(signal.SIGINT)

    previous = {signum: signal.signal(signum, stop) for signum in (signal.SIGINT, signal.SIGTERM)}
    try:
        if interrupted:
            return 130, True
        child = subprocess.Popen(
            command,
            cwd=repository,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
            start_new_session=True,
        )
        if interrupted:
            with contextlib.suppress(ProcessLookupError):
                child.send_signal(signal.SIGINT)
        assert child.stdout is not None
        for line in child.stdout:
            announce(line, end="")
        return child.wait(), interrupted
    finally:
        if child is not None and child.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                child.send_signal(signal.SIGINT)
            try:
                child.wait(timeout=150)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        if child is not None and child.stdout is not None:
            child.stdout.close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def stopped_state(root, child_code, interrupted) -> dict:
    """A paused/blocked scheduler can exit zero; only a recorded native result is completion."""
    runner = read_json(root / "runner/run.json") or {}
    result = read_json(root / "telemetry/game-result.json") or {}
    complete = runner.get("status") == "complete" and result.get("status") == "completed" and result.get("result")
    if interrupted:
        code = 130
    elif child_code != 0:
        code = child_code if child_code > 0 else 128 - child_code
    elif complete:
        code = 0
    elif runner.get("status") == "budget_exhausted":
        code = 3
    else:
        code = 2
    return {
        "exitCode": code,
        "childExitCode": child_code,
        "interrupted": interrupted,
        "runnerStatus": runner.get("status"),
        "reason": runner.get("reason"),
        "inFlight": runner.get("inFlight"),
        "invocations": runner.get("invocations"),
        "handoffs": runner.get("handoffs"),
        "gameStatus": result.get("status"),
        "result": result.get("result"),
        "scores": result.get("scores"),
        "usage": result.get("usage"),
    }


def main(argv=None):
    args = arguments(argv)
    python = Path(sys.executable)
    try:
        if not python.with_name("civ-agent").is_file():
            raise ValueError(
                "Run with the project environment: .venv/bin/python scripts/run_vanilla_game.py (install dependencies with uv sync --locked first)."
            )
        command = launch_command(args, python)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Cannot start: {error}", file=sys.stderr)
        return 2
    os.umask(0o077)
    root = args.state_dir
    launch = root / "launches" / (dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ") + f"-{os.getpid()}")
    launch.mkdir(mode=0o700, parents=True)
    started = time.time()
    with (launch / "console.log").open("w", buffering=1) as log:
        console_available = True

        def announce(message, end="\n"):
            nonlocal console_available
            log.write(message + end)
            log.flush()
            if console_available:
                try:
                    print(message, end=end, flush=True)
                except BrokenPipeError:
                    console_available = False

        write_json(
            launch / "launch.json",
            {
                "startedAt": started,
                "command": command,
                "repository": str(REPOSITORY),
                "source": provenance(REPOSITORY),
                "engine": provenance(REPOSITORY / "vendor/Unciv"),
                "model": "gpt-5.6-luna",
                "reasoningEffort": "medium",
            },
        )
        announce(f"Match: {root}\nLaunch log: {launch / 'console.log'}")
        announce("Players: Rome, Greece, France (gpt-5.6-luna, medium), native Egypt, two city-states.")
        announce(
            "Ctrl-C pauses and flushes the run (allow a minute). A blocked turn stops for inspection; this launcher does not restart it."
        )
        announce("Command: " + shlex.join(command))
        try:
            child_code, interrupted = run_logged(command, REPOSITORY, announce)
        except Exception as error:
            announce(f"Launcher failure: {type(error).__name__}: {error}")
            child_code, interrupted = 2, False
        try:
            summary = stopped_state(root, child_code, interrupted)
        except (OSError, ValueError, TypeError) as error:
            summary = {"exitCode": 2, "childExitCode": child_code, "reason": f"Cannot read final state: {error}"}
        summary = {**summary, "startedAt": started, "endedAt": time.time(), "stateDirectory": str(root)}
        write_json(launch / "stop.json", summary)
        announce(f"Stopped: {summary.get('runnerStatus') or 'startup failure'}; exit {summary['exitCode']}.")
        if summary.get("reason"):
            announce(str(summary["reason"]))
        if summary.get("result"):
            announce("Native result: " + json.dumps(summary["result"]))
        announce(f"Stop report: {launch / 'stop.json'}\nKeep the entire match directory for analysis: {root}")
        return summary["exitCode"]


if __name__ == "__main__":
    raise SystemExit(main())
