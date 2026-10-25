"""Read an operator's saved match records without contacting or changing the game."""

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import SQLAlchemyError

from civ_agent.diplomacy_view import DiplomaticEvent
from civ_agent.monitoring import path_for, read_database, record_key, records
from civ_agent.protocol import JsonObject
from civ_agent.runner_history import HistoryError, LiveOutput
from civ_agent.strategy_view import StrategyReader, StrategyView
from civ_agent.terminal_output import visible_text
from civ_agent.tls import regular_file
from civ_agent.turn_archive import civilization_directory_name

MAX_EVENT_BATCH = 1000
MAX_MESSAGE_CHARS = 16000
MAX_ACTIVITY = 600


@dataclass(frozen=True)
class Activity:
    key: str
    civilization: str
    turn: int
    time: str
    label: str
    text: str
    verbose: bool = False
    participants: tuple[str, ...] = ()
    published_at: float = 0

    @property
    def sort_key(self):
        # Communications have no wall-clock timestamp. Order them by publication
        # within their game turn so new messages appear at the live end of the feed.
        try:
            timestamp = dt.datetime.fromisoformat(self.time).timestamp()
        except ValueError:
            timestamp = self.published_at
        return self.turn, timestamp, self.key


class ActivityOutput:
    """Use the existing console's event interpretation and streaming deduplication."""

    def __init__(self, identity: str, civilization: str, turn: int):
        self.identity, self.civilization, self.turn = identity, civilization, turn
        self.entries: list[Activity] = []
        self.time = ""
        self.verbose = False
        self.open_key = None
        self.count = 0

    def finish_line(self, owner=None):
        self.open_key = None

    def line(self, label, value, *, civilization=None):
        self.open_key = None
        text = visible_text(value)
        if len(text) > MAX_MESSAGE_CHARS:
            text = text[:MAX_MESSAGE_CHARS] + "\n… Full text is in the saved history."
        self.entries.append(
            Activity(
                f"{self.identity}:{self.count:08d}",
                self.civilization,
                self.turn,
                self.time,
                label,
                text,
                self.verbose and label not in {"Error", "History"},
            )
        )

        self.count += 1
        self.entries = self.entries[-MAX_ACTIVITY:]

    def stream(self, owner, item, civilization, label, value, *, final=False):
        key = (owner, item)
        if value:
            if self.open_key == key and self.entries:
                entry = self.entries[-1]
                self.entries[-1] = replace(entry, text=(entry.text + visible_text(value))[:MAX_MESSAGE_CHARS])
            else:
                self.line(label, value)
            self.open_key = key
        if final:
            self.finish_line()


def consume_activity(output, decoder, event):
    event = decoder.chunks.expand(event)
    if event is None:
        return
    data = event.get("data", {})
    if not isinstance(data, dict) or not isinstance(event.get("kind"), str):
        raise ValueError("Invalid history event")
    output.time = str(event.get("time", ""))
    output.verbose = event["kind"] in {
        "tool_call",
        "tool_result",
        "tool_output",
        "reasoning_summary",
        "reasoning_summary_delta",
    } or (event["kind"] in {"developer_message", "user_message"} and data.get("source") == "service_startup")
    decoder.consume(event)


def activities(path: Path, source: JsonObject, lines: list[JsonObject]) -> tuple[Activity, ...]:
    output = ActivityOutput(str(path), source["civilization"], source["gameTurn"])
    decoder = LiveOutput(source["civilization"], verbose=True, renderer=output, history_path=path)
    for event in lines:
        consume_activity(output, decoder, event)
    return tuple(output.entries[-MAX_ACTIVITY:])


SPECIALIST_ROLES = ("economy", "military")


@dataclass(frozen=True)
class SpecialistView:
    civilization: str
    role: str
    job: JsonObject | None = None
    activity: tuple[Activity, ...] = ()
    error: str | None = None
    schedule: JsonObject | None = None

    @property
    def status(self):
        if self.schedule:
            phase = self.schedule.get("phase")
            turn, due = self.schedule["gameTurn"], self.schedule.get("nextReviewTurn")
            if phase == "sleeping":
                return (
                    f"Sleeping · review in {max(0, due - turn)} turns"
                    if due is not None
                    else "Sleeping · waiting for first city"
                )
            if phase == "finished" and (self.job or {}).get("gameTurn") == turn:
                failed = ((self.job or {}).get("telemetry") or {}).get("phase") in {"FAILED", "INTERRUPTED"}
                if not failed:
                    return f"Finished T{turn} · next review T{due}"
            if phase == "running" and (self.job or {}).get("gameTurn") != turn:
                return f"Starting T{turn}"
        if self.job is None:
            return "No job yet" if self.role == "economy" else "Inactive · activates during wars"
        phase = (self.job.get("telemetry") or {}).get("phase") or "Waiting for telemetry"
        state = (self.job.get("modelStatus") or {}).get("state")
        if phase in {"STARTING", "RUNNING"} and state in {"waiting", "retrying"}:
            phase = "Model " + state
        return f"{visible_text(phase).replace('_', ' ').capitalize()} T{self.job['gameTurn']}"


class SpecialistReader:
    """Tail existing, redacted service histories without constructing their writers."""

    def __init__(self, root):
        self.root = root
        self.epoch = None
        self.previous = {}
        self.cache = {}

    def read(self, civilizations, epoch, jobs):
        if self.epoch != epoch:
            self.epoch, self.previous, self.cache = epoch, {}, {}
        result = []
        used = set()
        for civilization in civilizations:
            for role in SPECIALIST_ROLES:
                key = (civilization, role)
                matching = [
                    job
                    for job in jobs
                    if job.get("epoch") == epoch
                    and job.get("civilization") == civilization
                    and job.get("purpose") == role
                ]
                job = max(matching, key=lambda j: (j["gameTurn"], j["claimedAtMs"]), default=None)
                view = SpecialistView(civilization, role, job)
                if job is not None:
                    used.add(job["id"])
                    try:
                        view = self.player(view)
                        self.previous[key] = view
                    except (OSError, ValueError, TypeError, KeyError, HistoryError) as error:
                        previous = self.previous.get(key)
                        if previous and previous.job and previous.job["id"] == job["id"]:
                            view = previous
                        view = replace(view, error=f"Cannot read {role} activity ({type(error).__name__}); retrying.")
                if role == "economy":
                    path = (
                        self.root / "agent-services" / civilization_directory_name(civilization) / "economy-status.json"
                    )
                    try:
                        if path.is_file():
                            regular_file(path, private=True)
                            schedule = json.loads(path.read_text())
                            if (
                                not isinstance(schedule, dict)
                                or schedule.get("phase") not in {"sleeping", "running", "finished"}
                                or (
                                    schedule.get("nextReviewTurn") is not None
                                    and type(schedule["nextReviewTurn"]) is not int
                                )
                                or not isinstance(schedule.get("reasons", []), list)
                                or any(not isinstance(reason, str) for reason in schedule.get("reasons", []))
                            ):
                                raise ValueError("Invalid economy schedule")
                            latest = max(
                                (
                                    j["gameTurn"]
                                    for j in jobs
                                    if j.get("epoch") == epoch and j.get("civilization") == civilization
                                ),
                                default=-1,
                            )
                            if (
                                schedule.get("epoch") == epoch
                                and type(schedule.get("gameTurn")) is int
                                and schedule["gameTurn"] >= latest
                            ):
                                view = replace(view, schedule=schedule)
                    except (OSError, ValueError, TypeError, KeyError):
                        view = replace(view, error="Cannot read economy schedule; retrying.")
                result.append(view)
        self.cache = {key: value for key, value in self.cache.items() if key in used}
        return tuple(result)

    def player(self, view):
        assert view.job is not None
        job = dict(view.job)
        base = self.root / "agent-services" / civilization_directory_name(view.civilization)
        digest = hashlib.sha256(job["id"].encode()).hexdigest()
        record_path = base / "activations" / (digest + ".json")
        if record_path.is_file():
            regular_file(record_path, private=True)
            record = json.loads(record_path.read_text())
            if any(
                record.get(field) != value
                for field, value in (
                    ("epoch", self.epoch),
                    ("civilization", view.civilization),
                    ("purpose", view.role),
                    ("activationId", job["id"]),
                )
            ):
                raise ValueError("Specialist records belong to another job, player or timeline")
            telemetry = dict(job.get("telemetry") or {})
            telemetry.update(record.get("effectiveSettings", record.get("settings", {})))
            phase = record["phase"]
            if phase not in {"COMPLETED", "FAILED", "INTERRUPTED"}:
                phase = {
                    "finished": "ROLE_FINISHED",
                    "waiting_negotiation": "WAITING_NEGOTIATION",
                    "waiting_economy": "WAITING_ROLE",
                }.get(record.get("roleState"), phase)
            if telemetry.get("phase") not in {"COMPLETED", "FAILED", "INTERRUPTED"}:
                telemetry["phase"] = phase
            if record.get("usageComplete"):
                telemetry["usageComplete"] = True
            usage = dict(job.get("usage") or {})
            for key, value in record.get("usage", {}).items():
                usage[key] = max(usage.get(key, 0), value)
            job.update(telemetry=telemetry, usage=usage)
            job["modelStatus"] = record.get("modelStatus")
            job["error"] = record.get("error")
        view = replace(view, job=job)
        path = base / "history" / digest / "events.jsonl"
        if not path.is_file():
            raise FileNotFoundError("Local specialist history is unavailable")
        regular_file(path, private=True)
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino)
        cached = self.cache.get(job["id"])
        if cached is None or cached[0] != identity or stat.st_size < cached[1]:
            output = ActivityOutput(job["id"], view.civilization, job["gameTurn"])
            cached = (identity, 0, 0, output, LiveOutput(view.civilization, verbose=True, renderer=output))
        self.cache[job["id"]] = cached
        _, offset, cursor, output, decoder = cached
        with path.open("rb") as stream:
            stream.seek(offset)
            for _ in range(MAX_EVENT_BATCH):
                line = stream.readline(64 * 1024 + 1)
                if len(line) > 64 * 1024 and not line.endswith(b"\n"):
                    raise HistoryError("Specialist history event exceeds the size limit")
                if not line or not line.endswith(b"\n"):
                    break  # Retry an append in progress on the next poll.
                event = json.loads(line)
                if not isinstance(event, dict) or not isinstance(event.get("data"), dict):
                    raise ValueError("Invalid specialist history event")
                if event.get("sequence") != cursor:
                    raise HistoryError("Specialist history sequence has a gap")
                consume_activity(output, decoder, event)
                offset, cursor = stream.tell(), cursor + 1
                self.cache[job["id"]] = (identity, offset, cursor, output, decoder)
        return replace(view, activity=tuple(output.entries))


@dataclass(frozen=True)
class WatchSnapshot:
    run: JsonObject
    result: JsonObject
    turns: tuple[JsonObject, ...]
    activity: tuple[Activity, ...]
    errors: tuple[str, ...]
    updated_at: float | None
    diplomacy: tuple[DiplomaticEvent, ...] = ()
    negotiation: JsonObject | None = None
    diplomacy_available: bool = False
    strategies: tuple[StrategyView, ...] = ()
    specialists: tuple[SpecialistView, ...] = ()

    @property
    def epoch(self):
        return self.run.get("epoch") or self.result.get("epoch")

    @property
    def players(self) -> list[str]:
        names = list(self.run.get("sessions", {}))
        for row in self.result.get("scores", []):
            if isinstance(row, dict) and isinstance(row.get("civilization"), str) and row["civilization"] not in names:
                names.append(row["civilization"])
        for record in self.turns:
            name = record["turn"]["civilization"]
            if name not in names:
                names.append(name)
        return names

    def latest_turn(self, civilization=None):
        return next(
            (r for r in reversed(self.turns) if civilization is None or r["turn"]["civilization"] == civilization), None
        )

    def player_model(self, civilization):
        settings = self.run.get("sessions", {}).get(civilization, {}).get("effectiveSettings") or {}
        if settings.get("model"):
            return settings["model"]
        # Opening reviews can report the model before the first gameplay session.
        review = next((view for view in self.strategies if view.civilization == civilization), None)
        return ((review.job or {}).get("telemetry") or {}).get("model") if review else None


class WatchReader:
    def __init__(self, root: Path):
        self.root = root
        self.path = path_for(root)
        self.identity = None
        self.revision = 0
        self.documents = {}
        self.turns = {}
        self.streams = {}
        self.diplomacy = {}
        self.observed_jobs = {}
        self.strategy_reader = StrategyReader(root)
        self.specialist_reader = SpecialistReader(root)
        self.cache = {}
        self.errors = []
        self.updated_at = None
        self.previous = WatchSnapshot({}, {}, (), (), (), None)

    def history(self, db, epoch, civilization, turn, activation):
        key = record_key(epoch, civilization, activation)
        source = self.streams.get(key)
        if source is None:
            return ()
        if source != dict(epoch=epoch, civilization=civilization, gameTurn=turn, activationId=activation):
            self.errors.append("History belongs to another turn or player; skipped")
            return ()
        if key not in self.cache:
            output = ActivityOutput(key, civilization, turn)
            self.cache[key] = (output, LiveOutput(civilization, verbose=True, renderer=output), 0)
        output, decoder, cursor = self.cache[key]
        values = db.execute(
            select(records.c.payload, records.c.updated_ms)
            .where(
                records.c.collection == "events",
                records.c.epoch == epoch,
                records.c.civilization == civilization,
                records.c.activation == activation,
                records.c.sequence >= cursor,
            )
            .order_by(records.c.sequence)
            .limit(MAX_EVENT_BATCH)
        )
        for value, updated_ms in values:
            event = json.loads(value)
            if event.get("sequence") != cursor:
                raise HistoryError("History event sequence has a gap")
            if not isinstance(event.get("data"), dict):
                raise ValueError("Invalid history event")
            consume_activity(output, decoder, event)
            self.updated_at = max(self.updated_at or 0, updated_ms / 1000)
            cursor += 1
            self.cache[key] = (output, decoder, cursor)
        return tuple(output.entries)

    def read(self, selected: tuple[str, int] | None = None) -> WatchSnapshot:
        self.errors = []
        try:
            stat = self.path.stat()
            identity = (stat.st_dev, stat.st_ino)
            if self.identity != identity:
                self.identity = identity
                self.revision = 0
                self.updated_at = None
                self.documents, self.turns, self.streams, self.cache = {}, {}, {}, {}
                self.diplomacy = {}
                self.observed_jobs = {}
                self.strategy_reader = StrategyReader(self.root)
                self.specialist_reader = SpecialistReader(self.root)
            with read_database(self.path) as db:
                snapshot = self._read(db, selected)
            # Opening reviews precede gameplay sessions. Include the native
            # agent roster so a civ waiting for its first review stays visible.
            agents = dict.fromkeys(snapshot.run.get("sessions", {}))
            agents.update(
                (row["civilization"], None)
                for row in snapshot.result.get("scores", [])
                if row.get("controller") == "AGENT"
            )
            jobs = [{"id": value["jobId"], **value["job"]} for value in self.observed_jobs.values()]
            strategies = self.strategy_reader.read(
                agents, snapshot.epoch, [job for job in jobs if job["purpose"] == "review"]
            )
            specialists = self.specialist_reader.read(agents, snapshot.epoch, jobs)
            self.previous = replace(snapshot, strategies=strategies, specialists=specialists)
            return self.previous
        except FileNotFoundError:
            return WatchSnapshot({}, {}, (), (), (), None)
        except (OSError, ValueError, TypeError, KeyError, HistoryError, SQLAlchemyError) as error:
            return replace(
                self.previous, errors=(f"Monitoring database: {type(error).__name__}; showing last readable records",)
            )

    def _read(self, db, selected):
        changes = db.execute(
            select(records)
            .where(
                or_(
                    records.c.collection.in_(["state", "turns", "streams", "diplomacy"]),
                    and_(
                        records.c.collection == "jobs",
                        func.json_extract(records.c.payload, "$.job.purpose").in_(["review", *SPECIALIST_ROLES]),
                    ),
                ),
                records.c.revision > self.revision,
            )
            .order_by(records.c.revision)
        ).mappings()
        for row in changes:
            collection = {
                "state": self.documents,
                "turns": self.turns,
                "streams": self.streams,
                "diplomacy": self.diplomacy,
                "jobs": self.observed_jobs,
            }[row["collection"]]
            value = json.loads(row["payload"])
            if not isinstance(value, dict):
                raise ValueError("Expected an object")
            collection[row["key"]] = (
                DiplomaticEvent.from_record(value, row["revision"], row["updated_ms"] / 1000)
                if row["collection"] == "diplomacy"
                else value
            )
            self.revision = row["revision"]
            self.updated_at = max(self.updated_at or 0, row["updated_ms"] / 1000)
        run = self.documents.get("run", {})
        result = self.documents.get("result", {})
        epoch = run.get("epoch") or result.get("epoch")
        if result and result.get("epoch") != epoch:
            self.errors.append("Telemetry belongs to another epoch; waiting for current records")
            result = {}
        turns = {}
        for record in self.turns.values():
            turn = record.get("turn")
            if not isinstance(turn, dict) or turn.get("epoch") != epoch:
                continue
            if (
                type(turn.get("handoff")) is not int
                or type(turn.get("gameTurn")) is not int
                or not isinstance(turn.get("civilization"), str)
            ):
                self.errors.append("Malformed turn telemetry; skipped")
                continue
            turns[turn["handoff"]] = record  # Later revisions replace accounting for an existing turn.
        ordered = tuple(turns[key] for key in sorted(turns))
        wanted: dict[tuple[str, int], list[str]] = {}
        counts: dict[str, int] = {}
        for record in reversed(ordered):
            turn = record["turn"]
            identity = (turn["civilization"], turn["gameTurn"])
            if identity == selected or (selected is None and counts.get(identity[0], 0) < 2):
                wanted.setdefault(identity, []).extend(record.get("jobs", []))
                counts[identity[0]] = counts.get(identity[0], 0) + 1
        flights = [run.get("inFlight"), *run.get("pendingFinalizations", [])]
        for flight in flights:
            if not isinstance(flight, dict) or type(flight.get("gameTurn")) is not int:
                continue
            name, activation = flight.get("civilization"), flight.get("activationId")
            if not isinstance(name, str) or not isinstance(activation, str):
                continue
            identity = (name, flight["gameTurn"])
            if selected is None or identity == selected:
                wanted.setdefault(identity, []).append(activation)
        entries = []
        if isinstance(epoch, str):
            for (civilization, turn), attempts in wanted.items():
                for activation in dict.fromkeys(a for a in attempts if isinstance(a, str)):
                    entries.extend(self.history(db, epoch, civilization, turn, activation))
        used = {
            record_key(epoch, civilization, activation)
            for (civilization, _), attempts in wanted.items()
            for activation in attempts
        }
        self.cache = {key: value for key, value in self.cache.items() if key in used}
        diplomacy = tuple(e for e in self.diplomacy.values() if e.epoch == epoch)
        latest_turn = max((r["turn"]["gameTurn"] for r in ordered), default=0)
        if type((run.get("inFlight") or {}).get("gameTurn")) is int:
            latest_turn = max(latest_turn, run["inFlight"]["gameTurn"])
        for event in diplomacy:
            if not event.visible or event.turn is None:
                continue
            if selected is not None:
                if selected[0] not in event.participants or event.turn != selected[1]:
                    continue
            elif event.turn < latest_turn - 1:
                continue
            if event.kind == "trade" and event.body.get("status") == "SUPERSEDED":
                continue
            text = event.headline
            if event.body.get("failure"):
                text += " Reason: " + event.body["failure"]
            text = text[:300] + ("… (Diplomacy tab has the full text)" if len(text) > 300 else "")
            label = "Social promise" if event.kind in {"offer", "consent"} else "Diplomacy"
            entries.append(
                Activity(
                    f"diplomacy:{event.order:012d}",
                    event.author,
                    event.turn,
                    "",
                    label,
                    text,
                    participants=event.participants,
                    published_at=event.published_at,
                )
            )
        entries.sort(key=lambda entry: entry.sort_key)
        diplomatic_state = self.documents.get("diplomacy", {})
        available = diplomatic_state.get("epoch") == epoch and epoch is not None
        return WatchSnapshot(
            run,
            result,
            ordered,
            tuple(entries[-MAX_ACTIVITY:]),
            tuple(dict.fromkeys(self.errors)),
            self.updated_at,
            diplomacy,
            diplomatic_state.get("activeNegotiation") if available else None,
            available,
        )


def event_time(value: str) -> str:
    try:
        return dt.datetime.fromisoformat(value).astimezone().strftime("%H:%M:%S")
    except ValueError:
        return "--:--:--"
