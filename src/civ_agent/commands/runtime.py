"""Container setup, immutable images and the dedicated benchmark login."""

import argparse
import getpass
import json
import os
import subprocess
import sys
from pathlib import Path

from civ_agent.container_images import build_images
from civ_agent.container_support import (
    DEFAULT_AUTH,
    auth_name,
    command,
    inspected,
    preflight,
    resolve_image,
)
from civ_agent.provider_credentials import PROVIDERS, read_api_key, valid_api_key
from civ_agent.tls import private_directory, write_private


def parser(*, add_help=True, group="runtime", action="setup"):
    result = argparse.ArgumentParser(description=__doc__, add_help=add_help)
    result.set_defaults(runtime_group=group, action=action)
    if group == "image":
        result.add_argument("--repo-dir", type=Path)
        result.add_argument("--unciv-dir")
        result.add_argument("--tag", default="localhost/civ-agent")
    if group == "auth":
        if action == "restart":
            result.description = "Recreate configured auth and Decisions servers using the selected runtime image."
        if action == "stop":
            result.description = "Stop the shared login manager, retaining saved credentials."
        if action not in {"restart", "stop"}:
            result.add_argument("--provider", choices=PROVIDERS, default="openai")
        result.add_argument("--auth-dir", type=Path, default=DEFAULT_AUTH)
        if action == "restart":
            result.add_argument("--image", default="localhost/civ-agent:runtime")
        if action == "login":
            result.add_argument("--key-file", type=Path, help="Import an owner-only OpenRouter API key file")
            result.add_argument("--project", help="Google Cloud project charged for Gemini requests")
            result.add_argument("--location", default="global", help="Google Cloud model location (default: global)")
            result.add_argument("--credentials-file", type=Path, help="Import a Google Cloud ADC JSON file")
    return result


def run(args):
    try:
        os.umask(0o077)
        if args.runtime_group == "image":
            print(
                json.dumps(
                    build_images(repo_dir=args.repo_dir, unciv_dir=args.unciv_dir, tag=args.tag),
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
        if args.action == "stop":
            from civ_agent.container_supervisor import stop_auth

            print(json.dumps(stop_auth(root), indent=2))
            return 0
        if args.action == "restart":
            from civ_agent.container_supervisor import restart_brokers

            image = resolve_image(args.image)
            print(json.dumps(restart_brokers(root, image), indent=2))
            return 0
        if args.provider == "google-cloud":
            from civ_agent.google_cloud_auth import CONFIG_NAME, import_login, saved_config

            if args.action == "status":
                status = {"provider": "google-cloud", "auth_directory": str(root), "login_present": False}
                if (root / CONFIG_NAME).is_file():
                    config = saved_config(root)
                    status.update(login_present=True, project=config.project, location=config.location)
                print(json.dumps(status, indent=2))
                return 0
            if args.key_file is not None:
                raise ValueError("Google Cloud login uses ADC; supply --credentials-file instead of --key-file")
            project = args.project or os.environ.get("GOOGLE_CLOUD_PROJECT")
            if not project:
                raise ValueError("Select the credited Google Cloud project with --project PROJECT_ID")
            config = import_login(root, project=project, location=args.location, credentials_file=args.credentials_file)
            print(f"Google Cloud login saved for {config.project} ({config.location}).")
            return 0
        if getattr(args, "credentials_file", None) is not None or getattr(args, "project", None) is not None:
            raise ValueError("--credentials-file and --project require --provider google-cloud")
        if args.provider == "openrouter":
            path = root / "openrouter.key"
            if args.action == "status":
                print(
                    json.dumps(
                        {"provider": "openrouter", "auth_directory": str(root), "key_present": path.is_file()}, indent=2
                    )
                )
                return 0
            key = read_api_key(args.key_file.expanduser()) if args.key_file else os.environ.get("OPENROUTER_API_KEY")
            if key is None:
                key = getpass.getpass("OpenRouter API key: ").strip()
            if not valid_api_key(key):
                raise ValueError("Invalid OpenRouter API key")
            private_directory(root)
            write_private(path, (key + "\n").encode())
            print(f"OpenRouter key stored in {path}. Resume stopped games to use the new key.")
            return 0
        if getattr(args, "key_file", None) is not None:
            raise ValueError("--key-file is only supported for --provider openrouter")
        if args.action == "status":
            print(
                json.dumps(
                    {
                        "auth_directory": str(root),
                        "login_present": (root / "openai-subscription.json").is_file(),
                        "manager_running": bool((inspected(auth_name(root)) or {}).get("State", {}).get("Running")),
                    },
                    indent=2,
                )
            )
            return 0
        from civ_agent.container_supervisor import login_subscription

        login_subscription(root)
        print("Subscription login saved.")
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 2
