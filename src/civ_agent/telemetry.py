"""Service-owned accounting, outside both model workspaces and game rollback snapshots."""

import json
import math
import re
import time
import uuid
from collections import Counter
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from statistics import median

from sqlalchemy import REAL, Column, Integer, MetaData, Table, Text, insert, select

from civ_agent.diplomacy_decisions import (
    CHOICES,
    POLICY_VERSION,
    QUESTION_CHOICES,
    two_question_answers,
)
from civ_agent.protocol import JsonObject
from civ_agent.sqlite_database import sqlite_engine
from civ_agent.tls import private_directory

TOKEN_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
TERMINAL = {"COMPLETED", "FAILED", "INTERRUPTED"}


def _probability(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def _label(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._:/-]{1,128}", value) else None


def _hash(value):
    return value if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) else None


def _counters(value, keys):
    return (
        {key: value[key] for key in keys if type(value.get(key)) is int and value[key] >= 0}
        if isinstance(value, dict)
        else {}
    )


ADVISORY_CHOICES = {
    "retrieval_include": {"include", "omit"},
    "retrieval_priority": {"high", "normal"},
    "preservation": {"consider", "skip"},
    "beliefs": {"reconsider", "retain"},
    "early_review": {"review", "wait"},
    "city_focus": {"keep", "defer"},
    "city_construction": {"keep", "defer"},
}
ADVISORY_STATUSES = {"accepted", "low_confidence", "refusal", "missing", "malformed"}
ADVISORY_DELIVERY_STATUSES = {"delivered", "no_notice", "not_selected", "stale", "not_accepted"}


def _advisory_name(value):
    if value == "early_review":
        return value
    if not isinstance(value, str):
        return None
    prefix, separator, index = value.partition(":")
    return (
        value
        if separator
        and prefix in ADVISORY_CHOICES
        and prefix != "early_review"
        and index.isascii()
        and index.isdigit()
        and len(index) <= 2
        and int(index) < 64
        else None
    )


def _advisory_choice(name, value):
    if not isinstance(value, str):
        return False
    prefix = name.split(":", 1)[0]
    if value in ADVISORY_CHOICES[prefix]:
        return True
    pattern = {"city_focus": r"focus\d{1,2}", "city_construction": r"construction\d{1,2}"}.get(prefix)
    return pattern is not None and re.fullmatch(pattern, value) is not None


def _advisory_answer(answer) -> JsonObject | None:
    if not isinstance(answer, dict) or (name := _advisory_name(answer.get("name"))) is None:
        return None
    if answer.get("type") == "refusal":
        return {"name": name, "type": "refusal"}
    if (
        answer.get("type") != "choice"
        or not _advisory_choice(name, answer.get("choice"))
        or not _probability(answer.get("confidence"))
    ):
        return None
    raw: JsonObject = {"name": name, "type": "choice", "choice": answer["choice"], "confidence": answer["confidence"]}
    vector = answer.get("probabilities")
    if isinstance(vector, list) and 1 <= len(vector) <= 64:
        values = [item.get("value") for item in vector if isinstance(item, dict)]
        if (
            all(isinstance(value, str) for value in values)
            and len(values) == len(vector) == len(set(values))
            and (name.split(":", 1)[0].startswith("city_") or set(values) == ADVISORY_CHOICES[name.split(":", 1)[0]])
            and all(
                _advisory_choice(name, item.get("value")) and _probability(item.get("probability")) for item in vector
            )
            and abs(sum(item["probability"] for item in vector) - 1) <= 0.02
        ):
            raw["probabilities"] = [{"value": item["value"], "probability": item["probability"]} for item in vector]
    return raw


def _advisory_metadata(result):
    names = (
        list(dict.fromkeys(name for item in result.get("questionNames", []) if (name := _advisory_name(item))))
        if isinstance(result.get("questionNames"), list)
        else []
    )
    raw = (
        [parsed for answer in result.get("rawAnswers", []) if (parsed := _advisory_answer(answer))]
        if isinstance(result.get("rawAnswers"), list)
        else []
    )
    accepted = (
        [
            parsed
            for answer in result.get("answers", [])
            if (parsed := _advisory_answer(answer)) and parsed["type"] == "choice"
        ]
        if isinstance(result.get("answers"), list)
        else []
    )
    statuses = []
    seen = set()
    for item in result.get("questionResults", []) if isinstance(result.get("questionResults"), list) else []:
        if (
            not isinstance(item, dict)
            or (name := _advisory_name(item.get("name"))) is None
            or name in seen
            or not isinstance(item.get("status"), str)
            or item["status"] not in ADVISORY_STATUSES
        ):
            continue
        seen.add(name)
        row: JsonObject = {"name": name, "status": item["status"]}
        if _probability(item.get("threshold")):
            row["threshold"] = item["threshold"]
        answer = _advisory_answer(item.get("rawAnswer"))
        if answer is not None and answer["name"] == name:
            row["rawAnswer"] = answer
        statuses.append(row)
    tasks = {}
    for prefix in ADVISORY_CHOICES:
        selected = [row for row in statuses if row["name"].split(":", 1)[0] == prefix]
        answers = [row for row in raw if row["name"].split(":", 1)[0] == prefix]
        confidences = [row["confidence"] for row in answers if row["type"] == "choice"]
        expected = sum(name.split(":", 1)[0] == prefix for name in names)
        if not (selected or answers or expected):
            continue
        tasks[prefix] = {
            "questions": expected,
            "statuses": dict(Counter(row["status"] for row in selected)),
            "unreported": max(0, expected - len(selected)),
            "rawChoices": dict(Counter(row["choice"] for row in answers if row["type"] == "choice")),
            "rawConfidence": {
                "known": len(confidences),
                "median": median(confidences) if confidences else None,
                "min": min(confidences) if confidences else None,
                "max": max(confidences) if confidences else None,
            },
        }
    return {
        **(
            {"brokerReconciliation": result["brokerReconciliation"]}
            if type(result.get("brokerReconciliation")) is bool
            else {}
        ),
        "questionNames": names,
        "answers": accepted,
        "rawAnswers": raw,
        "questionResults": statuses,
        "byTask": tasks,
    }


def _advisory_delivery_metadata(result):
    rows = []
    for item in result.get("deliveryResults", []) if isinstance(result.get("deliveryResults"), list) else []:
        if (
            not isinstance(item, dict)
            or (name := _advisory_name(item.get("name"))) is None
            or not isinstance(item.get("status"), str)
            or item["status"] not in ADVISORY_DELIVERY_STATUSES
        ):
            continue
        row: JsonObject = {"requestId": _hash(item.get("requestId")), "name": name, "status": item["status"]}
        if _advisory_choice(name, item.get("choice")):
            row["choice"] = item["choice"]
        if isinstance(item.get("exposure"), str) and item["exposure"] in {"context", "notice", "advice", "scheduler"}:
            row["exposure"] = item["exposure"]
        rows.append(row)
    prefixes = {row["name"].split(":", 1)[0] for row in rows}
    return {
        "usage": None,
        "accounting": None,
        "deliveryResults": rows,
        "byTask": {
            prefix: {
                "questions": sum(row["name"].split(":", 1)[0] == prefix for row in rows),
                "statuses": dict(Counter(row["status"] for row in rows if row["name"].split(":", 1)[0] == prefix)),
            }
            for prefix in sorted(prefixes)
        },
    }


def classifier_metadata(direction, result) -> JsonObject:
    """Copy typed accounting fields only; provider/input text never belongs here."""
    result = result if isinstance(result, dict) else {}
    usage = _counters(result.get("usage"), (*TOKEN_KEYS, "total_tokens"))
    supplied_usage = result.get("usage")
    if isinstance(supplied_usage, dict):
        for field, keys in (
            ("input_tokens_details", ("cached_tokens", "cache_write_tokens")),
            ("output_tokens_details", ("reasoning_tokens",)),
        ):
            details = _counters(supplied_usage.get(field), keys)
            if details:
                usage[field] = details
    provider = result.get("providerError")
    provider = (
        {
            **({"kind": _label(provider.get("kind"))} if _label(provider.get("kind")) else {}),
            **(
                {"httpStatus": provider["httpStatus"]}
                if type(provider.get("httpStatus")) is int and 100 <= provider["httpStatus"] <= 599
                else {}
            ),
        }
        if isinstance(provider, dict)
        else None
    )
    accounting = result.get("accounting")
    accounting = (
        {
            **_counters(
                accounting,
                (
                    "ceilingNanodollars",
                    "spentNanodollars",
                    "reservedNanodollars",
                    "requests",
                    "inputTokens",
                    "unknownCompletions",
                ),
            ),
            **{key: _label(accounting[key]) for key in ("rateVersion", "limitKind") if _label(accounting.get(key))},
            **({"authority": _hash(accounting.get("authority"))} if _hash(accounting.get("authority")) else {}),
        }
        if isinstance(accounting, dict)
        else None
    )
    report = {
        **{key: _label(result.get(key)) for key in ("model", "policyVersion", "choice", "fallback")},
        "usage": usage if supplied_usage is not None else None,
        "latencyMs": result["latencyMs"]
        if type(result.get("latencyMs")) in (int, float)
        and math.isfinite(result["latencyMs"])
        and result["latencyMs"] >= 0
        else None,
        "providerError": provider,
        "requestId": _hash(result.get("requestId")),
        "accounting": accounting,
    }
    for key in ("inputHash", "promptHash"):
        if _hash(result.get(key)):
            report[key] = result[key]
    if type(result.get("contextSchemaVersion")) is int and result["contextSchemaVersion"] >= 0:
        report["contextSchemaVersion"] = result["contextSchemaVersion"]
    if direction == "advisory_batch":
        return {**report, **_advisory_metadata(result)}
    if direction == "advisory_delivery":
        return {**report, **_advisory_delivery_metadata(result)}
    if direction not in CHOICES:
        return report
    for key in ("confidence", "rawChoiceConfidence", "appliedThreshold"):
        if _probability(result.get(key)):
            report[key] = result[key]
    for key in ("overridden", "lowConfidence"):
        if type(result.get(key)) is bool:
            report[key] = result[key]
    if isinstance(result.get("rawChoice"), str) and result["rawChoice"] in CHOICES[direction]:
        report["rawChoice"] = result["rawChoice"]
    if isinstance(result.get("thresholds"), dict):
        report["thresholds"] = {
            key: value
            for key, value in result["thresholds"].items()
            if key in CHOICES[direction] and _probability(value)
        }
    raw = (
        two_question_answers(result.get("rawAnswers"), direction)
        if result.get("policyVersion") == POLICY_VERSION
        else None
    )
    if raw is not None:
        report["rawAnswers"] = raw
    if (
        raw is not None
        and isinstance(result.get("questionBasis"), str)
        and result["questionBasis"] in QUESTION_CHOICES[direction]
    ):
        report["questionBasis"] = result["questionBasis"]
    # Independent binary questions have no joint three-way probability vector.
    report["probabilities"] = None
    return report


def merge_model_usage(record: JsonObject, total: object, fields: dict[str, str]) -> bool:
    """Merge one cumulative app-server sample and estimate newly billed responses."""
    if not isinstance(total, dict):
        return False
    values = {key: total.get(field, 0) for key, field in fields.items()}
    if any(type(value) is not int or value < 0 for value in values.values()):
        return False
    usage = record.setdefault("usage", {})
    increased = any(values[key] > usage.get(key, 0) for key in fields)
    for key in fields:
        usage[key] = max(usage.get(key, 0), values[key])
    if increased:
        record["modelRequests"] = record.get("modelRequests", 0) + 1
    record["usageReported"] = all(field in total for field in fields.values())
    return True


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

# Paid scheduling work is not a gameplay/communication model activation and is
# not rolled back with game memory. Do not store conversation text here.
classifier_calls = Table(
    "classifier_calls",
    metadata,
    Column("id", Text, primary_key=True),
    Column("payload", Text, nullable=False),
)


class JobAccounting:
    def classifier(self, direction, result):
        with self.connect() as db:
            db.execute(
                insert(classifier_calls).values(
                    id=uuid.uuid4().hex,
                    payload=json.dumps(
                        {
                            "direction": direction,
                            "atMs": int(time.time() * 1000),
                            **classifier_metadata(direction, result),
                        }
                    ),
                )
            )

    def classifier_reports(self):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute(select(classifier_calls.c.payload))]

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
                            "modelRequests": row["telemetry"].get("modelRequests", 0),
                            "settings": {
                                "provider": row["telemetry"].get("provider"),
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
            "roleWaitMs": 0,
            "usageReported": False,
            "usageComplete": False,
            "model": None,
            "provider": None,
            "reasoningEffort": None,
            "modelRequests": 0,
            "apiCalls": {},
        }
        row = {"epoch": epoch, "claimed": claimed, "usage": dict.fromkeys(TOKEN_KEYS, 0), "telemetry": report}
        with self.connect() as db:
            db.execute(insert(jobs).values(id=id, payload=json.dumps(row), sent=0).prefix_with("OR IGNORE"))
        self.starts[id] = time.monotonic()

    def begin_call(self, job: str, name: str, arguments: JsonObject, *, source: str = "tool") -> str:
        id = uuid.uuid4().hex
        if source not in {"tool", "startup", "internal"}:
            raise ValueError("Invalid call source")
        keys = [source + ":" + str(name)[:128]]
        if name == "player_command" and isinstance(arguments, dict) and isinstance(arguments.get("kind"), str):
            keys.append("command:" + arguments["kind"][:128])
        with self.connect() as db:
            db.execute(insert(calls).values(id=id, job=job, keys=json.dumps(keys), started=time.monotonic()))
        return id

    def end_call(self, id: str, outcome: str, replayed: bool, payload: dict[str, int] | None = None):
        with self.connect() as db:
            started = db.execute(select(calls.c.started).where(calls.c.id == id)).scalar_one()
            db.execute(
                calls.update()
                .where(calls.c.id == id)
                .values(
                    outcome=outcome, replayed=int(replayed), elapsed=max(0, int((time.monotonic() - started) * 1000))
                )
            )
            if payload is not None:
                job = db.execute(select(calls.c.job).where(calls.c.id == id)).scalar_one()
                saved = db.execute(select(jobs.c.payload).where(jobs.c.id == job)).scalar_one_or_none()
                if saved is not None:
                    row = json.loads(saved)
                    sizes = row.setdefault("payloadCharacters", {})
                    for key, value in payload.items():
                        sizes[key] = sizes.get(key, 0) + value
                    db.execute(jobs.update().where(jobs.c.id == job).values(payload=json.dumps(row)))

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
            row["purpose"] = record.get("purpose", "gameplay")
            if report["phase"] in TERMINAL and record["phase"] not in TERMINAL:
                return
            report["sequence"] += 1
            terminal = report["phase"] in TERMINAL
            # A rewind can restore an older activation record, but accounting
            # describes the work that actually ran. Completion evidence survives.
            if not report["usageComplete"]:
                report["phase"] = (
                    {
                        "finished": "ROLE_FINISHED",
                        "waiting_negotiation": "WAITING_NEGOTIATION",
                        "waiting_economy": "WAITING_ROLE",
                    }.get(str(record.get("roleState", "")), record["phase"])
                    if record["phase"] not in TERMINAL
                    else record["phase"]
                )
            if id in self.starts and not terminal:
                report["elapsedMs"] = max(report["elapsedMs"], int((time.monotonic() - self.starts[id]) * 1000))
            report["negotiationWaitMs"] = max(report["negotiationWaitMs"], int(wait_seconds * 1000))
            role_wait_ms = int(record.get("roleWaitSeconds", 0) * 1000)
            if record.get("roleWaitStartedAtMs") is not None:
                role_wait_ms += max(0, int(time.time() * 1000) - record["roleWaitStartedAtMs"])
            report["roleWaitMs"] = max(report.get("roleWaitMs", 0), role_wait_ms)
            settings = record.get("effectiveSettings", record.get("settings", {}))
            report.update(
                provider=settings.get("provider"),
                model=settings.get("model"),
                reasoningEffort=settings.get("reasoningEffort"),
            )
            if "reviewProgress" in record:
                # Local operator metadata, outside the native accounting protocol.
                row["reviewProgress"] = record["reviewProgress"]
            if "reviewTrace" in record:
                row["reviewTrace"] = record["reviewTrace"]
            report["usageReported"] = report["usageReported"] or bool(record.get("usageReported", False))
            report["usageComplete"] = report["usageComplete"] or (
                bool(record.get("usageReported", False)) and bool(record.get("usageComplete", False))
            )
            report["modelRequests"] = max(report.get("modelRequests", 0), record.get("modelRequests", 0))
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
