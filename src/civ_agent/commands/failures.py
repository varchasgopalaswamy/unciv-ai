"""Group recorded tool failures and inspect exact receipts without contacting models."""

import argparse
import json
import sys
import time
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from civ_agent.tls import private_directory, write_private, write_private_json
from civ_agent.tool_failures import failure_detail, failure_report, render_report


def positive_seconds(value):
    seconds = int(value)
    if seconds < 5:
        raise argparse.ArgumentTypeError("watch interval must be at least five seconds")
    return seconds


def parser(*, add_help=True):
    result = argparse.ArgumentParser(description=__doc__, add_help=add_help)
    result.add_argument("--state-dir", type=Path, nargs="+", required=True, help="Game or batch directories")
    result.add_argument("--role", help="For example gameplay (Main), economy, military or communication")
    result.add_argument("--tool", help="Exact tool name")
    result.add_argument("--code", help="Exact error code")
    result.add_argument("--since-turn", type=int, help="Include calls from this native turn onward")
    result.add_argument("--include-internal", action="store_true", help="Also show startup and host reads")
    result.add_argument("--id", help="Inspect one failure with its full response and nearby calls")
    result.add_argument("--json", action="store_true", help="Emit machine-readable groups and failure evidence")
    result.add_argument("--output-dir", type=Path, help="Write report.json and report.txt atomically here")
    result.add_argument(
        "--watch", type=positive_seconds, metavar="SECONDS", help="Refresh until interrupted (no model calls)"
    )
    return result


def run(args):
    try:
        if args.watch and args.id:
            raise ValueError("--watch cannot be combined with --id")
        if args.since_turn is not None and args.since_turn < 0:
            raise ValueError("--since-turn cannot be negative")
        while True:
            report = failure_report(
                args.state_dir,
                role=args.role,
                tool=args.tool,
                code=args.code,
                since_turn=args.since_turn,
                include_internal=args.include_internal,
            )
            if args.id:
                print(json.dumps(failure_detail(report, args.id), indent=2))
                return 0
            rendered = render_report(report)
            if args.output_dir:
                destination = private_directory(args.output_dir.expanduser().resolve())
                write_private_json(destination / "report.json", report)
                write_private(destination / "report.txt", rendered.encode())
            if not args.watch or not args.output_dir:
                print(json.dumps(report, indent=2) if args.json else rendered, flush=True)
            else:
                print(
                    f"{report['generatedAt']}: {len(report['failures'])} issues; {len(report['unavailable'])} unreadable journals",
                    flush=True,
                )
            if not args.watch:
                return 0 if not report["unavailable"] else 2
            time.sleep(args.watch)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, KeyError, TypeError, SQLAlchemyError) as error:
        print(f"Cannot read tool failures: {error}", file=sys.stderr)
        return 2
