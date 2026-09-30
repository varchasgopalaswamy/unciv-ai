"""Read an operator's saved match records without contacting or changing the game."""

import datetime as dt
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
        self.review_jobs = {}
        self.strategy_reader = StrategyReader(root)
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
                self.review_jobs = {}
                self.strategy_reader = StrategyReader(self.root)
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
            strategies = self.strategy_reader.read(
                agents,
                snapshot.epoch,
                [{"id": value["jobId"], **value["job"]} for value in self.review_jobs.values()],
            )
            self.previous = replace(snapshot, strategies=strategies)
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
                        func.json_extract(records.c.payload, "$.job.purpose") == "review",
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
                "jobs": self.review_jobs,
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
