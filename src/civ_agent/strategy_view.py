"""Read-only operator view of dated strategic advice and background review work."""

import json
import time
from dataclasses import dataclass, replace

from rich.text import Text
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from civ_agent import memory_schema as tables
from civ_agent.monitoring import read_database
from civ_agent.operator_state import published_game as published_managed_epoch
from civ_agent.orchestration.contracts import Scope
from civ_agent.orchestration.native_watch import verify_native_notebook
from civ_agent.player_paths import PlayerPaths as ManagedPlayerPaths
from civ_agent.protocol import JsonObject
from civ_agent.terminal_output import visible_text
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

    def read(self, civilizations, epoch, jobs, *, histories=None):
        if self.epoch != epoch:
            self.epoch, self.previous = epoch, {}
        result = []
        for civilization in civilizations:
            try:
                view = self.player(civilization, epoch, jobs, (histories or {}).get(civilization, ()))
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

    def player(self, civilization, epoch, jobs, history=()):
        request = published_managed_epoch(self.root)
        selected = next(
            ((index, seat) for index, seat in enumerate(request.seats) if seat.civilization == civilization), None
        )
        if selected is None:
            raise ValueError("NATIVE_WATCH_REVIEW_PLAYER")
        index, seat = selected
        paths = ManagedPlayerPaths(self.root, index, civilization)
        if request.authority.epoch != epoch or seat.session.player != paths.player:
            raise ValueError("NATIVE_WATCH_REVIEW_SCOPE")
        scope = Scope(game=request.authority.game, player=paths.player, epoch=epoch, activation="watch")
        path = paths.coordinator / "notebook/memory.sqlite3"
        with read_database(path) as db:
            verify_native_notebook(db, scope)
            # Never load frozen input snapshots or private notes to display reports.
            query = (
                select(
                    tables.reviews.c.job,
                    tables.reviews.c.turn,
                    tables.reviews.c.status,
                    tables.reviews.c.attempts,
                    func.json_remove(tables.reviews.c.advice, "$._evidence").label("advice"),
                    func.coalesce(tables.jobs.c.epoch, epoch).label("sourceEpoch"),
                )
                .select_from(tables.reviews.outerjoin(tables.jobs, tables.reviews.c.job == tables.jobs.c.id))
                .order_by(tables.reviews.c.turn.desc())
            )
            completed = db.execute(query.where(tables.reviews.c.status == "COMPLETED").limit(1)).mappings().first()
            pending = db.execute(query.where(tables.reviews.c.status == "PENDING").limit(1)).mappings().first()
            recent = db.execute(query.limit(1)).mappings().first()
            report = {"turn": completed["turn"], **json.loads(completed["advice"])} if completed else None
            # A report's existence does not prove a later player read it. The
            # native delivery projection currently has no report-preload receipt.
            delivered = None
            task = pending if pending is not None else recent
            matching = [
                job
                for job in jobs
                if task is not None
                and job["civilization"] == civilization
                and job["epoch"] == epoch
                and job.get("reviewTurn") == task["turn"]
            ]
            latest = max(matching, key=lambda job: job["claimedAtMs"], default=None)
            # Completed attempts from a discarded timeline cannot complete a restored task.
            if pending and latest and (latest.get("telemetry") or {}).get("phase") == "COMPLETED":
                latest = None
            selected_history = [entry for entry in history if task is not None and entry.turn == task["turn"]]
            trace = {
                "entries": [{"label": entry.label, "text": entry.text} for entry in selected_history],
                "truncated": len(selected_history) >= 600,
            }
            progress = None
            if selected_history:
                progress = {
                    "stage": "Reviewing",
                    "toolCalls": sum(entry.label == "Tool call" for entry in selected_history),
                    "draftCharacters": sum(len(entry.text) for entry in selected_history if entry.label == "Assistant"),
                }
            return StrategyView(
                civilization,
                report,
                dict(pending) if pending else None,
                delivered,
                latest,
                error=(
                    f"The review for T{recent['turn']} was unavailable. Play can continue; any earlier report is dated."
                    if recent and recent["status"] == "UNAVAILABLE" and pending is None
                    else None
                ),
                progress=progress,
                trace=trace,
            )


def trace_text(view: StrategyView) -> Text:
    text = Text()
    text.append("Latest review attempt · committed SDK messages and execution events\n", style="bold")
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
            ("Relative outcome", "relativeOutcome"),
            ("Risks and uncertainties", "risks"),
        ):
            section(label, plan[field])
        if "expectedObservations" in plan:
            section("Expected observations", "\n".join(plan["expectedObservations"]))
            section("First actions", "\n".join(plan["firstActions"]))
        else:
            # Read-only display of retained baseline reports; never migrate their stores.
            section("Projection", plan["projection"])
    if "horizonTurns" in review:
        section("Comparison horizon", f"{review['horizonTurns']} turns")
        section("Prior outcomes", review["outcomeAssessment"])
    else:
        section("Short-term plan", "\n".join(review["shortTerm"]))
        section("Mid-term plan", review["midTerm"])
        section("Long-term plan", review["longTerm"])
        for outlook in review["victoryOutlooks"]:
            section(outlook["victory"] + " victory", outlook["outlook"])
    section("Reconsider when", review["reconsiderWhen"])
    diplomacy = review["diplomacy"]
    section("Diplomacy", diplomacy["assessment"])
    if not diplomacy["initiatives"]:
        section("Diplomatic initiatives", "No initiative proposed.")
    for initiative in diplomacy["initiatives"]:
        section("Diplomatic initiative · " + ", ".join(initiative["players"]), initiative["objective"])
        if "plan" in initiative:
            section("Supports plan", str(initiative["plan"]))
        for label, field in (
            ("Their incentives", "incentives"),
            ("Approach", "approach"),
            ("Next step", "nextStep"),
            ("Reconsider when", "reconsiderWhen"),
        ):
            section(label, initiative[field])
    return text
