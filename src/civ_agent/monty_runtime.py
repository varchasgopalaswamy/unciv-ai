"""Build-time smoke check for the pinned sandbox client and worker binary."""

import json
import sysconfig
from importlib.metadata import version
from pathlib import Path

from pydantic_monty import Monty


def verify_runtime() -> dict[str, str]:
    expected = {
        "pydantic-ai-slim": "2.64.0",
        "pydantic-ai-harness": "0.64.0",
        "pydantic-monty": "1.5.0",
        "pydantic-monty-client": "1.5.0",
        "pydantic-monty-runtime": "1.5.0",
    }
    installed = {name: version(name) for name in expected}
    if installed != expected:
        raise RuntimeError("MONTY_RUNTIME_VERSION_MISMATCH")
    # The runtime wheel installs here, and the SDK searches this directory
    # independently of PATH. Exercise its default discovery and worker IPC.
    binary = Path(sysconfig.get_path("scripts")) / "monty"
    if not binary.is_file():
        raise RuntimeError("MONTY_RUNTIME_BINARY_MISSING")
    with Monty(max_processes=1, request_timeout=5) as pool:
        with pool.checkout(limits={"max_memory": 32 * 1024 * 1024}) as session:
            if session.feed_run("sum([2, 3, 5])") != 10:
                raise RuntimeError("MONTY_RUNTIME_EXECUTION_FAILED")
    return {**installed, "binary": str(binary), "execution": "passed"}


if __name__ == "__main__":
    print(json.dumps(verify_runtime(), sort_keys=True))
