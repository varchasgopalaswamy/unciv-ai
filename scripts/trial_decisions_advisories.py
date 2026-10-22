"""Isolated classifier trial for either backend, using fixed replay cases.

The optional Luna API key is read in this trusted process, never copied into trial
artifacts. Reusing a trial directory retains its identity, allowance and results.
"""

import argparse
import asyncio
import json
import secrets
from pathlib import Path

from aiohttp import web

from civ_agent.advisory_replay import live, load_cases
from civ_agent.container_supervisor import authorize_agent
from civ_agent.decisions_backends import DecisionBackend
from civ_agent.decisions_broker import DecisionsBroker, RoutingClient, read_decisions_key
from civ_agent.decisions_policy import DecisionsSettings
from civ_agent.diplomacy_decisions import DecisionsClient
from civ_agent.tls import private_directory, write_private_json


async def trial(root, key_file, fixtures, *, backend: DecisionBackend = "jev", acceptance_probability=None):
    root = private_directory(root)
    manifest = root / "trial.json"
    if not manifest.exists():
        write_private_json(manifest, {"game": "advisory-trial-" + secrets.token_hex(16)})
    identity = json.loads(manifest.read_text())["game"]
    options = {"decisions_acceptance_probability": acceptance_probability} if acceptance_probability is not None else {}
    settings = DecisionsSettings(
        decisions_enabled=True, decisions_backend=backend, additional_decisions_budget_usd=0.005, **options
    )
    auth, seat = private_directory(root / "broker"), private_directory(root / "player")
    authorize_agent(auth, identity, 0, seat, decisions=True, settings=settings.model_dump(), player="Rome")
    key = read_decisions_key(key_file) if backend == "luna" else None
    broker = DecisionsBroker(auth, DecisionsClient(key, backend=backend))
    socket = root / "broker.sock"
    socket.unlink(missing_ok=True)
    runner = web.AppRunner(broker.application())
    await runner.setup()
    try:
        await web.UnixSite(runner, str(socket)).start()
        result = await live(
            load_cases(fixtures),
            RoutingClient(socket, seat / "decisions-client.json", backend=backend),
            settings=settings,
        )
        result["accounting"] = broker.ledger.status(identity + "-0")
        write_private_json(root / "results.json", result)
        print(json.dumps({k: v for k, v in result.items() if k != "responses"}, indent=2))
    finally:
        await runner.cleanup()
        socket.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial-dir", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, help="Required only for --backend luna")
    parser.add_argument("--backend", choices=("luna", "jev"), default="jev")
    parser.add_argument("--acceptance-probability", type=float)
    parser.add_argument("--fixtures", type=Path, default=Path("tests/fixtures/decisions-advisory-replay.json"))
    args = parser.parse_args()
    if args.backend == "luna" and args.key_file is None:
        parser.error("--key-file is required for the Luna backend")
    asyncio.run(
        trial(
            args.trial_dir,
            args.key_file,
            args.fixtures,
            backend=args.backend,
            acceptance_probability=args.acceptance_probability,
        )
    )


if __name__ == "__main__":
    main()
