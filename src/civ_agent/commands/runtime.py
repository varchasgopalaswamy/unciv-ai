"""Container setup, immutable images and the dedicated benchmark login."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from civ_agent.container_images import build_images
from civ_agent.container_support import (
    DEFAULT_AUTH,
    ContainerError,
    auth_name,
    command,
    inspected,
    locked,
    mount,
    preflight,
    resolve_image,
)
from civ_agent.tls import private_directory


def parser(*, add_help=True, group="runtime", action="setup"):
    result = argparse.ArgumentParser(description=__doc__, add_help=add_help)
    result.set_defaults(runtime_group=group, action=action)
    if group == "image":
        result.add_argument("--repo-dir", type=Path)
        result.add_argument("--unciv-dir")
        result.add_argument("--codex", default="codex")
        result.add_argument("--tag", default="localhost/civ-agent")
    if group == "auth":
        result.add_argument("--auth-dir", type=Path, default=DEFAULT_AUTH)
        result.add_argument("--image", default="localhost/civ-agent:runtime")
    return result


def run(args):
    try:
        os.umask(0o077)
        if args.runtime_group == "image":
            print(
                json.dumps(
                    build_images(repo_dir=args.repo_dir, unciv_dir=args.unciv_dir, codex=args.codex, tag=args.tag),
                    indent=2,
                )
            )
            return 0
        if args.runtime_group == "runtime":
            preflight(background=False)
            command(["loginctl", "enable-linger", str(os.getuid())])
            preflight()
            print("Rootless runtime ready. Games will survive terminal closure and logout.")
            return 0
        root = args.auth_dir.expanduser().resolve()
        if args.action == "status":
            print(
                json.dumps(
                    {
                        "auth_directory": str(root),
                        "login_present": (root / "codex/auth.json").is_file(),
                        "manager_running": bool((inspected(auth_name(root)) or {}).get("State", {}).get("Running")),
                    },
                    indent=2,
                )
            )
            return 0
        private_directory(root)
        private_directory(root / "codex")
        with locked(root / ".launch.lock"):
            if (inspected(auth_name(root)) or {}).get("State", {}).get("Running"):
                raise ContainerError(
                    "Stop the login manager before changing its account; active games use this session"
                )
            image = resolve_image(args.image)
            return subprocess.call(
                [
                    "podman",
                    "run",
                    "--rm",
                    "-it",
                    "--userns=keep-id",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges",
                    "--read-only",
                    "--tmpfs",
                    "/tmp:rw,size=64m,mode=1777",
                    *mount(root / "codex", "/auth", writable=True),
                    "--env",
                    "CODEX_HOME=/auth",
                    "--env",
                    "HOME=/tmp",
                    "--entrypoint",
                    "/usr/local/bin/codex",
                    image,
                    "-c",
                    'cli_auth_credentials_store="file"',
                    "login",
                    "--device-auth",
                ]
            )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 2
