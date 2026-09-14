"""Launch one civilization's HTTPS Codex service in the foreground."""

import argparse
import errno
import hashlib
import json
import os
import secrets
import shutil
import sys
from pathlib import Path

from aiohttp import web

from civ_agent.agent_service import AgentService, ServiceError, server_ssl_context
from civ_agent.agent_settings import add_settings_arguments
from civ_agent.tls import (
    TLSConfigError,
    endpoint_configuration,
    ensure_server_certificate,
    host_origin,
    private_directory,
    regular_file,
    write_private_json,
)
from civ_agent.turn_archive import civilization_directory_name


def parser(*, add_help: bool = True) -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__, add_help=add_help)
    result.add_argument("civilization", help="Fixed civilization served by this process, e.g. Rome")
    result.add_argument(
        "--state-dir",
        type=Path,
        default=Path(os.environ.get("UNCIV_HOST_DIR", "~/.local/state/unciv-host/demo")).expanduser(),
    )
    result.add_argument("--service-dir", type=Path, help="Independent durable service state directory")
    result.add_argument("--workspace", type=Path, help="This civilization's Codex working directory")
    result.add_argument("--mcp-url", help="Permitted HTTPS host MCP endpoint; inferred from endpoints.json by default")
    result.add_argument("--mcp-ca-file", type=Path, help="Additional trusted CA for the host MCP endpoint")
    result.add_argument(
        "--port", type=int, default=None, help="HTTPS port (Rome:8865, Greece:8866, France:8867; required for others)"
    )
    result.add_argument("--listen-host", default="127.0.0.1")
    result.add_argument(
        "--public-host", default="localhost", help="Hostname clients use; included in generated certificate"
    )
    result.add_argument(
        "--cert-file", type=Path, help="Existing service HTTPS certificate, with --key-file and --ca-file"
    )
    result.add_argument("--key-file", type=Path)
    result.add_argument("--ca-file", type=Path, help="CA clients trust for this service certificate")
    result.add_argument("--token-file", type=Path, help="Existing service bearer token file (created if absent)")
    result.add_argument("--codex", default="codex", help="Codex executable path")
    add_settings_arguments(
        result,
        model_help="Default model for new conversations; civ-agent match start --model overrides it",
        reasoning_help="Default reasoning effort for new conversations; civ-agent match start --reasoning-effort overrides it",
    )
    return result


def run(args: argparse.Namespace) -> int:
    port = args.port
    os.umask(0o077)
    try:
        if not args.civilization.strip() or len(args.civilization) > 128:
            raise ServiceError("INVALID_CIVILIZATION")
        port = (
            args.port
            if args.port is not None
            else {"Rome": 8865, "Greece": 8866, "France": 8867}.get(args.civilization)
        )
        if port is None or not 1 <= port <= 65535:
            raise ServiceError("INVALID_PORT")
        root = args.state_dir.expanduser().resolve()
        identity = civilization_directory_name(args.civilization)
        service_dir = private_directory((args.service_dir or root / "agent-services" / identity).expanduser().resolve())
        workspace = private_directory((args.workspace or root / "agents" / identity).expanduser().resolve())
        endpoint, mcp_ca = endpoint_configuration(root, args.mcp_url, args.mcp_ca_file)
        material_options = (args.cert_file, args.key_file, args.ca_file)
        if any(material_options) and not all(material_options):
            raise ServiceError("CERTIFICATE_KEY_AND_CA_REQUIRED")
        if all(material_options):
            material = dict(
                zip(
                    ("cert_file", "key_file", "ca_file"),
                    (path.expanduser().resolve() for path in material_options),
                    strict=True,
                )
            )
        else:
            material = ensure_server_certificate(
                root,
                "agent-" + hashlib.sha256(args.civilization.encode()).hexdigest(),
                ["localhost", "127.0.0.1", args.public_host],
            )
        token_file = (args.token_file or service_dir / "token.json").expanduser().resolve()
        if any(path.is_relative_to(workspace) for path in (service_dir, token_file, material["key_file"])):
            raise ServiceError("PRIVATE_STATE_INSIDE_WORKSPACE")
        if not token_file.exists():
            write_private_json(token_file, {"token": secrets.token_urlsafe(32)})
        regular_file(token_file, private=True)
        credential = json.loads(token_file.read_text())
        if not isinstance(credential, dict) or set(credential) != {"token"}:
            raise ServiceError("INVALID_SERVICE_TOKEN")
        token = credential["token"]
        if (
            not isinstance(token, str)
            or len(token) != 43
            or any(
                character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
                for character in token
            )
        ):
            raise ServiceError("INVALID_SERVICE_TOKEN")
        executable = shutil.which(args.codex)
        if executable is None:
            raise ServiceError("CODEX_NOT_FOUND")
        ssl_context = server_ssl_context(material["cert_file"], material["key_file"])
        service = AgentService(
            args.civilization,
            workspace,
            service_dir,
            token,
            endpoint + "/mcp",
            mcp_ca,
            executable,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            gateway_url=host_origin(args.public_host, port) + "/mcp",
            gateway_ca_file=material["ca_file"],
        )
        url = host_origin(args.public_host, port) + "/agent"
        # This file is operator-only discovery. Neither its bearer token nor the host's
        # operator credentials are ever given to the model subprocess.
        discovery = root / "agent-services" / identity / "endpoint.json"
        write_private_json(
            discovery,
            {
                "civilizationId": args.civilization,
                "url": url,
                "tokenFile": str(token_file),
                "caFile": str(material["ca_file"]),
            },
        )
        print(f"Starting {args.civilization} agent service at {url}", flush=True)
        print(f"Player turn files: {workspace}", flush=True)
        print(
            f"New conversation defaults: model={args.model or 'Codex config'}, reasoning={args.reasoning_effort or 'Codex config'}",
            flush=True,
        )
        web.run_app(
            service.application(),
            host=args.listen_host,
            port=port,
            ssl_context=ssl_context,
            access_log=None,
            print=lambda _: print(f"AGENT-READY:{url}", flush=True),
        )
        return 0
    except (OSError, ValueError, ServiceError, TLSConfigError) as error:
        if isinstance(error, OSError) and error.errno == errno.EADDRINUSE:
            print(
                f"Cannot launch agent service: {args.listen_host}:{port} is already in use. "
                "Stop the service using that port or choose a different --port. "
                "Each running agent service needs its own port, even across different match directories.",
                file=sys.stderr,
            )
        else:
            print(
                "Cannot launch agent service. Check HTTPS endpoint, certificate paths, private file permissions, port, and Codex installation.",
                file=sys.stderr,
            )
        return 1
