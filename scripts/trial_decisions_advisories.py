"""Short paid classifier trial with an isolated trusted ledger and fixed replay cases.

The separate API key is read in this trusted process, never copied into trial
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
from civ_agent.decisions_broker import DecisionsBroker, RoutingClient, read_decisions_key
from civ_agent.decisions_policy import DecisionsSettings
from civ_agent.diplomacy_decisions import DecisionsClient
from civ_agent.tls import private_directory, write_private_json


async def trial(root, key_file, fixtures):
    root = private_directory(root)
    manifest = root / "trial.json"
    if not manifest.exists():
        write_private_json(manifest, {"game": "advisory-trial-" + secrets.token_hex(16)})
    identity = json.loads(manifest.read_text())["game"]
    settings = DecisionsSettings(decisions_enabled=True, additional_decisions_budget_usd=0.005)
    auth, seat = private_directory(root / "broker"), private_directory(root / "player")
    authorize_agent(auth, identity, 0, seat, decisions=True, settings=settings.model_dump(), player="Rome")
    broker = DecisionsBroker(auth, DecisionsClient(read_decisions_key(key_file)))
    socket = root / "broker.sock"
    socket.unlink(missing_ok=True)
    runner = web.AppRunner(broker.application())
    await runner.setup()
    try:
        await web.UnixSite(runner, str(socket)).start()
        result = await live(load_cases(fixtures), RoutingClient(socket, seat / "decisions-client.json"))
        result["accounting"] = broker.ledger.status(identity + "-0")
        write_private_json(root / "results.json", result)
        print(json.dumps({k: v for k, v in result.items() if k != "responses"}, indent=2))
    finally:
        await runner.cleanup()
        socket.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial-dir", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, default=Path("tests/fixtures/decisions-advisory-replay.json"))
    args = parser.parse_args()
    asyncio.run(trial(args.trial_dir, args.key_file, args.fixtures))


if __name__ == "__main__":
    main()
