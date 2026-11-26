"""Explicit private operator exports of native SDK conversations and authored stores."""

import argparse
import asyncio
import os
import sqlite3
import sys
from pathlib import Path

from civ_agent.orchestration.history_exports import ExportError as ExportError
from civ_agent.orchestration.history_exports import export_native_history


def export_history(
    state_dir: Path,
    output_dir: Path,
    civilization: str | None = None,
    *,
    notebooks: bool = False,
    epoch: str | None = None,
    coordinated: bool = False,
):
    return asyncio.run(
        export_native_history(
            state_dir, output_dir, civilization, notebooks=notebooks, epoch=epoch, coordinated=coordinated
        )
    )


def parser(*, add_help: bool = True) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, add_help=add_help)
    parser.add_argument(
        "--state-dir", type=Path, default=Path(os.environ.get("UNCIV_HOST_DIR", "~/.local/state/unciv-host/demo"))
    )
    parser.add_argument("civilization", nargs="?", help="Export one player (default: all native players)")
    parser.add_argument("--epoch", help="Explicit retained historical epoch; default: published current epoch")
    parser.add_argument(
        "--notebooks", action="store_true", help="Also export selected private native notes and all four role plans"
    )
    parser.add_argument("--coordinated", action="store_true", help="Require a stopped runtime and drained writers")
    parser.add_argument("--output-dir", type=Path, help="Default: sibling STATE_DIR-exports; creates a new directory")
    return parser


def run(args: argparse.Namespace) -> int:
    try:
        state_dir = args.state_dir.expanduser().resolve()
        output = (
            args.output_dir.expanduser().resolve()
            if args.output_dir
            else state_dir.with_name(state_dir.name + "-exports")
        )
        for entry in export_history(
            state_dir,
            output,
            args.civilization,
            notebooks=args.notebooks,
            epoch=args.epoch,
            coordinated=args.coordinated,
        ):
            print(
                f"{entry['civilization']} {entry['epoch']} {entry['agent']} {entry['runId']}: {entry['history'] or entry['run']}"
            )
        return 0
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        message = (
            str(error)
            if isinstance(error, ExportError)
            else "Cannot export native history; check publication, private paths, permissions and storage."
        )
        print("civ-agent history: " + message, file=sys.stderr)
        return 2
