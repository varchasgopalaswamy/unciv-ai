"""Explicit operator exports of recorded conversations and telemetry."""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from civ_agent.memory_exports import export_files, notebook_files
from civ_agent.monitoring import get_record, path_for, read_database, records
from civ_agent.runner_history import render_markdown
from civ_agent.tls import private_directory, regular_file, write_private, write_private_json
from civ_agent.turn_archive import civilization_directory_name


class ExportError(ValueError):
    pass


def export_history(state_dir: Path, output_dir: Path, civilization: str | None = None, *, notebooks: bool = False):
    """Export committed monitoring rows without contacting a service or the model."""
    with read_database(path_for(state_dir)) as db:
        query = select(records.c.payload).where(records.c.collection == "streams")
        if civilization is not None:
            query = query.where(records.c.civilization == civilization)
        sources = [json.loads(value) for value in db.scalars(query.order_by(records.c.revision))]
        if not sources:
            raise ExportError("No recorded conversations match this selection.")
        export_root = Path(tempfile.mkdtemp(prefix="export-", dir=private_directory(output_dir)))
        entries = []
        for source in sources:
            directory = export_root
            for component in (
                source["epoch"],
                source["civilization"],
                f"turn_{source['gameTurn']}",
                source["activationId"],
            ):
                directory = private_directory(directory / civilization_directory_name(component))
            values = db.scalars(
                select(records.c.payload)
                .where(
                    records.c.collection == "events",
                    records.c.epoch == source["epoch"],
                    records.c.civilization == source["civilization"],
                    records.c.activation == source["activationId"],
                )
                .order_by(records.c.sequence)
            )
            events = [json.loads(value) for value in values]
            write_private(directory / "events.jsonl", "".join(json.dumps(event) + "\n" for event in events).encode())
            write_private(directory / "history.md", render_markdown(source, events).encode())
            entries.append(
                {**source, "history": str(directory / "history.md"), "events": str(directory / "events.jsonl")}
            )
        for collection in ("turns", "jobs", "host-api", "diplomacy"):
            query = select(records.c.payload).where(records.c.collection == collection)
            if civilization is not None and collection != "diplomacy":
                query = query.where(records.c.civilization == civilization)
            values = db.scalars(query.order_by(records.c.revision))
            if civilization is not None and collection == "diplomacy":
                values = (value for value in values if civilization in json.loads(value)["participants"])
            write_private(export_root / (collection + ".jsonl"), "".join(value + "\n" for value in values).encode())
        # These are operator reports; the result may include every player's score.
        for name in ("run", "result"):
            write_private_json(export_root / (name + ".json"), get_record(db, "state", name) or {})
        write_private_json(export_root / "index.json", {"conversations": entries})
    if notebooks:
        for name in dict.fromkeys(source["civilization"] for source in sources):
            service = state_dir / "agent-services" / civilization_directory_name(name)
            memory = service / "civilization/memory.sqlite3"
            if not memory.exists():
                raise ExportError(f"No local private notebook for {name}; export it on the agent machine.")
            with read_database(memory) as db:
                files = notebook_files(db)
            destination = private_directory(export_root / "notebooks" / civilization_directory_name(name))
            export_files(destination, destination, files)
            result = service / "result.json"
            if result.exists():
                regular_file(result, private=True)
                write_private(destination / "result.json", result.read_bytes())
    return entries


def parser(*, add_help: bool = True) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, add_help=add_help)
    parser.add_argument(
        "--state-dir", type=Path, default=Path(os.environ.get("UNCIV_HOST_DIR", "~/.local/state/unciv-host/demo"))
    )
    parser.add_argument("civilization", nargs="?", help="Export one player (default: all players and epochs)")
    parser.add_argument(
        "--notebooks", action="store_true", help="Also export the selected local players' private notebooks"
    )
    parser.add_argument("--output-dir", type=Path, help="Default: STATE_DIR/exports; creates a new directory")
    return parser


def run(args: argparse.Namespace) -> int:
    try:
        state_dir = args.state_dir.expanduser().resolve()
        output = args.output_dir.expanduser().resolve() if args.output_dir else state_dir / "exports"
        for entry in export_history(state_dir, output, args.civilization, notebooks=args.notebooks):
            print(f"{entry['civilization']} turn {entry['gameTurn']}: {entry['history']}")
        return 0
    except (OSError, ValueError, KeyError, TypeError, SQLAlchemyError) as error:
        message = (
            str(error)
            if isinstance(error, ExportError)
            else "Cannot read or write monitoring records; check paths, permissions and storage."
        )
        print("civ-agent history: " + message, file=sys.stderr)
        return 2
