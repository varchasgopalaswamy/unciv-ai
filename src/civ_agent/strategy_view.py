"""Read-only operator view of dated strategic advice and background review work."""

import json
import time
from dataclasses import dataclass, replace

from rich.text import Text
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from civ_agent import memory_schema as tables
from civ_agent.monitoring import read_database
from civ_agent.protocol import JsonObject
from civ_agent.telemetry import jobs as accounting_jobs
from civ_agent.terminal_output import visible_text
from civ_agent.turn_archive import civilization_directory_name
from civ_agent.turn_display import seconds


@dataclass(frozen=True)
class StrategyView:
    civilization: str
    report: JsonObject | None = None
    pending: JsonObject | None = None
    delivered_turn: int | None = None
    job: JsonObject | None = None
    error: str | None = None
    progress: JsonObject | None = None
    trace: JsonObject | None = None

    @property
    def status(self):
        if self.error:
            return "Unavailable"
        if self.pending:
            phase = ((self.job or {}).get("telemetry") or {}).get("phase")
            label = "Reviewing" if phase in {"STARTING", "RUNNING"} else "Pending"
            if self.progress and phase in {"STARTING", "RUNNING"}:
                label = self.progress["stage"]
                state = (self.progress.get("modelStatus") or {}).get("state")
                if state in {"waiting", "retrying"}:
                    label = state.capitalize()
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
                    func.coalesce(tables.jobs.c.epoch, epoch).label("sourceEpoch"),
                )
                .select_from(tables.reviews.outerjoin(tables.jobs, tables.reviews.c.job == tables.jobs.c.id))
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
            progress = None
            trace = None
            accounting = path.parent.parent / "telemetry/accounting.sqlite3"
            if latest and latest.get("id") and accounting.is_file():
                with read_database(accounting) as ledger:
                    payload = ledger.scalar(
                        select(accounting_jobs.c.payload).where(accounting_jobs.c.id == latest["id"])
                    )
                if payload:
                    record = json.loads(payload)
                    if record["epoch"] == epoch:
                        progress = record.get("reviewProgress")
                        trace = record.get("reviewTrace")
            return StrategyView(
                civilization,
                report,
                dict(pending) if pending else None,
                delivered,
                latest,
                progress=progress,
                trace=trace,
            )


def trace_text(view: StrategyView) -> Text:
    text = Text()
    text.append("Latest review attempt · exposed summaries and streamed output\n", style="bold")
    if view.trace is None:
        text.append("Live trace is unavailable from this review service.", style="dim")
        return text
    if view.trace.get("truncated"):
        text.append("Showing the most recent trace; earlier text is omitted.\n", style="dim")
    entries = view.trace.get("entries", [])
    if not entries:
        text.append("Waiting for review output…", style="dim")
    for entry in entries:
        text.append("\n" + visible_text(entry["label"]) + "\n", style="bold cyan")
        text.append(visible_text(entry["text"]) + "\n")
    return text


def progress_text(view: StrategyView) -> Text:
    text = Text()
    if not view.pending:
        return text
    telemetry = (view.job or {}).get("telemetry") or {}
    if telemetry.get("model"):
        text.append(
            f"\n{telemetry.get('provider') or 'unknown provider'} / {telemetry['model']} · {telemetry.get('reasoningEffort') or 'default'} reasoning",
            style="dim",
        )
    progress = view.progress
    if progress:
        details = [f"{progress['toolCalls']} tool calls", f"{progress['draftCharacters']:,} report characters"]
        if progress.get("lastActivityAtMs") is not None:
            details.append(f"last activity {seconds(max(0, time.time() * 1000 - progress['lastActivityAtMs']))} ago")
        else:
            details.append("no model activity received yet")
        text.append("\n" + " · ".join(details), style="dim")
        notice = progress.get("modelStatus") or {}
        if notice.get("message"):
            text.append("\n" + visible_text(notice["message"]), style="yellow")
    elif view.job:
        text.append("\nDetailed progress unavailable from this service.", style="dim")
    return text


def report_text(view: StrategyView) -> Text:
    text = Text()

    def section(title, body):
        text.append(visible_text(title) + "\n", style="bold")
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
    preferred = review["preferredPlan"]
    section(
        "Recommendation",
        "No preferred plan; the choice remains open."
        if preferred is None
        else f"Plan {preferred} · {review['plans'][preferred - 1]['name']}",
    )
    for number, plan in enumerate(review["plans"], 1):
        section(f"Plan {number} · {plan['name']}", plan["objective"])
        for label, field in (
            ("Investment", "investment"),
            ("Projection", "projection"),
            ("Relative outcome", "relativeOutcome"),
            ("Risks and uncertainties", "risks"),
        ):
            section(label, plan[field])
    section("Reconsider when", review["reconsiderWhen"])
    section("Short-term plan", "\n".join(f"• {step}" for step in review["shortTerm"]))
    section("Mid-term plan", review["midTerm"])
    section("Long-term plan", review["longTerm"])
    diplomacy = review["diplomacy"]
    section("Diplomacy", diplomacy["assessment"])
    if not diplomacy["initiatives"]:
        section("Diplomatic initiatives", "No initiative proposed.")
    for initiative in diplomacy["initiatives"]:
        section("Diplomatic initiative · " + ", ".join(initiative["players"]), initiative["objective"])
        for label, field in (
            ("Their incentives", "incentives"),
            ("Approach", "approach"),
            ("Next step", "nextStep"),
            ("Reconsider when", "reconsiderWhen"),
        ):
            section(label, initiative[field])
    for path in review["victoryOutlooks"]:
        section(path["victory"] + " victory", path["outlook"])
    return text
