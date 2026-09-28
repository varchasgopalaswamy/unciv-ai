"""Compact pointers to immutable evidence in a civilization's database."""

import copy
import json

from sqlalchemy import select, tuple_

from civ_agent import memory_schema as tables
from civ_agent.memory_store import encode


def load_snapshot(db, saved):
    return expand(db, json.loads(saved))


def dump_snapshot(db, snapshot):
    return encode(compact(db, snapshot))


def job_states(db):
    """Completed jobs are immutable; only running jobs need a snapshot copy."""
    running = {
        row["id"]: json.loads(row["snapshot"])
        for row in db.execute(
            select(tables.jobs.c.id, tables.jobs.c.snapshot)
            .select_from(tables.active_jobs)
            .where(tables.jobs.c.status == "RUNNING")
        ).mappings()
    }
    return [
        {**row, **({"snapshot": running[row["id"]]} if row["id"] in running else {})}
        for row in db.execute(
            select(tables.jobs.c.id, tables.jobs.c.status).select_from(tables.active_jobs).order_by(tables.jobs.c.id)
        ).mappings()
    ]


def record_bodies(db, references):
    result = {}
    references = list(dict.fromkeys(references))
    for offset in range(0, len(references), 300):
        rows = db.execute(
            select(tables.records).where(
                tuple_(tables.records.c.key, tables.records.c.version).in_(references[offset : offset + 300])
            )
        ).mappings()
        for row in rows:
            result[row["key"], row["version"]] = {"category": row["category"], "body": json.loads(row["body"])}
    if len(result) != len(references):
        raise ValueError("CHECKPOINT_HISTORY_MISSING")
    return result


def compact(db, snapshot):
    """Keep frozen projections as small patches over their stored record versions."""
    result = copy.deepcopy({key: value for key, value in snapshot.items() if key != "records"})
    references = [(key, record["version"]) for key, record in snapshot["records"].items()]
    stored = record_bodies(db, references)
    frozen = {}
    for key, record in snapshot["records"].items():
        original = stored[key, record["version"]]
        changes = {
            name: value
            for name, value in record["body"].items()
            if name not in original["body"] or original["body"][name] != value
        }
        removed = sorted(set(original["body"]) - set(record["body"]))
        frozen[key] = {"version": record["version"], "changes": copy.deepcopy(changes), "removed": removed}
    result["records"] = frozen
    return result


def expand(db, snapshot):
    result = copy.deepcopy(snapshot)
    stored = record_bodies(
        db,
        [(key, reference["version"]) for key, reference in result["records"].items()],
    )
    for key, reference in result["records"].items():
        record = stored[key, reference["version"]]
        record["version"] = reference["version"]
        for name in reference["removed"]:
            record["body"].pop(name, None)
        record["body"].update(reference["changes"])
        result["records"][key] = record
    return result
