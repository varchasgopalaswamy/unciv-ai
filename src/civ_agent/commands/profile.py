"""Summarize native SDK boundary durations and published host accounting."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from civ_agent.runtime_profile import profile


def parser(*, add_help=True):
    result = argparse.ArgumentParser(description=__doc__, add_help=add_help)
    result.add_argument("--state-dir", type=Path, nargs="+", required=True, help="Game or batch directories")
    result.add_argument("--since-restart", action="store_true", help="Use each game's latest launch window")
    result.add_argument(
        "--json",
        action="store_true",
        help="Include measured SDK timing, model/role breakdowns and unavailable measurements",
    )
    return result


def run(args):
    try:
        report = profile(args.state_dir, since_restart=args.since_restart)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, SQLAlchemyError) as error:
        print(f"Cannot read native runtime profile ({type(error).__name__}).", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    print(f"Recorded latency across {len(report['windows'])} games (concurrent durations can overlap):")
    rows = []
    rows.extend(report["llm"]["metrics"].items())
    for name, row in rows:
        mean = f"{row['meanMs'] / 1000:.3f}s" if row["meanMs"] is not None else "unavailable"
        print(f"{name}: {row['count']} measurements, {row['sumMs'] / 1000:.1f}s total, {mean} mean")
    if not report["llm"]["available"]:
        print("Native SDK timing samples unavailable; no timing is inferred from tokens or counters.")
    for name, row in report["hostTimings"].items():
        print(
            f"Host {name}: {row['count']} calls, {row['sumMs'] / 1000:.3f}s total, {row['maxMs'] / 1000:.3f}s maximum"
        )
    for row in report["unavailable"]:
        print(f"Unavailable: {row['reason']} ({row['game']})")
    for note in report["notes"]:
        print(note)
    return 0
