"""Service-owned accounting, outside both model workspaces and game rollback snapshots."""

import json
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import REAL, Column, Integer, MetaData, Table, Text, insert, select

from civ_agent.protocol import JsonObject
from civ_agent.sqlite_database import sqlite_engine
from civ_agent.tls import private_directory

TOKEN_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
TERMINAL = {"COMPLETED", "FAILED", "INTERRUPTED"}

metadata = MetaData()
jobs = Table(
    "jobs",
    metadata,
    Column("id", Text, primary_key=True, nullable=True),
    Column("payload", Text, nullable=False),
    Column("sent", Integer, nullable=False),
)
calls = Table(
    "calls",
    metadata,
    Column("id", Text, primary_key=True, nullable=True),
    Column("job", Text, nullable=False),
    Column("keys", Text, nullable=False),
    Column("started", REAL, nullable=False),
    Column("outcome", Text),
    Column("replayed", Integer, nullable=False, server_default="0"),
    Column("elapsed", Integer),
)


class JobAccounting:
    def __init__(self, directory: Path, memory_edits: Callable[[str], JsonObject] | None = None):
        self.path = private_directory(directory) / "accounting.sqlite3"
        if self.path.is_symlink():
            raise ValueError("Unsafe accounting database")
        self.engine = sqlite_engine(self.path)
        self.memory_edits = memory_edits
        self.starts: dict[str, float] = {}
        with self.connect() as db:
            metadata.create_all(db)
            db.execute(calls.update().where(calls.c.outcome.is_(None)).values(outcome="incomplete", elapsed=0))
            recovered = []
            # Work lost on service restart is still paid work. Retain the last known counters.
            for id, payload in db.execute(select(jobs.c.id, jobs.c.payload)).all():
                row = json.loads(payload)
                if row["telemetry"]["phase"] not in TERMINAL:
                    row["telemetry"].update(phase="FAILED", endedAtMs=int(time.time() * 1000), usageComplete=False)
                    row["telemetry"]["sequence"] += 1
                    db.execute(jobs.update().where(jobs.c.id == id).values(payload=json.dumps(row), sent=0))
                    recovered.append(
                        {
                            "activationId": id,
                            "phase": "FAILED",
                            "usage": row["usage"],
                            "usageReported": row["telemetry"]["usageReported"],
                            "settings": {
                                "model": row["telemetry"]["model"],
                                "reasoningEffort": row["telemetry"]["reasoningEffort"],
                            },
                        }
                    )
        self.path.chmod(0o600)
        for record in recovered:
            self.sample(record)

    @contextmanager
    def connect(self):
        with self.engine.begin() as db:
            yield db

    def start(self, id: str, epoch: str, claimed: bool):
        report: JsonObject = {
            "sequence": 0,
            "phase": "STARTING",
            "startedAtMs": int(time.time() * 1000),
            "endedAtMs": None,
            "elapsedMs": 0,
            "negotiationWaitMs": 0,
            "usageReported": False,
            "usageComplete": False,
            "model": None,
            "reasoningEffort": None,
            "apiCalls": {},
        }
        row = {"epoch": epoch, "claimed": claimed, "usage": dict.fromkeys(TOKEN_KEYS, 0), "telemetry": report}
        with self.connect() as db:
            db.execute(insert(jobs).values(id=id, payload=json.dumps(row), sent=0).prefix_with("OR IGNORE"))
        self.starts[id] = time.monotonic()

    def begin_call(self, job: str, name: str, arguments: JsonObject, *, source: str = "tool") -> str:
        id = uuid.uuid4().hex
        if source not in {"tool", "startup"}:
            raise ValueError("Invalid call source")
        keys = [source + ":" + str(name)[:128]]
        if name == "player_command" and isinstance(arguments, dict) and isinstance(arguments.get("kind"), str):
            keys.append("command:" + arguments["kind"][:128])
        with self.connect() as db:
            db.execute(insert(calls).values(id=id, job=job, keys=json.dumps(keys), started=time.monotonic()))
        return id

    def end_call(self, id: str, outcome: str, replayed: bool):
        with self.connect() as db:
            started = db.execute(select(calls.c.started).where(calls.c.id == id)).scalar_one()
            db.execute(
                calls.update()
                .where(calls.c.id == id)
                .values(
                    outcome=outcome, replayed=int(replayed), elapsed=max(0, int((time.monotonic() - started) * 1000))
                )
            )

    def sample(self, record: JsonObject, wait_seconds: float = 0):
        id = record["activationId"]
        # Read the durable memory journal, not the transport outcome: one
        # successful request can contain rejected edits, and replies can be lost.
        edits = self.memory_edits(id) if self.memory_edits else None
        with self.connect() as db:
            saved = db.execute(select(jobs.c.payload).where(jobs.c.id == id)).fetchone()
            if saved is None:
                return
            row = json.loads(saved[0])
            report = row["telemetry"]
            if report["phase"] in TERMINAL and record["phase"] not in TERMINAL:
                return
            report["sequence"] += 1
            terminal = report["phase"] in TERMINAL
            # A rewind can restore an older activation record, but accounting
            # describes the work that actually ran. Completion evidence survives.
            if not report["usageComplete"]:
                report["phase"] = record["phase"]
            if id in self.starts and not terminal:
                report["elapsedMs"] = max(report["elapsedMs"], int((time.monotonic() - self.starts[id]) * 1000))
            report["negotiationWaitMs"] = max(report["negotiationWaitMs"], int(wait_seconds * 1000))
            settings = record.get("effectiveSettings", record.get("settings", {}))
            report.update(model=settings.get("model"), reasoningEffort=settings.get("reasoningEffort"))
            report["usageReported"] = report["usageReported"] or bool(record.get("usageReported", False))
            report["usageComplete"] = report["usageComplete"] or (
                bool(record.get("usageReported", False)) and bool(record.get("usageComplete", False))
            )
            for field in ("gameplayElapsedMs", "finalizationElapsedMs"):
                if field in record:
                    report[field] = max(report.get(field, 0), record[field])
            if record["phase"] in TERMINAL:
                report["endedAtMs"] = report["endedAtMs"] or int(time.time() * 1000)
            for key in TOKEN_KEYS:
                row["usage"][key] = max(row["usage"][key], record.get("usage", {}).get(key, 0))
            counts: JsonObject = {}
            for keys, outcome, replayed, elapsed in db.execute(
                select(calls.c["keys"], calls.c.outcome, calls.c.replayed, calls.c.elapsed).where(calls.c.job == id)
            ):
                for key in json.loads(keys):
                    count = counts.setdefault(
                        key, dict.fromkeys(("calls", "succeeded", "errors", "replayed", "incomplete", "elapsedMs"), 0)
                    )
                    count["calls"] += 1
                    count[{"success": "succeeded", "error": "errors"}.get(outcome, "incomplete")] += 1
                    count["replayed"] += replayed
                    count["elapsedMs"] += elapsed or 0
            report["apiCalls"] = counts
            if edits is not None:
                report["memoryEdits"] = edits
            db.execute(jobs.update().where(jobs.c.id == id).values(payload=json.dumps(row), sent=0))

    def pending(self) -> list[JsonObject]:
        with self.connect() as db:
            return [
                {"job": id, **json.loads(payload)}
                for id, payload in db.execute(select(jobs.c.id, jobs.c.payload).where(jobs.c.sent == 0))
                if json.loads(payload)["claimed"]
            ]

    def acknowledge(self, id: str, sequence: int):
        with self.connect() as db:
            saved = db.execute(select(jobs.c.payload).where(jobs.c.id == id)).fetchone()
            if saved and json.loads(saved[0])["telemetry"]["sequence"] == sequence:
                db.execute(jobs.update().where(jobs.c.id == id).values(sent=1))
