"""Disposable human-readable projections, with a trusted manifest for cleanup."""

import json
import re
from pathlib import Path

from civ_agent.memory_records import MEMORY_TYPES, record_text
from civ_agent.memory_store import encode
from civ_agent.tls import private_directory, write_private, write_private_json


def render_record(key, record):
    body = record["body"]
    lines = [f"## {key} — {record['category']}", record_text(body)]
    for field in ("approach", "successCriteria", "reviewAfterTurn", "uncertainty"):
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
    groups = {"SOUL.md": [], "STRATEGY.md": [], "economy.md": []}
    for key, record in active.items():
        rendered = render_record(key, record)
        category = record["category"]
        if category == "soul":
            groups["SOUL.md"].append(rendered)
        if category == "plan":
            groups["STRATEGY.md"].append(rendered)
        if category == "note":
            files[f"notes/{key}.md"] = rendered
        for subject in record["body"].get("subjects", []):
            if subject == "economy":
                groups["economy.md"].append(rendered)
            elif re.fullmatch(r"[A-Z][A-Za-z0-9_ -]{0,100}", subject):
                groups.setdefault(f"relationships/{subject}.md", []).append(rendered)
    social = records.get("observation:communication", {}).get("body", {})
    ledger = {key: social.get(key, []) for key in ("offers", "consents", "outcomes", "trades")}
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
