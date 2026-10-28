"""Summarize locally recorded host, LLM and decision-model latency."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from civ_agent.runtime_profile import profile


def parser(*, add_help=True):
    result = argparse.ArgumentParser(description=__doc__, add_help=add_help)
    result.add_argument("--state-dir", type=Path, nargs="+", required=True, help="Game or batch directories")
    result.add_argument("--since-restart", action="store_true", help="Use each game's latest launch window")
    result.add_argument("--json", action="store_true", help="Include histogram statistics and model/role breakdowns")
    return result


def run(args):
    try:
        report = profile(args.state_dir, since_restart=args.since_restart)
    except (OSError, ValueError, KeyError, sqlite3.Error) as error:
        print(f"Cannot read runtime profile: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    print(f"Recorded latency across {len(report['windows'])} games (concurrent durations can overlap):")
    rows = [("Host round trips", report["hostRoundTrips"]), ("Decision models", report["decisions"])]
    rows.extend(report["llm"]["metrics"].items())
    for name, row in rows:
        mean = f"{row['meanMs'] / 1000:.3f}s" if row["meanMs"] is not None else "unavailable"
        print(f"{name}: {row['count']} measurements, {row['sumMs'] / 1000:.1f}s total, {mean} mean")
    if not report["llm"]["available"]:
        print(
            "LLM timings unavailable: these services have not recorded runner metrics. Rebuild and resume to collect."
        )
    for note in report["notes"]:
        print(note)
    return 0
