"""Disposable human-readable projections, with a trusted manifest for cleanup."""

import json
import re
from pathlib import Path

from sqlalchemy import select

from civ_agent import memory_schema as tables
from civ_agent.memory_history import SummaryReader, summary_markdown
from civ_agent.memory_records import MEMORY_TYPES, record_text
from civ_agent.memory_store import RecordStore, encode
from civ_agent.model_view import model_view
from civ_agent.tls import private_directory, write_private, write_private_json


def render_record(key, record):
    body = record["body"]
    lines = [f"## {key} — {record['category']}", record_text(body)]
    for field in ("reasons", "uncertainties", "reconsiderWhen", "updatedTurn", "uncertainty"):
        if body.get(field):
            lines.append(f"{field}: {body[field]}")
    if body.get("sources"):
        lines.append("Evidence: " + ", ".join(body["sources"]))
    for assumption in body.get("assumptions", []):
        lines.append("Assumption: " + assumption["text"])
    if body.get("needsReview"):
        lines.append("Review: " + "; ".join(body["needsReview"]))
    return "\n\n".join(lines) + "\n"


def projection_files(records, events):
    active = {
        key: r for key, r in records.items() if r["category"] in MEMORY_TYPES and r["body"].get("state") == "active"
    }
    files = {
        "memory-index.md": "Private memory; use memory_read or memory_search.\n\n"
        + "\n".join(f"- {key}: {r['category']} — {record_text(r['body'])}" for key, r in active.items())
        + "\n",
        "chat.jsonl": "".join(encode(event) + "\n" for event in events),
    }
    groups = {"HANDOFF.md": [], "STRATEGY.md": [], "economy.md": []}
    for key, record in active.items():
        rendered = render_record(key, record)
        category = record["category"]
        if category == "handoff":
            groups["HANDOFF.md"].append(rendered)
        if category == "strategy":
            groups["STRATEGY.md"].append(rendered)
        if category == "city_plan":
            files[f"cities/{key}.md"] = rendered
            groups["economy.md"].append(rendered)
        if category == "note":
            files[f"notes/{key}.md"] = rendered
        for subject in record["body"].get("subjects", []):
            if subject == "economy":
                groups["economy.md"].append(rendered)
            elif re.fullmatch(r"[A-Z][A-Za-z0-9_ -]{0,100}", subject):
                groups.setdefault(f"relationships/{subject}.md", []).append(rendered)
    social = records.get("observation:communication", {}).get("body", {})
    ledger = {key: social.get(key, []) for key in ("offers", "consents", "outcomes", "trades", "agreements")}
    if any(ledger.values()):
        files["communication/ledger.json"] = json.dumps(ledger, ensure_ascii=False, indent=2) + "\n"
        participants = {
            name
            for key in ("offers", "consents", "outcomes")
            for row in ledger[key]
            for name in row.get("participants", [])
        }
        participants.update(
            name for row in ledger["trades"] for name in (row.get("author"), row.get("partner")) if name
        )
        participants.update(row["partner"] for row in ledger["agreements"] if row.get("partner"))
        for name in participants:
            if re.fullmatch(r"[A-Z][A-Za-z0-9_ -]{0,100}", name):
                groups.setdefault(f"relationships/{name}.md", []).append(
                    "Public statements, accepted commitments and confirmed engine outcomes: [commitment ledger](../communication/ledger.json).\n"
                )
    files.update({name: "\n".join(parts) for name, parts in groups.items() if parts})
    return files


def export_files(root, workspace, files):
    private_directory(workspace)
    manifest = root / "generated-files.json"
    previous = set(json.loads(manifest.read_text())) if manifest.exists() else set()
    planned = set(files)
    # Write-ahead list lets a restart clean up partially written projections.
    write_private_json(manifest, sorted(previous | planned))
    for name in previous | planned:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Invalid projection manifest")
        parent = workspace
        for part in relative.parts[:-1]:
            parent = private_directory(parent / part)
        path = workspace / name
        if name in files:
            write_private(path, files[name].encode())
        elif path.is_symlink():
            raise ValueError("Unsafe projection path")
        elif path.exists():
            path.unlink()
    write_private_json(manifest, sorted(planned))


def notebook_files(db):
    """Build an explicit report using only the supplied database snapshot."""
    events = [
        json.loads(value)
        for value in db.scalars(
            select(tables.events.c.body).select_from(tables.events.join(tables.inbox)).order_by(tables.events.c.id)
        )
    ]
    files = projection_files(RecordStore(db).all(), events)
    groups = {}
    for row in db.execute(
        select(tables.jobs.c.id, tables.jobs.c.game_turn, tables.jobs.c.purpose).select_from(tables.active_jobs)
    ).mappings():
        target = (
            (row["game_turn"], None)
            if row["purpose"] == "gameplay" and row["game_turn"] is not None
            else (None, row["id"])
        )
        groups.setdefault(target, [])
    for request in db.execute(
        select(tables.outbox, tables.jobs.c.game_turn, tables.jobs.c.purpose)
        .select_from(tables.live_requests.join(tables.jobs, tables.outbox.c.job == tables.jobs.c.id))
        .order_by(tables.request_order)
    ).mappings():
        target = (
            (request["game_turn"], None)
            if request["purpose"] == "gameplay" and request["game_turn"] is not None
            else (None, request["job"])
        )
        groups.setdefault(target, []).append(request)
    reader = SummaryReader(db, list(groups)) if groups else None
    for (turn, job), requests in groups.items():
        assert reader is not None
        directory = f"turn_{turn}" if turn is not None else f"communication/{job}"
        summary = reader.build(turn, job)
        files[directory + "/receipts.jsonl"] = "".join(
            encode(
                {
                    "request": json.loads(row["payload"]),
                    "receipt": model_view(json.loads(row["result"])) if row["result"] else None,
                    "decision": row["decision"],
                }
            )
            + "\n"
            for row in requests
        )
        files[directory + "/summary.json"] = json.dumps(model_view(summary), ensure_ascii=False, indent=2) + "\n"
        files[directory + "/summary.md"] = summary_markdown(summary)
    return files
