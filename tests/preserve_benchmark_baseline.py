"""Freeze benchmark evidence without opening a player service or changing source games.

The private bundle contains credentials and player memories. Keep it outside Git.
SQLite's online backup includes the WAL and retains old record versions; the
copied recovery manifest, not the database's latest heads, defines the boundary.
"""

import argparse
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from civ_agent import memory_schema as tables
from civ_agent.civilization_state import CivilizationState
from civ_agent.monitoring import path_for, read_record
from civ_agent.sqlite_database import sqlite_engine
from civ_agent.tls import write_private_json
from civ_agent.turn_archive import civilization_directory_name


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def private_bytes(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("xb") as stream:
        path.chmod(0o600)
        stream.write(content)


def backup_database(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    private_bytes(destination, b"")
    engine = sqlite_engine(source, read_only=True)
    try:
        with engine.connect() as db, closing(sqlite3.connect(destination)) as target:
            # Online backup is a DBAPI operation, not a query or writable source connection.
            connection = db.connection.driver_connection
            assert connection is not None
            connection.backup(target)
    finally:
        engine.dispose()
    return digest(destination)


def preserve_game(source, destination):
    checkpoint_bytes = (source / "turn-recovery/current.json").read_bytes()
    checkpoint = json.loads(checkpoint_bytes)
    private_bytes(destination / "checkpoint.json", checkpoint_bytes)
    private_bytes(destination / "game.json", checkpoint["host"]["gameJson"].strip().encode() + b"\n")
    config = json.loads((source / "resolved.json").read_text())
    container = json.loads((source / "container.json").read_text())
    result = read_record(path_for(source), "state", "result") or {}
    state = read_record(path_for(source), "state", "run") or {}
    players = {}
    for name, reference in checkpoint["players"].items():
        slug = civilization_directory_name(name)
        player_source, player_output = source / "agent-services" / slug, destination / "players" / slug
        candidates = [
            p
            for directory in ("turn-recovery", "abandoned")
            for p in (player_source / directory).rglob(reference["id"] + ".json")
        ]
        matching = next((p for p in candidates if digest(p) == reference["sha256"]), None)
        if matching is None:
            raise ValueError(f"Missing matching checkpoint for {source.name}/{name}; retained partial bundle")
        saved = json.loads(matching.read_text())
        private_bytes(player_output / "checkpoint.json", matching.read_bytes())
        database_hash = backup_database(player_source / "civilization/memory.sqlite3", player_output / "memory.sqlite3")
        engine = sqlite_engine(player_output / "memory.sqlite3", read_only=True)
        try:
            with engine.connect() as db, db.begin():
                store_id = json.loads(db.scalar(select(tables.meta.c.value).where(tables.meta.c.key == "storeId")))
                verified = (
                    store_id == saved["storeId"]
                    and CivilizationState._history_digest(db, saved) == saved["historySha256"]
                )
        finally:
            engine.dispose()
        activations = []
        for path in sorted((player_source / "activations").glob("*.json")):
            row = json.loads(path.read_text())
            if row.get("gameTurn", checkpoint["turn"] + 1) <= checkpoint["turn"]:
                activations.append(
                    {
                        key: row[key]
                        for key in (
                            "activationId",
                            "civilization",
                            "epoch",
                            "gameTurn",
                            "handoff",
                            "phase",
                            "modelPhase",
                            "settings",
                            "effectiveSettings",
                            "startup",
                            "usage",
                            "usageComplete",
                            "error",
                        )
                        if key in row
                    }
                )
        write_private_json(player_output / "activations.json", activations)
        players[name] = {
            "checkpointSha256": reference["sha256"],
            "memorySha256": database_hash,
            "historyVerified": verified,
            "format": json.loads(dict(saved["meta"])["format"]),
            "activationEvidence": len(activations),
            "activationSha256": digest(player_output / "activations.json"),
        }
        print(f"Preserved {source.name}/{name}: boundary history verified={verified}", flush=True)
    monitoring_hash = backup_database(path_for(source), destination / "monitoring.sqlite3")
    manifest = {
        "source": str(source.resolve()),
        "capturedAt": datetime.now(UTC).isoformat(),
        "turn": checkpoint["turn"],
        "handoff": checkpoint["handoff"],
        "epoch": checkpoint["epoch"],
        "checkpointSha256": digest(destination / "checkpoint.json"),
        "saveSha256": digest(destination / "game.json"),
        "monitoringSha256": monitoring_hash,
        "images": container.get("images"),
        "config": {key: config[key] for key in ("game", "agents", "assignments")},
        "hostRevision": result.get("hostRevision"),
        "engineRevision": result.get("engineRevision"),
        "runner": {key: state.get(key) for key in ("status", "reason", "initialTurn", "handoffs")},
        "players": players,
        "limitations": [
            "Checkpoint fixes the board and recovery heads. Databases may contain later records; use the copied checkpoint to select history.",
            "Telemetry and activation files were captured after the boundary, not atomically with it. Filter by epoch, handoff and recorded interval.",
            "Original format and images are retained. This bundle is not a new-format player-memory seed or evidence of baseline variance.",
        ],
    }
    write_private_json(destination / "manifest.json", manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--games", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--game", action="append", default=None)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Use a new bundle directory; previous evidence is never overwritten")
    manifests = {}
    for name in args.game or ["g0001", "g0002", "g0003", "g0004"]:
        if Path(name).name != name:
            parser.error("Game must be a directory name")
        manifests[name] = preserve_game(args.games / name, args.output / name)
    write_private_json(
        args.output / "manifest.json",
        {
            "formatVersion": 1,
            "capturedAt": datetime.now(UTC).isoformat(),
            "games": manifests,
        },
    )
    return 0 if all(p["historyVerified"] for m in manifests.values() for p in m["players"].values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
