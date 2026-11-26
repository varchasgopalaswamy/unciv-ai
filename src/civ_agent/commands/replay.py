"""Verify a replay archive or export a civilization perspective."""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

from civ_agent.replay import ReplayArchive, perspective


def parser(*, add_help=True):
    result = argparse.ArgumentParser(description=__doc__, add_help=add_help)
    result.add_argument("--state-dir", required=True, type=Path)
    result.add_argument("--civilization", help="Export one major civilization's observations")
    outputs = result.add_mutually_exclusive_group()
    outputs.add_argument("--output", type=Path, help="Write the civilization timeline to a new JSONL file")
    outputs.add_argument("--video", type=Path, help="Render a civilization perspective to a new MP4 file")
    outputs.add_argument("--web", type=Path, help="Write a standalone interactive HTML replay")
    result.add_argument(
        "--from-snapshots", action="store_true", help="Reconstruct --web from archived player observations"
    )
    result.add_argument("--unciv-dir", type=Path, help="Use this Unciv checkout's artwork for --web")
    return result


def run(args):
    try:
        if args.web is None and (args.civilization is None) != (args.output is None and args.video is None):
            raise ValueError("Supply --civilization with --output or --video. For --web, --civilization is optional.")
        if args.unciv_dir is not None and args.web is None:
            raise ValueError("Supply --unciv-dir with --web.")
        if args.from_snapshots:
            if args.web is None:
                raise ValueError("Supply --from-snapshots with --web.")
            from civ_agent.replay_snapshots import snapshot_web_payload
            from civ_agent.replay_web import write_web_payload

            checkpoint = json.loads((args.state_dir / "exploration.json").read_bytes())
            world = json.loads(checkpoint["gameJson"])
            payload, counts = snapshot_web_payload(checkpoint, world, args.state_dir, args.civilization)
            write_web_payload(payload, args.web, world, unciv_dir=args.unciv_dir)
            print(json.dumps({"web": str(args.web), "snapshot_turns": counts}, indent=2))
            return 0
        archive = ReplayArchive(args.state_dir / "exploration.json")
        evidence = archive.verify()
        if args.output is not None:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=args.output.parent) as output:
                for event in perspective(archive, args.civilization):
                    output.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
                output.flush()
                os.link(output.name, args.output)
            evidence["output"] = str(args.output)
        if args.video is not None:
            from civ_agent.replay_movie import write_movie

            write_movie(perspective(archive, args.civilization), args.civilization, args.video)
            evidence["video"] = str(args.video)
        if args.web is not None:
            from civ_agent.replay_web import write_web_replay

            write_web_replay(archive, args.state_dir, args.civilization, args.web, unciv_dir=args.unciv_dir)
            evidence["web"] = str(args.web)
        print(json.dumps(evidence, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, IndexError, EOFError, sqlite3.Error) as error:
        print(f"Cannot read replay: {error}", file=sys.stderr)
        return 2
