"""Inspect, propose and decide canonical economic trades over HTTPS."""

import argparse
import asyncio
import json
import uuid
from pathlib import Path

from civ_agent.commands.agent import player_credentials
from civ_agent.host_connection import HostConnection
from civ_agent.proposal_gateway import public_result
from civ_agent.tls import endpoint_configuration


def parser(*, add_help=True):
    result = argparse.ArgumentParser(add_help=add_help)
    result.add_argument("civilization")
    result.add_argument("--state-dir", type=Path, required=True)
    result.add_argument("--partner")
    result.add_argument("--proposal")
    result.add_argument("--give-json", default="[]", help="JSON array of items from trade options")
    result.add_argument("--receive-json", default="[]", help="JSON array of items from trade options")
    return result


async def perform(args):
    root = args.state_dir.expanduser().resolve()
    _, token = player_credentials(root / "players.json", args.civilization)
    endpoint, ca = endpoint_configuration(root, None, None)
    async with HostConnection(endpoint + "/mcp", token, ca) as host:
        state = await host.tool(
            "player_communication", {"op": "state", "epoch": (await host.tool("player_turn", {}))["epoch"]}
        )
        epoch = state["epoch"]
        action = args.trade_action

        async def read():
            if not args.proposal:
                raise ValueError("--proposal is required")
            response = await host.tool(
                "player_communication", {"op": "trade_read", "epoch": epoch, "proposal": args.proposal}
            )
            if "error" in response:
                raise ValueError("Proposal is unavailable")
            return response

        fields = {"epoch": epoch, "requestId": "human-" + uuid.uuid4().hex}
        if action == "list":
            print(json.dumps(state.get("trades", []), indent=2))
            return 0
        if action == "read":
            print(json.dumps(public_result(await read()), indent=2))
            return 0
        if action == "options":
            if not args.partner:
                raise ValueError("--partner is required")
            fields = {"epoch": epoch, "op": "trade_options", "partner": args.partner}
        elif action in {"offer", "friendship"}:
            if not args.partner:
                raise ValueError("--partner is required")
            fields.update(
                op="trade_offer",
                partner=args.partner,
                give=json.loads(args.give_json),
                receive=json.loads(args.receive_json),
            )
            if action == "friendship":
                if args.proposal or args.give_json != "[]" or args.receive_json != "[]":
                    raise ValueError("Friendship has fixed terms; supply only --partner")
                fields.update(op="friendship_offer")
                del fields["give"], fields["receive"]
            if args.proposal:
                observed = (await read())["trade"]
                fields.update(proposal=args.proposal, version=observed["version"])
        else:
            observed = (await read())["trade"]
            print(json.dumps(public_result(observed), indent=2))
            if action == "accept" and input("Accept and enact these exact trade terms? [y/N] ").strip().lower() != "y":
                return 0
            # Pin what was displayed before prompting, even if a counteroffer arrived meanwhile.
            fields.update(op="trade_decide", proposal=args.proposal, version=observed["version"], decision=action)
        response = await host.tool("player_communication", fields)
        print(json.dumps(public_result(response), indent=2))
        return 1 if "error" in response else 0


def run(args):
    try:
        return asyncio.run(perform(args))
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, KeyError) as error:
        print(f"Trade command failed: {error}")
        return 2
