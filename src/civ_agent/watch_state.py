"""Read an operator's saved match records without contacting or changing the game."""

import datetime as dt
import hashlib
import json
import stat as file_stat
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from civ_agent.protocol import JsonObject
from civ_agent.runner_history import HistoryError, LiveOutput
from civ_agent.terminal_output import visible_text
from civ_agent.turn_archive import civilization_directory_name

MAX_FILE_BYTES = 64 * 1024 * 1024
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


class ActivityOutput:
    """Use the existing console's event interpretation and streaming deduplication."""

    def __init__(self, identity: str, civilization: str, turn: int):
        self.identity, self.civilization, self.turn = identity, civilization, turn
        self.entries: list[Activity] = []
        self.time = ""
        self.verbose = False
        self.open_key = None

    def finish_line(self, owner=None):
        self.open_key = None

    def line(self, label, value, *, civilization=None):
        self.open_key = None
        text = visible_text(value)
        if len(text) > MAX_MESSAGE_CHARS:
            text = text[:MAX_MESSAGE_CHARS] + "\n… Full text is in the saved history."
        self.entries.append(
            Activity(
                f"{self.identity}:{len(self.entries):08d}",
                self.civilization,
                self.turn,
                self.time,
                label,
                text,
                self.verbose and label not in {"Error", "History"},
            )
        )

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


def activities(path: Path, source: JsonObject, lines: list[JsonObject]) -> tuple[Activity, ...]:
    output = ActivityOutput(str(path), source["civilization"], source["gameTurn"])
    decoder = LiveOutput(
        source["civilization"], verbose=True, renderer=output, history_path=path.with_name("history.md")
    )
    for event in lines:
        event = decoder.chunks.expand(event)
        if event is None:
            continue
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
    return tuple(output.entries[-MAX_ACTIVITY:])


@dataclass(frozen=True)
class WatchSnapshot:
    run: JsonObject
    result: JsonObject
    turns: tuple[JsonObject, ...]
    activity: tuple[Activity, ...]
    errors: tuple[str, ...]
    updated_at: float | None

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
        self.cache: dict[Path, tuple[tuple, object]] = {}
        self.used: set[Path] = set()
        self.errors: list[str] = []
        self.updated_at: float | None = None

    def load[T](self, path: Path, parse: Callable[[bytes], T], default: T) -> T:
        self.used.add(path)
        previous = self.cache.get(path)
        try:
            stat = path.stat()
            if not file_stat.S_ISREG(stat.st_mode):
                raise ValueError("Expected a regular record file")
            self.updated_at = max(self.updated_at or 0, stat.st_mtime)
            signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
            if previous and signature == previous[0]:
                return cast(T, previous[1])
            if stat.st_size > MAX_FILE_BYTES:
                raise ValueError("Record exceeds the viewer's 64 MiB limit")
            with path.open("rb") as stream:
                body = stream.read(MAX_FILE_BYTES + 1)
            if len(body) > MAX_FILE_BYTES:
                raise ValueError("Record exceeds the viewer's 64 MiB limit")
            value = parse(body)
            self.cache[path] = (signature, value)
            return value
        except FileNotFoundError:
            # Records may not exist during launch, or while a new attempt is being published.
            return default
        except (OSError, ValueError, TypeError, KeyError, HistoryError) as error:
            self.errors.append(f"{path.relative_to(self.root)}: {type(error).__name__}; showing last readable records")
            return cast(T, previous[1]) if previous else default

    def document(self, path) -> JsonObject:
        def parse(body):
            value = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError("Expected an object")
            return value

        return self.load(path, parse, {})

    @staticmethod
    def json_lines(body) -> list[JsonObject]:
        values = []
        for line in body.splitlines(keepends=True):
            if not line.endswith(b"\n"):
                # Appending processes can expose an unfinished final record. Read it next time.
                break
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("Expected an object")
            values.append(value)
        return values

    def history(self, epoch, civilization, turn, activation) -> tuple[Activity, ...]:
        try:
            player = civilization_directory_name(civilization)
            attempt = civilization_directory_name(activation)
        except ValueError:
            self.errors.append("Invalid history identity; skipped")
            return ()
        directory = self.root / "runner/history" / player / f"turn_{turn}" / f"attempt_{attempt}"
        if not directory.exists():
            # The first history format used hashes rather than readable turn directories.
            directory = self.root / "runner/history"
            for component in (epoch, civilization, activation):
                directory /= hashlib.sha256(component.encode()).hexdigest()
        source = self.document(directory / "source.json")
        if not source:
            return ()
        if any(
            source.get(k) != v
            for k, v in {"epoch": epoch, "civilization": civilization, "activationId": activation}.items()
        ):
            self.errors.append("History belongs to another match epoch or player; skipped")
            return ()
        if source.get("gameTurn", turn) != turn:
            self.errors.append("History turn does not match its records; skipped")
            return ()
        source = {**source, "gameTurn": turn}
        path = directory / "events.jsonl"
        return self.load(path, lambda body: activities(path, source, self.json_lines(body)), ())

    def read(self, selected: tuple[str, int] | None = None) -> WatchSnapshot:
        self.used, self.errors, self.updated_at = set(), [], None
        run = self.document(self.root / "runner/run.json")
        result = self.document(self.root / "telemetry/game-result.json")
        epoch = run.get("epoch") or result.get("epoch")
        if result and result.get("epoch") != epoch:
            self.errors.append("Telemetry belongs to another epoch; waiting for current records")
            result = {}
        records = self.load(self.root / "telemetry/turns.jsonl", self.json_lines, [])
        turns = {}
        for record in records:
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
            turns[turn["handoff"]] = record  # Exports replace previous accounting for an existing turn.
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
                    entries.extend(self.history(epoch, civilization, turn, activation))
        self.cache = {path: value for path, value in self.cache.items() if path in self.used}
        entries.sort(key=lambda entry: (entry.time, entry.key))
        return WatchSnapshot(
            run, result, ordered, tuple(entries[-MAX_ACTIVITY:]), tuple(dict.fromkeys(self.errors)), self.updated_at
        )


def event_time(value: str) -> str:
    try:
        return dt.datetime.fromisoformat(value).astimezone().strftime("%H:%M:%S")
    except ValueError:
        return "--:--:--"
