"""Native Pydantic AI players and local game services."""

import argparse
from collections.abc import Sequence
from importlib.metadata import version

from civ_agent import match_watch
from civ_agent.commands import (
    batch,
    chat,
    failures,
    game,
    history,
    host,
    inspect,
    manager_test,
    profile,
    replay,
    runtime,
    trade,
    verify_agreement_scenario,
    verify_manager_scenario,
    verify_trade_cycle,
    verify_war_scenario,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="civ-agent", description=__doc__)
    result.add_argument("--version", action="version", version=f"%(prog)s {version('civ-agent')}")
    groups = result.add_subparsers(dest="command", required=True)

    games = groups.add_parser("game", help="Launch and control a native game").add_subparsers(required=True)
    for action in ("start", "resume", "stop", "status", "remove"):
        command = games.add_parser(action, parents=[game.parser(add_help=False, action=action)])
        command.set_defaults(handler=game.run)

    manager_tests = games.add_parser(
        "test-managers",
        parents=[manager_test.parser(add_help=False)],
        help="Run controlled economy and military acceptance games",
    )
    manager_tests.set_defaults(handler=manager_test.run)

    batches = groups.add_parser("batch", help="Run isolated native games from TOML").add_subparsers(required=True)
    for action in ("generate", "start", "resume", "stop", "status"):
        command = batches.add_parser(action, parents=[batch.parser(add_help=False, action=action)])
        command.set_defaults(handler=batch.run)
    for group, actions in (
        ("runtime", ("setup",)),
        ("image", ("build",)),
        ("auth", ("login", "status", "stop", "restart")),
    ):
        commands = groups.add_parser(group).add_subparsers(required=True)
        for action in actions:
            command = commands.add_parser(action, parents=[runtime.parser(add_help=False, group=group, action=action)])
            command.set_defaults(handler=runtime.run)

    host_commands = groups.add_parser("host", help="Start the game host or inspect a saved game").add_subparsers(
        required=True
    )
    start = host_commands.add_parser(
        "start", parents=[host.parser(add_help=False)], help="Build and start the HTTPS game host"
    )
    start.set_defaults(handler=host.run)
    view = host_commands.add_parser(
        "inspect", parents=[inspect.parser(add_help=False)], help="Export or open a saved game in the desktop UI"
    )
    view.set_defaults(handler=inspect.run)

    matches = groups.add_parser("match", help="Inspect native games and verify domain scenarios").add_subparsers(
        required=True
    )
    watch = matches.add_parser(
        "watch", parents=[match_watch.parser(add_help=False)], help="Open a live terminal dashboard for a saved match"
    )
    watch.set_defaults(handler=match_watch.run)
    timing = matches.add_parser(
        "profile", parents=[profile.parser(add_help=False)], help="Inspect recorded host and model latency"
    )
    timing.set_defaults(handler=profile.run)
    failed_tools = matches.add_parser(
        "failures", parents=[failures.parser(add_help=False)], help="Group tool failures and inspect exact receipts"
    )
    failed_tools.set_defaults(handler=failures.run)
    verify_managers = matches.add_parser(
        "verify-manager-scenario",
        parents=[verify_manager_scenario.parser(add_help=False)],
        help="Audit controlled economy and military manager exercises",
    )
    verify_managers.set_defaults(handler=verify_manager_scenario.run)

    verify_trade = matches.add_parser(
        "verify-trade-cycle",
        parents=[verify_trade_cycle.parser(add_help=False)],
        help="Audit every trade-cycle step against canonical receipts and the native save",
    )
    verify_trade.set_defaults(handler=verify_trade_cycle.run)

    verify_war = matches.add_parser(
        "verify-war-scenario",
        parents=[verify_war_scenario.parser(add_help=False)],
        help="Audit three-cornered war milestones against receipts and native state",
    )
    verify_war.set_defaults(handler=verify_war_scenario.run)

    verify_agreements = matches.add_parser(
        "verify-agreement-scenario",
        parents=[verify_agreement_scenario.parser(add_help=False)],
        help="Audit agreement expiry, resource interruption and restart preservation",
    )
    verify_agreements.set_defaults(handler=verify_agreement_scenario.run)

    histories = groups.add_parser("history", help="Export recorded player conversations").add_subparsers(required=True)
    export = histories.add_parser(
        "export", parents=[history.parser(add_help=False)], help="Export native SDK histories and authored stores"
    )
    export.set_defaults(handler=history.run)
    replay_command = groups.add_parser(
        "replay", parents=[replay.parser(add_help=False)], help="Verify or export a recorded game replay"
    )
    replay_command.set_defaults(handler=replay.run)
    messages = groups.add_parser("chat", help="Public/private messages and social commitments").add_subparsers(
        required=True
    )
    for action in ("send", "inbox", "follow", "ledger", "offer", "accept", "withdraw", "reconsider"):
        command = messages.add_parser(action, parents=[chat.parser(add_help=False)])
        command.set_defaults(handler=chat.run, chat_action=action)
    trades = groups.add_parser("trade", help="Canonical economic and peace proposals and engine deals").add_subparsers(
        required=True
    )
    for action in ("list", "options", "read", "offer", "friendship", "accept", "decline", "withdraw"):
        command = trades.add_parser(action, parents=[trade.parser(add_help=False)])
        command.set_defaults(handler=trade.run, trade_action=action)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return args.handler(args)
