"""Read-only operator view of dated strategic advice and background review work."""

import json
from dataclasses import dataclass, replace

from rich.text import Text
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from civ_agent import memory_schema as tables
from civ_agent.monitoring import read_database
from civ_agent.protocol import JsonObject
from civ_agent.terminal_output import visible_text
from civ_agent.turn_archive import civilization_directory_name


@dataclass(frozen=True)
class StrategyView:
    civilization: str
    report: JsonObject | None = None
    pending: JsonObject | None = None
    delivered_turn: int | None = None
    job: JsonObject | None = None
    error: str | None = None

    @property
    def status(self):
        if self.error:
            return "Unavailable"
        if self.pending:
            phase = ((self.job or {}).get("telemetry") or {}).get("phase")
            label = "Reviewing" if phase in {"STARTING", "RUNNING"} else "Pending"
            if self.pending["status"] == "PREPARED":
                label = "Preparing"
            return f"{label} T{self.pending['turn']}"
        return "Completed" if self.report else "No review yet"


class StrategyReader:
    def __init__(self, root):
        self.root = root
        self.epoch = None
        self.previous = {}

    def read(self, civilizations, epoch, jobs):
        if self.epoch != epoch:
            self.epoch, self.previous = epoch, {}
        result = []
        for civilization in civilizations:
            try:
                view = self.player(civilization, epoch, jobs)
                self.previous[civilization] = view
            except FileNotFoundError:
                view = StrategyView(civilization, error="Local review records are unavailable.")
            except (OSError, ValueError, KeyError, TypeError, SQLAlchemyError) as error:
                view = replace(
                    self.previous.get(civilization, StrategyView(civilization)),
                    error=f"Cannot read reviews ({type(error).__name__}); showing the last readable report.",
                )
            result.append(view)
        return tuple(result)

    def player(self, civilization, epoch, jobs):
        path = self.root / "agent-services" / civilization_directory_name(civilization) / "civilization/memory.sqlite3"
        with read_database(path) as db:
            identity = {
                key: value
                for key, value in db.execute(
                    select(tables.meta).where(tables.meta.c.key.in_(["epoch", "civilization"]))
                )
            }
            if json.loads(identity["epoch"]) != epoch or json.loads(identity["civilization"]) != civilization:
                return StrategyView(civilization, error="Review records belong to another player or game timeline.")
            # Never load frozen input snapshots or private notes to display reports.
            query = (
                select(
                    tables.reviews.c.job,
                    tables.reviews.c.turn,
                    tables.reviews.c.status,
                    tables.reviews.c.attempts,
                    tables.reviews.c.error,
                    tables.reviews.c.advice,
                    tables.jobs.c.epoch.label("sourceEpoch"),
                )
                .select_from(tables.reviews.join(tables.jobs))
                .order_by(tables.reviews.c.turn.desc())
            )
            completed = db.execute(query.where(tables.reviews.c.status == "COMPLETED").limit(1)).mappings().first()
            pending = db.execute(query.where(tables.reviews.c.status != "COMPLETED").limit(1)).mappings().first()
            report = {"turn": completed["turn"], **json.loads(completed["advice"])} if completed else None
            delivered = None
            if completed:
                delivered = db.scalar(
                    select(func.min(tables.jobs.c.game_turn))
                    .select_from(tables.active_jobs)
                    .where(
                        tables.jobs.c.purpose == "gameplay",
                        func.json_extract(tables.jobs.c.snapshot, "$.strategicAdvice.asOfTurn") == completed["turn"],
                    )
                )
            task = pending if pending is not None else completed
            matching = [
                job
                for job in jobs
                if task is not None
                and job["civilization"] == civilization
                and job["epoch"] == task["sourceEpoch"]
                and job["gameTurn"] == task["turn"]
            ]
            latest = max(matching, key=lambda job: job["claimedAtMs"], default=None)
            # Completed attempts from a discarded timeline cannot complete a restored task.
            if pending and latest and (latest.get("telemetry") or {}).get("phase") == "COMPLETED":
                latest = None
            return StrategyView(civilization, report, dict(pending) if pending else None, delivered, latest)


def report_text(view: StrategyView) -> Text:
    text = Text()

    def section(title, body):
        text.append(title + "\n", style="bold")
        text.append(visible_text(body) + "\n\n")

    if view.error:
        text.append(view.error + "\n\n", style="yellow")
    if view.report is None:
        text.append(
            "The first review is pending. Its report will appear here when complete."
            if view.pending
            else "No completed strategic report yet.",
            style="dim",
        )
        return text
    report, review = view.report, view.report["review"]
    section("Assessment", report["assessment"])
    section("Short-term plan", "\n".join(f"• {step}" for step in review["shortTerm"]))
    section("Mid-term plan", review["midTerm"])
    section("Long-term plan", review["longTerm"])
    for title, field in (
        ("Victory race", "race"),
        ("Strongest alternative", "alternative"),
        ("Disrupting rivals", "disruption"),
        ("Diplomacy and agreements", "agreements"),
    ):
        section(title, review[field])
    for path in review["victoryPaths"]:
        text.append(visible_text(path["victory"]) + " victory\n", style="bold cyan")
        # Display every saved report field without reinterpreting the advice.
        order = ("remaining", "objective", "investment", "payoff", "projection", "rivals")
        for field in sorted(path, key=lambda field: (order.index(field) if field in order else len(order), field)):
            if field == "victory":
                continue
            text.append(visible_text(field.capitalize()) + ": ", style="bold")
            text.append(visible_text(path[field]) + "\n")
        text.append("\n")
    return text
