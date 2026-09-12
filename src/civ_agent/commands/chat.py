"""Human public/private chat and explicit social commitment decisions over HTTPS."""

import argparse
import asyncio
import json
import uuid
from pathlib import Path

from civ_agent.commands.agent import player_credentials
from civ_agent.proposal_gateway import HostConnection, public_result
from civ_agent.tls import endpoint_configuration


def parser(*, add_help=True):
    result = argparse.ArgumentParser(add_help=add_help)
    result.add_argument("civilization")
    result.add_argument("--state-dir", type=Path, required=True)
    result.add_argument("--to", help="Private recipient; omit for public chat")
    result.add_argument("--text")
    result.add_argument("--mention", action="append", default=[])
    result.add_argument("--proposal")
    result.add_argument("--participants", nargs="+")
    result.add_argument("--terms")
    result.add_argument("--conditions", default="Unconditional")
    result.add_argument("--kind", choices=("peace", "other"), default="other")
    result.add_argument("--expires-turn", type=int)
    result.add_argument("--reason")
    result.add_argument("--cursor", type=int, default=0)
    return result


async def perform(args):
    root = args.state_dir.expanduser().resolve()
    name, token = player_credentials(root / "players.json", args.civilization)
    endpoint, ca = endpoint_configuration(root, None, None)
    host = HostConnection(endpoint + "/mcp", token, ca)
    state = await host.tool("player_turn", {})
    epoch = state["epoch"]
    action = args.chat_action
    fields = {"epoch": epoch, "requestId": "human-" + uuid.uuid4().hex}
    if action == "send":
        if not args.text:
            raise ValueError("--text is required")
        fields.update(op="send", text=args.text, mentions=args.mention)
        if args.to:
            fields["to"] = args.to
    elif action == "offer":
        if not args.terms or not args.participants:
            raise ValueError("--terms and --participants are required")
        fields.update(
            op="offer",
            participants=list(dict.fromkeys([name, *args.participants])),
            terms=args.terms,
            conditions=args.conditions,
            kind=args.kind,
        )
        if args.expires_turn is not None:
            fields["expiresTurn"] = args.expires_turn
        if args.proposal:
            fields.update(op="revise", proposal=args.proposal)
    elif action in {"accept", "withdraw", "reconsider"}:
        ledger = await host.tool("player_communication", {"op": "state", "epoch": epoch})
        offers = [offer for offer in ledger["offers"] if offer["proposal"] == args.proposal]
        if not offers:
            raise ValueError("Read the ledger and supply a visible --proposal")
        offer = (
            offers[-1]
            if action == "accept"
            else next((offer for offer in offers if offer.get("effective")), offers[-1])
        )
        print(json.dumps(public_result(offer), indent=2))
        if action == "accept" and input("Accept these exact social terms? [y/N] ").strip().lower() != "y":
            return
        fields.update(op=action, proposal=args.proposal, version=offer["version"])
        if action != "accept":
            if not args.reason:
                raise ValueError("--reason is required")
            fields["reason"] = args.reason
    elif action == "ledger":
        fields = {"op": "state", "epoch": epoch}
    else:
        cursor = args.cursor
        while True:
            page = await host.tool(
                "player_communication",
                {
                    "op": "read",
                    "epoch": epoch,
                    "cursor": cursor,
                    "limit": 100,
                    "waitSeconds": 25 if action == "follow" else 0,
                },
            )
            if "error" in page:
                raise ValueError("Chat history unavailable")
            for event in page["events"]:
                print(json.dumps(public_result(event), ensure_ascii=False), flush=True)
            cursor = page["nextCursor"]
            if action != "follow" and not page["hasMore"]:
                print(f"Next cursor: {cursor}")
                return
    response = await host.tool("player_communication", fields)
    print(json.dumps(public_result(response), indent=2, ensure_ascii=False))


def run(args):
    try:
        asyncio.run(perform(args))
        return 0
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, KeyError):
        print("Chat command failed. Check player identity, HTTPS configuration, and command arguments.")
        return 2
