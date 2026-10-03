"""Shared subprocess commands for exercising the installed application."""

import contextlib
import os
import signal
import subprocess
import sys
import time

from civ_agent.cli import main
from civ_agent.turn_archive import civilization_directory_name


def match_arguments(*args: str) -> list[str]:
    action = args[0] if args and not args[0].startswith("-") else "start"
    options = args[1:] if args and args[0] == action else args
    return ["match", action, *options]


def match_command(*args: str) -> list[str]:
    return [sys.executable, "-B", "-m", "civ_agent", *match_arguments(*args)]


def run_match(args: list[str]) -> int:
    return main(match_arguments(*args))


@contextlib.contextmanager
def services(root, startup_timeout, announce):
    """Own only the processes started here; agents close before the host."""

    def stop(process):
        if process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                process.wait(timeout=60)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)

    with contextlib.ExitStack() as logs:

        def start(arguments, label, ready):
            path = root / f"{civilization_directory_name(label)}.log"
            log = logs.enter_context(path.open("a"))
            offset = log.tell()
            process = subprocess.Popen(
                [sys.executable, "-u", "-m", "civ_agent", *arguments, "--state-dir", str(root)],
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
            logs.callback(stop, process)
            deadline = time.monotonic() + startup_timeout
            with path.open() as reader:
                reader.seek(offset)
                while True:
                    if process.poll() is not None:
                        raise RuntimeError(f"{label} exited with {process.returncode}; inspect {path}")
                    if any(line.startswith(ready) for line in reader.readlines()):
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f"{label} startup timed out after {startup_timeout}s; inspect {path}")
                    time.sleep(0.25)
            announce("Ready", label)

        yield start
