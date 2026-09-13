"""Transactional private memory. Markdown is an export, never an input authority.

All methods are short synchronous transactions, called on ServiceIO's worker.
Model computation and HTTPS requests happen outside these transactions.
"""

import hashlib
import json
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from civ_agent.protocol import JsonObject
from civ_agent.tls import private_directory, write_private, write_private_json


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class StateConflict(ValueError):
    pass


class StaleAssumptions(StateConflict):
    """A local pre-dispatch rejection; the affected evidence must be read again."""

    def __init__(self, keys: list[str]):
        super().__init__("STALE_ASSUMPTIONS")
        self.keys = sorted(keys)


class CivilizationState:
    def __init__(self, root: Path, civilization: str):
        self.root = private_directory(root)
        self.path = self.root / "memory.sqlite3"
        self.civilization = civilization
        if self.path.is_symlink():
            raise ValueError("Unsafe memory path")
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY, source TEXT UNIQUE NOT NULL,
                    kind TEXT NOT NULL, body TEXT NOT NULL, wake INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS records(
                    key TEXT NOT NULL, version INTEGER NOT NULL, category TEXT NOT NULL,
                    body TEXT NOT NULL, PRIMARY KEY(key,version));
                CREATE TABLE IF NOT EXISTS jobs(
                    id TEXT PRIMARY KEY, epoch TEXT NOT NULL, purpose TEXT NOT NULL,
                    snapshot TEXT NOT NULL, settings TEXT NOT NULL, status TEXT NOT NULL, game_turn INTEGER);
                CREATE TABLE IF NOT EXISTS outbox(
                    id TEXT PRIMARY KEY, job TEXT NOT NULL, transport TEXT NOT NULL,
                    payload TEXT NOT NULL, dependencies TEXT NOT NULL, result TEXT);
                CREATE TABLE IF NOT EXISTS decisions(
                    id INTEGER PRIMARY KEY, job TEXT, outcome TEXT NOT NULL, detail TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS calls(job TEXT, id TEXT, name TEXT NOT NULL,
                    arguments TEXT NOT NULL, result TEXT, PRIMARY KEY(job,id));
                CREATE TABLE IF NOT EXISTS heads(key TEXT PRIMARY KEY, version INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS inbox(id INTEGER PRIMARY KEY REFERENCES events(id));
                CREATE TABLE IF NOT EXISTS outgoing(id TEXT PRIMARY KEY REFERENCES outbox(id));
            """)
            existing = self._meta(db, "civilization")
            if existing is not None and existing != civilization:
                raise StateConflict("CIVILIZATION_CONFLICT")
            self._set(db, "civilization", civilization)
            if self._meta(db, "storeId") is None:
                self._set(db, "storeId", secrets.token_hex(16))
            # A process restart never resumes a model's uncommitted computation.
            db.execute("UPDATE jobs SET status='INTERRUPTED' WHERE status='RUNNING'")
        self.path.chmod(0o600)

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _meta(db, key, default=None) -> Any:
        row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def _set(db, key, value):
        db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, encode(value)))

    def bind(self, epoch: str):
        with self.transaction() as db:
            if self._meta(db, "epoch", epoch) != epoch:
                raise StateConflict("STALE_EPOCH")
            self._set(db, "epoch", epoch)

    def cursor(self) -> int:
        with self.transaction() as db:
            return self._meta(db, "cursor", 0)

    def ingest(self, epoch: str, events: list[JsonObject], cursor: int):
        """Commit authenticated events and their reconnect cursor together."""
        with self.transaction() as db:
            if self._meta(db, "paused", False):
                raise StateConflict("STATE_PAUSED")
            if self._meta(db, "epoch") != epoch:
                raise StateConflict("STALE_EPOCH")
            for event in events:
                source = epoch + ":" + str(event["id"])
                db.execute(
                    "INSERT OR IGNORE INTO events(source,kind,body,wake) VALUES (?,?,?,?)",
                    (source, event["kind"], encode(event), bool(event.get("wake"))),
                )
                db.execute("INSERT OR IGNORE INTO inbox SELECT id FROM events WHERE source=?", (source,))
                # Preserve an utterance as an authenticated statement, never a
                # world observation inferred from that utterance's content.
                self._record(
                    db, "event:" + str(event["id"]), "observation" if event["kind"] == "engine" else "statement", event
                )
            self._set(db, "cursor", max(cursor, self._meta(db, "cursor", 0)))

    @staticmethod
    def _records(db) -> JsonObject:
        return {
            row["key"]: {"version": row["version"], "category": row["category"], "body": json.loads(row["body"])}
            for row in db.execute("SELECT r.* FROM records r JOIN heads h ON r.key=h.key AND r.version=h.version")
        }

    @staticmethod
    def _record(db, key, category, body):
        prior = db.execute(
            "SELECT version,category,body FROM records WHERE key=? ORDER BY version DESC LIMIT 1", (key,)
        ).fetchone()
        if prior and prior["body"] == encode(body) and prior["category"] == category:
            db.execute("INSERT OR REPLACE INTO heads VALUES (?,?)", (key, prior["version"]))
            return prior["version"]
        version = prior["version"] + 1 if prior else 1
        db.execute("INSERT INTO records VALUES (?,?,?,?)", (key, version, category, encode(body)))
        db.execute("INSERT OR REPLACE INTO heads VALUES (?,?)", (key, version))
        return version

    def observe(self, key: str, body: JsonObject, job: str | None = None):
        with self.transaction() as db:
            if self._meta(db, "paused", False):
                raise StateConflict("STATE_PAUSED")
            if job is not None:
                self._validate(db, job, {})
            key = "observation:" + key
            version = self._record(db, key, "observation", body)
            if job is not None:
                snapshot = json.loads(db.execute("SELECT snapshot FROM jobs WHERE id=?", (job,)).fetchone()[0])
                snapshot["records"][key] = {"version": version, "category": "observation", "body": body}
                db.execute("UPDATE jobs SET snapshot=? WHERE id=?", (encode(snapshot), job))
            return version

    def snapshot(self, purpose: str) -> JsonObject:
        with self.transaction() as db:
            records = self._records(db)
            cursor = self._meta(db, "handled:" + purpose, 0)
            events = [
                json.loads(row["body"])
                for row in db.execute(
                    "SELECT body FROM events JOIN inbox USING(id) WHERE id>? ORDER BY id LIMIT 50", (cursor,)
                )
            ]
            high = db.execute(
                "SELECT COALESCE(MAX(id),0) FROM (SELECT id FROM inbox WHERE id>? ORDER BY id LIMIT 50)", (cursor,)
            ).fetchone()[0]
            return {
                "epoch": self._meta(db, "epoch"),
                "purpose": purpose,
                "records": records,
                "events": events,
                "cursor": max(cursor, high),
            }

    def observe_player(self, body: JsonObject, job: str):
        """Independent evidence versions; the host still validates the full view too."""
        scopes = {
            "game": body,
            "turn": {key: body.get(key) for key in ("epoch", "turn", "phase", "pending", "alerts")},
            "economy": {"turn": body.get("turn"), "economy": body.get("economy")},
            "research": {
                key: body.get(key)
                for key in ("turn", "research", "researchQueue", "researchOptions", "freeTechnologies")
            },
            "policy": {key: body.get(key) for key in ("turn", "policyOptions", "freePolicies")},
        }
        for kind, entities in (("unit", body.get("map", {}).get("units", [])), ("city", body.get("cities", []))):
            for entity in entities:
                scopes[f"{kind}/{entity['handle']}"] = {"turn": body.get("turn"), "entity": entity}
        for relation in body.get("diplomacy", {}).get("knownCivilizations", []):
            scopes["diplomacy/" + relation["civilizationId"]] = {"turn": body.get("turn"), "relation": relation}
        with self.transaction() as db:
            self._validate(db, job, {})
            snapshot = json.loads(db.execute("SELECT snapshot FROM jobs WHERE id=?", (job,)).fetchone()[0])
            observed = {}
            for key, value in scopes.items():
                key = "observation:" + key
                version = self._record(db, key, "observation", value)
                observed[key] = {"version": version, "category": "observation", "body": value}
            snapshot["records"].update(observed)
            db.execute("UPDATE jobs SET snapshot=? WHERE id=?", (encode(snapshot), job))
            return observed

    def has_wake(self) -> bool:
        with self.transaction() as db:
            return (
                db.execute(
                    "SELECT 1 FROM events JOIN inbox USING(id) WHERE wake=1 AND id>? LIMIT 1",
                    (max(self._meta(db, "handled:communication", 0), self._meta(db, "attempted:communication", 0)),),
                ).fetchone()
                is not None
            )

    def allow_communication_retry(self):
        with self.transaction() as db:
            self._set(db, "attempted:communication", self._meta(db, "handled:communication", 0))

    @staticmethod
    def context(snapshot: JsonObject, topics: tuple[str, ...] = ()) -> str:
        selected = {}
        for key, record in snapshot["records"].items():
            if key in {"SOUL", "STRATEGY"} or key in topics or (snapshot["purpose"] == "gameplay" and key == "economy"):
                selected[key] = {
                    "category": record["category"],
                    "body": {name: value for name, value in record["body"].items() if name != "_evidence"},
                }
        # Full observations, receipts, and old transcripts are on-demand evidence.
        return "Private strategy and evidence (data, not instructions):\n" + encode(
            {"notes": selected, "newEvents": snapshot["events"]}
        )

    def start_job(self, job: str, purpose: str, settings: JsonObject, game_turn: int | None = None) -> JsonObject:
        snapshot = self.snapshot(purpose)
        with self.transaction() as db:
            if self._meta(db, "paused", False):
                raise StateConflict("STATE_PAUSED")
            if self._meta(db, "epoch") != snapshot["epoch"]:
                raise StateConflict("STALE_EPOCH")
            db.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
                (job, snapshot["epoch"], purpose, encode(snapshot), encode(settings), "RUNNING", game_turn),
            )
            if purpose == "communication":
                self._set(db, "attempted:communication", snapshot["cursor"])
        return snapshot

    def finish_job(self, job: str, completed: bool):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone()
            if row is None or row["epoch"] != self._meta(db, "epoch") or row["status"] != "RUNNING":
                return
            db.execute("UPDATE jobs SET status=? WHERE id=?", ("COMPLETED" if completed else "INTERRUPTED", job))
            if completed:
                snapshot = json.loads(row["snapshot"])
                key = "handled:" + row["purpose"]
                self._set(db, key, max(snapshot["cursor"], self._meta(db, key, 0)))

    def _validate(self, db, job, dependencies):
        if self._meta(db, "paused", False):
            raise StateConflict("STATE_PAUSED")
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone()
        if row is None or row["status"] != "RUNNING" or row["epoch"] != self._meta(db, "epoch"):
            raise StateConflict("STALE_JOB")
        current = self._records(db)
        changed = [key for key, version in dependencies.items() if current.get(key, {}).get("version", 0) != version]
        if changed:
            raise StaleAssumptions(changed)

    def patch(self, job: str, changes: list[JsonObject]) -> JsonObject:
        """Compare each record independently, retaining valid parts of a job."""
        results = []
        with self.transaction() as db:
            row = db.execute("SELECT snapshot FROM jobs WHERE id=?", (job,)).fetchone()
            if not row:
                raise StateConflict("STALE_JOB")
            snapshot = json.loads(row[0])
            self._validate(db, job, {})
            for change in changes:
                key, category, body = change.get("key"), change.get("category"), change.get("body")
                if (
                    not isinstance(key, str)
                    or not key
                    or len(key) > 128
                    or category not in {"belief", "plan", "policy"}
                    or not isinstance(body, dict)
                ):
                    raise ValueError("INVALID_MEMORY_PATCH")
                if not (
                    key in {"SOUL", "STRATEGY", "economy"}
                    or key.startswith("relationships/")
                    or key.startswith("note/")
                ):
                    raise ValueError("INVALID_MEMORY_KEY")
                if len(encode(body)) > 8000 or not isinstance(body.get("text"), str):
                    raise ValueError("INVALID_MEMORY_BODY")
                if category == "belief" and (
                    not isinstance(body.get("sources"), list)
                    or not body["sources"]
                    or body.get("uncertainty") not in {"low", "medium", "high"}
                ):
                    raise ValueError("BELIEF_PROVENANCE_REQUIRED")
                if category == "belief":
                    evidence = []
                    for source in body["sources"]:
                        if not isinstance(source, str):
                            raise ValueError("BELIEF_SOURCE_NOT_OBSERVED")
                        candidates = [
                            (source_key, record)
                            for source_key, record in snapshot["records"].items()
                            if source_key == source
                            or (
                                record["category"] == "statement"
                                and record["body"].get("body", {}).get("message") == source
                            )
                        ]
                        if len(candidates) != 1:
                            raise ValueError("BELIEF_SOURCE_NOT_OBSERVED")
                        source_key, record = candidates[0]
                        evidence.append(
                            {"key": source_key, "version": record["version"], "category": record["category"]}
                        )
                    body = {**body, "_evidence": evidence}
                version = snapshot["records"].get(key, {}).get("version", 0)
                try:
                    self._validate(db, job, {key: version})
                    updated = self._record(db, key, category, body)
                    # This job may intentionally refine its own previous patch.
                    snapshot["records"][key] = {"version": updated, "category": category, "body": body}
                    results.append({"key": key, "accepted": True})
                except StateConflict:
                    results.append(
                        {"key": key, "accepted": False, "reason": "Newer committed memory; read it and reconsider."}
                    )
            db.execute("UPDATE jobs SET snapshot=? WHERE id=?", (encode(snapshot), job))
            db.execute("INSERT INTO decisions(job,outcome,detail) VALUES (?,?,?)", (job, "memory", encode(results)))
        return {"changes": results}

    def queue(
        self, job: str, transport: str, payload: JsonObject, dependencies: JsonObject, call_id: str
    ) -> JsonObject:
        """Code owns identity. Retrying one tool call reuses the exact saved request."""
        request_id = "r" + hashlib.sha256((job + ":" + call_id).encode()).hexdigest()[:24]
        with self.transaction() as db:
            self._validate(db, job, dependencies)
            previous = db.execute("SELECT * FROM outbox WHERE id=?", (request_id,)).fetchone()
            if previous:
                if previous["transport"] != transport or json.loads(previous["payload"]) != {
                    **payload,
                    "requestId": request_id,
                }:
                    raise StateConflict("CALL_CONFLICT")
                return dict(previous)
            body = {**payload, "requestId": request_id}
            db.execute(
                "INSERT INTO outbox VALUES (?,?,?,?,?,NULL)",
                (request_id, job, transport, encode(body), encode(dependencies)),
            )
            db.execute("INSERT INTO outgoing VALUES (?)", (request_id,))
            return {
                "id": request_id,
                "job": job,
                "transport": transport,
                "payload": encode(body),
                "dependencies": encode(dependencies),
                "result": None,
            }

    def pending(self):
        with self.transaction() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT o.* FROM outbox o JOIN outgoing a ON o.id=a.id WHERE result IS NULL ORDER BY o.rowid"
                )
            ]

    def requests_for_job(self, job: str):
        with self.transaction() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT o.* FROM outbox o JOIN outgoing a ON o.id=a.id WHERE job=? ORDER BY o.rowid", (job,)
                )
            ]

    def call_receipt(self, job: str, call_id: str):
        request_id = "r" + hashlib.sha256((job + ":" + call_id).encode()).hexdigest()[:24]
        with self.transaction() as db:
            self._validate(db, job, {})
            row = db.execute("SELECT result FROM outbox WHERE id=? AND job=?", (request_id, job)).fetchone()
            return json.loads(row[0]) if row and row[0] is not None else None

    def receipt(self, request_id: str, result: JsonObject):
        with self.transaction() as db:
            row = db.execute("SELECT result FROM outbox WHERE id=?", (request_id,)).fetchone()
            if row is None:
                raise StateConflict("UNKNOWN_REQUEST")
            if row[0] is not None:
                if json.loads(row[0]) != result:
                    raise StateConflict("RECEIPT_CONFLICT")
                return
            db.execute("UPDATE outbox SET result=? WHERE id=? AND result IS NULL", (encode(result), request_id))
            self._record(db, "receipt:" + request_id, "observation", result)

    def call(self, job, id, name, arguments, result=None):
        with self.transaction() as db:
            self._validate(db, job, {})
            prior = db.execute("SELECT * FROM calls WHERE job=? AND id=?", (job, id)).fetchone()
            if prior and (prior["name"] != name or prior["arguments"] != encode(arguments)):
                raise StateConflict("CALL_CONFLICT")
            db.execute("INSERT OR IGNORE INTO calls VALUES (?,?,?,?,NULL)", (job, id, name, encode(arguments)))
            if result is not None:
                db.execute("UPDATE calls SET result=? WHERE job=? AND id=?", (encode(result), job, id))
            return json.loads(prior["result"]) if prior and prior["result"] else None

    def refresh_memory(self, job, keys, cursor=None):
        with self.transaction() as db:
            self._validate(db, job, {})
            row = db.execute("SELECT snapshot FROM jobs WHERE id=?", (job,)).fetchone()
            snapshot = json.loads(row[0])
            current = self._records(db)
            chosen = {key: current[key] for key in keys if key in current}
            snapshot["records"].update(chosen)
            if cursor is not None:
                snapshot["cursor"] = max(snapshot["cursor"], cursor)
            db.execute("UPDATE jobs SET snapshot=? WHERE id=?", (encode(snapshot), job))
            return chosen

    def chat_history(self, participant=None, query=""):
        if (
            (participant is not None and not isinstance(participant, str))
            or not isinstance(query, str)
            or len(query) > 200
        ):
            raise ValueError("INVALID_ARGUMENT")
        search = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self.transaction() as db:
            rows = db.execute(
                "SELECT events.body FROM events JOIN inbox USING(id) WHERE events.body LIKE ? ESCAPE '\\' AND (? IS NULL OR EXISTS (SELECT 1 FROM json_each(events.body,'$.body.participants') WHERE value=?)) ORDER BY id DESC LIMIT 21",
                (search, participant, participant),
            ).fetchall()
            return {"events": [json.loads(row[0]) for row in reversed(rows[:20])], "hasMore": len(rows) > 20}

    def history(self, game_turn: int, offset: int = 0):
        if type(game_turn) is not int or game_turn < 0 or type(offset) is not int or offset < 0:
            raise ValueError("INVALID_ARGUMENT")
        with self.transaction() as db:
            rows = db.execute(
                "SELECT o.payload,o.result FROM outbox o JOIN outgoing a ON o.id=a.id JOIN jobs j ON o.job=j.id WHERE j.game_turn=? ORDER BY o.rowid LIMIT 21 OFFSET ?",
                (game_turn, offset),
            ).fetchall()
            return {
                "turn": game_turn,
                "actions": [
                    {"command": json.loads(row[0]), "receipt": json.loads(row[1]) if row[1] else None}
                    for row in rows[:20]
                ],
                "hasMore": len(rows) > 20,
                "nextOffset": offset + min(len(rows), 20),
            }

    def export(self, workspace: Path):
        with self.transaction() as db:
            records = self._records(db)
            events = [
                json.loads(row[0]) for row in db.execute("SELECT body FROM events JOIN inbox USING(id) ORDER BY id")
            ]
            requests = [
                dict(row)
                for row in db.execute(
                    "SELECT o.*,j.game_turn FROM outbox o JOIN outgoing a ON o.id=a.id JOIN jobs j ON o.job=j.id ORDER BY o.rowid"
                )
            ]
        receipts = {}
        for request in requests:
            key = "communication" if request["game_turn"] is None else f"turn_{request['game_turn'] + 1}"
            receipts.setdefault(key, []).append(
                {
                    "request": json.loads(request["payload"]),
                    "receipt": json.loads(request["result"]) if request["result"] else None,
                }
            )
        for directory, rows in receipts.items():
            write_private(
                workspace / directory / "receipts.jsonl", "".join(encode(row) + "\n" for row in rows).encode()
            )
        for key, record in records.items():
            if record["category"] not in {"belief", "plan", "policy"}:
                continue
            # Never turn model-supplied relationship names into filesystem paths.
            if key.startswith("relationships/"):
                name = key.split("/", 1)[1]
                if not name or any(
                    c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_- " for c in name
                ):
                    continue
                path = private_directory(workspace / "relationships") / (name + ".md")
            elif key in {"SOUL", "STRATEGY", "economy"}:
                path = workspace / (key + ".md")
            else:
                continue
            write_private(path, (record["body"]["text"] + "\n").encode())
        write_private(
            workspace / "memory-index.md",
            (
                "Private memory; use memory_read for records.\n\n"
                + "\n".join(
                    f"- {key}: {value['category']}"
                    for key, value in records.items()
                    if value["category"] in {"belief", "plan", "policy"}
                )
                + "\n"
            ).encode(),
        )
        write_private(workspace / "chat.jsonl", "".join(encode(event) + "\n" for event in events).encode())

    def backup(self, destination: Path) -> str:
        if destination.exists():
            raise StateConflict("CHECKPOINT_EXISTS")
        private_directory(destination.parent)
        with self.transaction() as db:
            snapshot = {
                "civilization": self.civilization,
                "storeId": self._meta(db, "storeId"),
                "meta": [list(row) for row in db.execute("SELECT * FROM meta ORDER BY key")],
                "heads": [list(row) for row in db.execute("SELECT * FROM heads ORDER BY key")],
                "inbox": [row[0] for row in db.execute("SELECT id FROM inbox ORDER BY id")],
                "outgoing": [row[0] for row in db.execute("SELECT id FROM outgoing ORDER BY id")],
            }
            snapshot["historySha256"] = self._history_digest(db, snapshot)
        write_private_json(destination, snapshot)
        return hashlib.sha256(destination.read_bytes()).hexdigest()

    @staticmethod
    def _history_digest(db, saved):
        """Verify referenced immutable evidence without copying it into every round."""
        checksum = hashlib.sha256()
        queries = (
            ("records", saved["heads"], "SELECT * FROM records WHERE key=? AND version=?"),
            ("events", [(id,) for id in saved["inbox"]], "SELECT * FROM events WHERE id=?"),
            (
                "outbox",
                [(id,) for id in saved["outgoing"]],
                "SELECT o.*,j.epoch,j.purpose,j.game_turn FROM outbox o JOIN jobs j ON j.id=o.job WHERE o.id=?",
            ),
        )
        for table, references, query in queries:
            for reference in references:
                row = db.execute(query, reference).fetchone()
                if row is None:
                    raise StateConflict("CHECKPOINT_HISTORY_MISSING")
                if table == "outbox" and row["result"] is None:
                    raise StateConflict("CHECKPOINT_REQUEST_UNSETTLED")
                checksum.update((encode([table, dict(row)]) + "\n").encode())
        return checksum.hexdigest()

    def verify(self, source: Path, checksum: str):
        if source.is_symlink() or hashlib.sha256(source.read_bytes()).hexdigest() != checksum:
            raise StateConflict("CHECKPOINT_INVALID")
        saved = json.loads(source.read_text())
        with self.transaction() as db:
            if saved["civilization"] != self.civilization or saved["storeId"] != self._meta(db, "storeId"):
                raise StateConflict("CHECKPOINT_INVALID")
            if saved["historySha256"] != self._history_digest(db, saved):
                raise StateConflict("CHECKPOINT_HISTORY_CHANGED")
        return saved

    def fence(self, epoch: str):
        with self.transaction() as db:
            self._set(db, "epoch", epoch)
            db.execute("UPDATE jobs SET status='INTERRUPTED' WHERE status='RUNNING'")

    def pause(self, paused: bool):
        with self.transaction() as db:
            self._set(db, "paused", paused)

    def is_paused(self) -> bool:
        with self.transaction() as db:
            return self._meta(db, "paused", False)

    def restore(self, source: Path, checksum: str, epoch: str):
        saved = self.verify(source, checksum)
        with self.transaction() as db:
            for table in ("meta", "heads", "inbox", "outgoing"):
                db.execute("DELETE FROM " + table)
            db.executemany("INSERT INTO meta VALUES (?,?)", saved["meta"])
            db.executemany("INSERT INTO heads VALUES (?,?)", saved["heads"])
            db.executemany("INSERT INTO inbox VALUES (?)", [(id,) for id in saved["inbox"]])
            db.executemany("INSERT INTO outgoing VALUES (?)", [(id,) for id in saved["outgoing"]])
            self._set(db, "epoch", epoch)
            self._set(db, "paused", True)
            db.execute("UPDATE jobs SET status='INTERRUPTED' WHERE status='RUNNING'")


def job_id() -> str:
    return "job-" + secrets.token_urlsafe(12)
