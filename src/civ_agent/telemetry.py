"""Service-owned accounting, outside both model workspaces and game rollback snapshots."""

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from civ_agent.protocol import JsonObject
from civ_agent.tls import private_directory

TOKEN_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
TERMINAL = {"COMPLETED", "FAILED", "INTERRUPTED"}


class JobAccounting:
    def __init__(self, directory: Path):
        self.path = private_directory(directory) / "accounting.sqlite3"
        if self.path.is_symlink():
            raise ValueError("Unsafe accounting database")
        self.starts: dict[str, float] = {}
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, payload TEXT NOT NULL, sent INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS calls(id TEXT PRIMARY KEY, job TEXT NOT NULL, keys TEXT NOT NULL,
                    started REAL NOT NULL, outcome TEXT, replayed INTEGER NOT NULL DEFAULT 0, elapsed INTEGER);
            """)
            db.execute("UPDATE calls SET outcome='incomplete',elapsed=0 WHERE outcome IS NULL")
            recovered = []
            # Work lost on service restart is still paid work. Retain the last known counters.
            for id, payload in db.execute("SELECT id,payload FROM jobs").fetchall():
                row = json.loads(payload)
                if row["telemetry"]["phase"] not in TERMINAL:
                    row["telemetry"].update(phase="FAILED", endedAtMs=int(time.time() * 1000), usageComplete=False)
                    row["telemetry"]["sequence"] += 1
                    db.execute("UPDATE jobs SET payload=?,sent=0 WHERE id=?", (json.dumps(row), id))
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
        db = sqlite3.connect(self.path)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

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
            db.execute("INSERT OR IGNORE INTO jobs VALUES(?,?,0)", (id, json.dumps(row)))
        self.starts[id] = time.monotonic()

    def begin_call(self, job: str, name: str, arguments: JsonObject) -> str:
        id = uuid.uuid4().hex
        keys = ["tool:" + str(name)[:128]]
        if name == "player_command" and isinstance(arguments, dict) and isinstance(arguments.get("kind"), str):
            keys.append("command:" + arguments["kind"][:128])
        with self.connect() as db:
            db.execute(
                "INSERT INTO calls(id,job,keys,started) VALUES(?,?,?,?)", (id, job, json.dumps(keys), time.monotonic())
            )
        return id

    def end_call(self, id: str, outcome: str, replayed: bool):
        with self.connect() as db:
            started = db.execute("SELECT started FROM calls WHERE id=?", (id,)).fetchone()[0]
            db.execute(
                "UPDATE calls SET outcome=?,replayed=?,elapsed=? WHERE id=?",
                (outcome, int(replayed), max(0, int((time.monotonic() - started) * 1000)), id),
            )

    def sample(self, record: JsonObject, wait_seconds: float = 0):
        id = record["activationId"]
        with self.connect() as db:
            saved = db.execute("SELECT payload FROM jobs WHERE id=?", (id,)).fetchone()
            if saved is None:
                return
            row = json.loads(saved[0])
            report = row["telemetry"]
            if report["phase"] in TERMINAL and record["phase"] not in TERMINAL:
                return
            report["sequence"] += 1
            terminal = report["phase"] in TERMINAL
            report["phase"] = record["phase"]
            if id in self.starts and not terminal:
                report["elapsedMs"] = max(report["elapsedMs"], int((time.monotonic() - self.starts[id]) * 1000))
            report["negotiationWaitMs"] = max(report["negotiationWaitMs"], int(wait_seconds * 1000))
            settings = record.get("effectiveSettings", record.get("settings", {}))
            report.update(model=settings.get("model"), reasoningEffort=settings.get("reasoningEffort"))
            report["usageReported"] = bool(record.get("usageReported", False))
            report["usageComplete"] = report["usageReported"] and bool(record.get("usageComplete", False))
            if record["phase"] in TERMINAL:
                report["endedAtMs"] = report["endedAtMs"] or int(time.time() * 1000)
            for key in TOKEN_KEYS:
                row["usage"][key] = max(row["usage"][key], record.get("usage", {}).get(key, 0))
            calls: JsonObject = {}
            for keys, outcome, replayed, elapsed in db.execute(
                "SELECT keys,outcome,replayed,elapsed FROM calls WHERE job=?", (id,)
            ):
                for key in json.loads(keys):
                    count = calls.setdefault(
                        key, dict.fromkeys(("calls", "succeeded", "errors", "replayed", "incomplete", "elapsedMs"), 0)
                    )
                    count["calls"] += 1
                    count[{"success": "succeeded", "error": "errors"}.get(outcome, "incomplete")] += 1
                    count["replayed"] += replayed
                    count["elapsedMs"] += elapsed or 0
            report["apiCalls"] = calls
            db.execute("UPDATE jobs SET payload=?,sent=0 WHERE id=?", (json.dumps(row), id))

    def pending(self) -> list[JsonObject]:
        with self.connect() as db:
            return [
                {"job": id, **json.loads(payload)}
                for id, payload in db.execute("SELECT id,payload FROM jobs WHERE sent=0")
                if json.loads(payload)["claimed"]
            ]

    def acknowledge(self, id: str, sequence: int):
        with self.connect() as db:
            saved = db.execute("SELECT payload FROM jobs WHERE id=?", (id,)).fetchone()
            if saved and json.loads(saved[0])["telemetry"]["sequence"] == sequence:
                db.execute("UPDATE jobs SET sent=1 WHERE id=?", (id,))
