"""An attachable terminal dashboard for local match records."""

import argparse
import asyncio
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, Header, RichLog, Static, TabbedContent, TabPane

from civ_agent.diplomacy_view import conversations, trade_status
from civ_agent.model_status import status_text
from civ_agent.strategy_view import progress_text, report_text, trace_text
from civ_agent.terminal_output import player_style, visible_text
from civ_agent.turn_display import api_calls, memory_edits_text, number, seconds
from civ_agent.watch_state import SPECIALIST_ROLES, WatchReader, WatchSnapshot, event_time


def count(value):
    return "—" if value is None else number(value)


def player_text(name, no_color=False, *, model=None):
    color = player_style(name)
    if not color.startswith("bright_"):
        color = "bright_" + color
    text = Text(visible_text(name), style="bold" if no_color else "bold " + color)
    if model:
        text.append(" · " + visible_text(model), style="not bold dim")
    return text


def usage_text(usage):
    return (
        f"Input {number(usage.get('input_tokens'))} · cached {number(usage.get('cached_input_tokens'))}  |  "
        f"Output {number(usage.get('output_tokens'))} · reasoning {number(usage.get('reasoning_output_tokens'))}"
    )


def game_directories(root: Path) -> list[tuple[str, Path]]:
    """Resolve either one match or the saved games in a batch, without writing."""
    path = root / "batch.json"
    if not path.is_file():
        return [(root.name, root)]
    manifest = json.loads(path.read_text())
    if not isinstance(manifest, dict) or manifest.get("format") != 2 or not isinstance(manifest.get("runs"), list):
        raise ValueError("Unsupported batch manifest")
    games = []
    seen = set()
    for row in manifest["runs"]:
        game_id = row.get("id") if isinstance(row, dict) else None
        if (
            not isinstance(game_id, str)
            or not game_id
            or Path(game_id).name != game_id
            or game_id in {".", ".."}
            or game_id in seen
        ):
            raise ValueError("Invalid game ID in batch manifest")
        seen.add(game_id)
        games.append((game_id, root / "games" / game_id))
    if not games:
        raise ValueError("Batch contains no games")
    return games


class MatchWatch(App):
    TITLE = "Unciv · Match dashboard"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { background: $surface; }
    Header { background: $panel; }
    #status { height: auto; min-height: 2; max-height: 5; padding: 0 1; }
    #game-switcher { height: auto; max-height: 3; padding: 0 1; color: $text-muted; }
    #negotiation { height: auto; padding: 0 1; color: $warning; }
    #players { height: 7; border: round $primary; margin: 0 1; }
    #metrics { height: auto; max-height: 5; padding: 0 2; }
    #scope { height: 1; padding: 0 2; color: $text-muted; }
    TabbedContent { height: 1fr; margin: 0 1; }
    TabPane { padding: 0; }
    RichLog { height: 1fr; padding: 0 1; }
    #conversations { height: auto; max-height: 8; }
    #diplomacy-help { height: auto; padding: 0 1; color: $text-muted; }
    #strategies { height: auto; max-height: 7; }
    #strategy-status { height: auto; max-height: 7; padding: 0 1; }
    .specialists { height: auto; max-height: 7; }
    .specialist-status { height: auto; max-height: 7; padding: 0 1; }
    Footer { background: $panel; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit viewer", priority=True),
        Binding("ctrl+c", "quit", show=False, priority=True),
        Binding("space", "freeze", "Freeze view", priority=True),
        Binding("a", "all_players", "All players"),
        Binding("l", "live", "Live activity"),
        Binding("f", "follow", "Follow log"),
        Binding("v", "verbose", "Verbose"),
        Binding("d", "diplomacy", "Diplomacy"),
        Binding("s", "strategy", "Strategy"),
        Binding("e", "economy", "Economy"),
        Binding("m", "military", "Military"),
        Binding("left_square_bracket", "previous_game", "Previous game", priority=True),
        Binding("right_square_bracket", "next_game", "Next game", priority=True),
    ]

    def __init__(self, state_directory: Path, *, verbose=False, refresh=1.0):
        super().__init__()
        self.state_directory = state_directory
        self.games = game_directories(state_directory)
        self.game_index = 0
        self.game_id, self.game_directory = self.games[0]
        self.reader = WatchReader(self.game_directory)
        self.sub_title = self.game_subtitle()
        self.verbose = verbose
        self.refresh_seconds = refresh
        self.frozen = False
        self.follow_log = True
        self.civilization: str | None = None
        self.snapshot = WatchSnapshot({}, {}, (), (), (), None)
        self.table_values = {}
        self.last_activity = None
        self.read_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="match-watch")
        self.reading = False
        self.poll_pending = False
        self.conversation: str | None = None
        self.last_diplomacy = None
        self.strategy_civilization: str | None = None
        self.last_strategy_selection = None
        self.last_strategy = None
        self.last_strategy_trace = None
        self.specialist_civilizations: dict[str, str | None] = dict.fromkeys(SPECIALIST_ROLES)
        self.last_specialists = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="game-switcher", markup=False)
        yield Static("Waiting for match records…", id="status", markup=False)
        yield Static(id="negotiation", markup=False)
        yield DataTable(id="players", cursor_type="row", zebra_stripes=True)
        yield Static(id="metrics", markup=False)
        yield Static(id="scope", markup=False)
        with TabbedContent(initial="activity-tab", id="main-tabs"):
            with TabPane("Activity", id="activity-tab"):
                yield RichLog(id="activity", wrap=True, min_width=1, max_lines=6000, markup=False)
            with TabPane("Diplomacy", id="diplomacy-tab"):
                yield Static(
                    "Select a conversation to read messages, offers and decisions.", id="diplomacy-help", markup=False
                )
                yield DataTable(id="conversations", cursor_type="row", zebra_stripes=True)
                yield RichLog(id="diplomacy", wrap=True, min_width=1, markup=False)
            with TabPane("Strategy", id="strategy-tab"):
                yield DataTable(id="strategies", cursor_type="row", zebra_stripes=True)
                yield Static(id="strategy-status", markup=False)
                with TabbedContent(id="strategy-content", initial="strategy-report-tab"):
                    with TabPane("Report", id="strategy-report-tab"):
                        yield RichLog(id="strategy", wrap=True, min_width=1, markup=False)
                    with TabPane("Live trace", id="strategy-trace-tab"):
                        yield RichLog(id="strategy-trace", wrap=True, min_width=1, markup=False)
            for role in SPECIALIST_ROLES:
                with TabPane(role.capitalize(), id=f"{role}-tab"):
                    yield DataTable(id=f"{role}-agents", classes="specialists", cursor_type="row", zebra_stripes=True)
                    yield Static(id=f"{role}-status", classes="specialist-status", markup=False)
                    yield RichLog(id=f"{role}-activity", wrap=True, min_width=1, max_lines=6000, markup=False)
        yield Footer()

    async def on_mount(self):
        for identity, columns in {
            "players": ("Player", "Status", "Turn", "Score", "Input", "Cached", "Output", "Game API"),
            "conversations": ("Participants", "Status", "Latest turn"),
            "strategies": ("Player", "Review status", "Report turn", "Preloaded turn", "Attempt time"),
        }.items():
            self.query_one(f"#{identity}", DataTable).add_columns(*columns)
        for role in SPECIALIST_ROLES:
            self.query_one(f"#{role}-agents", DataTable).add_columns(
                "Player / model", "Status", "Input", "Output", "Time"
            )
        self.query_one("#players", DataTable).border_title = "Players · Enter to filter"
        self.render_game_switcher()
        self.query_one("#activity", RichLog).focus()
        await self.poll_records()
        self.set_interval(self.refresh_seconds, self.poll_records)

    async def poll_records(self):
        if self.frozen:
            return
        if self.reading:
            self.poll_pending = True
            return
        self.reading = True
        reader = self.reader
        try:
            snapshot = await asyncio.get_running_loop().run_in_executor(self.read_executor, reader.read)
            if reader is self.reader and not self.frozen:
                self.snapshot = snapshot
                self.render_records()
        except (OSError, ValueError, TypeError, KeyError) as error:
            self.query_one("#status", Static).update(
                Text(
                    f"Cannot read match records ({type(error).__name__}). Retaining the last view; retrying.",
                    style="yellow",
                )
            )
        finally:
            self.reading = False
            pending, self.poll_pending = self.poll_pending, False
            if pending and self.is_running:
                self.call_later(self.poll_records)

    def on_unmount(self):
        self.poll_pending = False
        self.read_executor.shutdown(wait=False, cancel_futures=True)

    def on_resize(self):
        if self.is_mounted:
            self.resize_player_table()
            log = self.query_one("#activity", RichLog)
            follow = self.follow_log and log.is_vertical_scroll_end
            self.call_after_refresh(self.render_activity, follow_override=follow)
            self.call_after_refresh(self.render_diplomacy, force=True)
            self.call_after_refresh(self.render_strategy, force=True)
            self.call_after_refresh(self.render_specialists, force=True)

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated):
        if event.pane.id in {"strategy-report-tab", "strategy-trace-tab"}:
            self.call_after_refresh(self.render_strategy, force=True)
            return
        diplomatic = event.pane.id == "diplomacy-tab"
        reading = diplomatic or event.pane.id in {"strategy-tab", *(f"{role}-tab" for role in SPECIALIST_ROLES)}
        self.resize_player_table()
        self.query_one("#metrics", Static).display = not reading
        self.query_one("#scope", Static).display = not reading
        if diplomatic:
            self.call_after_refresh(self.render_diplomacy, force=True)
        elif event.pane.id == "activity-tab":
            self.call_after_refresh(self.render_activity)
        elif event.pane.id == "strategy-tab":
            self.call_after_refresh(self.render_strategy, force=True)
        else:
            self.call_after_refresh(self.render_specialists, force=True)

    def resize_player_table(self):
        compact = self.size.height < 30 and self.query_one("#main-tabs", TabbedContent).active in {
            "strategy-tab",
            *(f"{role}-tab" for role in SPECIALIST_ROLES),
        }
        self.query_one("#players", DataTable).styles.height = min(
            4 if compact else 10, max(4, len(self.snapshot.players) + 3)
        )

    def update_table(self, identity, rows):
        if self.table_values.get(identity) == rows:
            return
        self.table_values[identity] = rows
        table = self.query_one(f"#{identity}", DataTable)
        previous = table.ordered_rows[table.cursor_row].key.value if table.row_count else None
        offset = table.scroll_offset
        table.clear()
        cursor = 0
        for index, (key, cells) in enumerate(rows):
            table.add_row(*(cell if isinstance(cell, Text) else Text(str(cell)) for cell in cells), key=key)
            if key == previous:
                cursor = index
        if rows:
            table.move_cursor(row=cursor, animate=False, scroll=False)
        table.scroll_to(x=offset.x, y=offset.y, animate=False)

    def render_records(self):
        self.render_game_switcher()
        snapshot = self.snapshot
        run, result = snapshot.run, snapshot.result
        flight = run.get("inFlight") or {}
        review_wait = run.get("reviewPhase") or {}
        current = flight or review_wait or (snapshot.latest_turn() or {}).get("turn", {})
        status = str(run.get("status") or result.get("status") or "Waiting for match records").replace("_", " ")
        title = Text(
            f"{status.capitalize()}  ·  Turn {current.get('gameTurn', '—')}  ·  {visible_text(current.get('civilization', '—'))}",
            style="bold",
        )
        if review_wait:
            title.append(
                "  ·  "
                + visible_text(review_wait["stage"])
                + ": "
                + visible_text(", ".join(review_wait["civilizations"]))
            )
        if snapshot.updated_at is not None:
            age = max(0, int(time.time() - snapshot.updated_at))
            title.append(f"  ·  records updated {age}s ago", style="dim")
        latest_turn = (snapshot.latest_turn() or {}).get("turn", {})
        start, end = latest_turn.get("startedAtMs"), latest_turn.get("endedAtMs")
        if type(start) is int and latest_turn.get("handoff") == current.get("handoff"):
            wall = max(0, (end if type(end) is int else int(time.time() * 1000)) - start)
            title.append(f"  ·  turn wall {seconds(wall)}", style="dim")
        if self.frozen:
            title.append("  ·  VIEW FROZEN", style="bold yellow")
        if run.get("reason"):
            title.append("\n" + visible_text(run["reason"]), style="yellow")
        if review_wait:
            title.append(
                f"\nShared review after round {review_wait['reviewTurn']}; "
                + (
                    "finishing active conversations before the pause."
                    if review_wait["stage"] == "Finishing communication"
                    else "gameplay and communication are paused."
                ),
                style="yellow",
            )
        model_status = flight.get("modelStatus") or {}
        if (
            run.get("status") == "running"
            and flight.get("agentPhase") not in {"COMPLETED", "FAILED", "INTERRUPTED"}
            and not flight.get("negotiationWait")
            and model_status.get("state") in {"waiting", "retrying", "warning", "failed"}
        ):
            title.append("\n" + visible_text(status_text(model_status)), style="yellow")
        victory = result.get("result")
        if isinstance(victory, dict):
            title.append(
                f"\nWinner: {visible_text(victory.get('winner', '—'))} · {visible_text(victory.get('victoryType', ''))}",
                style="bold green",
            )
        for error in snapshot.errors[:2]:
            title.append("\n" + visible_text(error), style="yellow")
        self.query_one("#status", Static).update(title)
        negotiation = snapshot.negotiation
        banner = self.query_one("#negotiation", Static)
        banner.display = negotiation is not None
        if negotiation:
            deadline = negotiation.get("deadlineMillis")
            remaining = max(0, (deadline - int(time.time() * 1000)) // 1000) if deadline is not None else None
            timing = (
                "awaiting a decision; no response deadline"
                if remaining is None
                else f"{remaining}s remaining"
                if remaining
                else "deadline elapsed; awaiting host outcome"
            )
            banner.update(
                Text(
                    visible_text(
                        f"Gameplay paused: {negotiation.get('author')} ↔ {negotiation.get('partner')} · {trade_status(negotiation)} · {timing}"
                    ),
                    style="bold yellow",
                )
            )

        scores = {r["civilization"]: r for r in result.get("scores", [])}
        players = {r["civilization"]: r for r in result.get("players", [])}
        pending = {r["civilization"] for r in run.get("pendingFinalizations", [])}
        rows = []
        for name in snapshot.players:
            score, accounting = scores.get(name, {}), players.get(name, {})
            latest = snapshot.latest_turn(name)
            phase = "Waiting"
            if score.get("controller") == "BUILT_IN_AI":
                phase = "Built-in AI"
            elif name in pending:
                phase = "Bookkeeping"
            elif name in review_wait.get("civilizations", []):
                phase = review_wait["stage"]
            elif flight.get("civilization") == name:
                phase = (
                    "Human turn"
                    if flight.get("controller") == "HUMAN"
                    else str(flight.get("agentPhase", "Starting")).replace("_", " ").capitalize()
                )
                if phase == "Running" and flight.get("negotiationWait"):
                    phase = (
                        "Negotiating (model suspended)"
                        if flight["negotiationWait"]["state"] == "waiting"
                        else "Suspending for negotiation"
                    )
                elif phase == "Running" and flight.get("economyWait"):
                    phase = "Waiting for economy"
                elif phase == "Running" and model_status.get("state") in {"waiting", "retrying"}:
                    phase = "Model " + model_status["state"]
                if run.get("status") != "running":
                    phase = status.capitalize()
            elif score.get("controller") == "HUMAN":
                phase = "Human"
            if score.get("defeated"):
                phase = "Defeated"
            usage = accounting.get("usage", {})
            rows.append(
                (
                    name,
                    (
                        player_text(name, self.no_color, model=snapshot.player_model(name)),
                        phase,
                        str(latest["turn"]["gameTurn"]) if latest else "—",
                        count(score.get("score")),
                        count(usage.get("input_tokens")),
                        count(usage.get("cached_input_tokens")),
                        count(usage.get("output_tokens")),
                        count(api_calls(accounting.get("hostCalls"))),
                    ),
                )
            )
        self.update_table("players", rows)
        self.resize_player_table()

        scope = self.civilization or "All players"
        accounting = players.get(self.civilization, {}) if self.civilization else result
        usage = accounting.get("usage", {}) or (run.get("usage", {}) if self.civilization is None else {})
        complete = usage.get("complete") is True
        label = f"Recorded totals · {scope}"
        metrics = f"{label}:  {usage_text(usage)}\n"
        metrics += (
            f"Game API {count(api_calls(accounting.get('hostCalls')))} · "
            f"Model requests {count(accounting.get('modelRequests'))}"
        )
        metrics += " · Usage " + ("complete" if complete else "partial / not yet finalized")
        metrics += " · Cached and reasoning tokens are included in input and output."
        metrics += "\n" + memory_edits_text(accounting.get("memoryEdits"))
        self.query_one("#metrics", Static).update(Text(metrics))
        inspection = f"Recent activity · {scope}"
        self.query_one("#scope", Static).update(
            Text(
                f"{inspection}  |  {'Verbose' if self.verbose else 'Concise'}  |  Follow {'on' if self.follow_log else 'off'}  |  — = unavailable"
            )
        )

        self.render_activity()
        self.render_diplomacy()
        self.render_strategy()
        self.render_specialists()

    def render_specialists(self, *, force=False):
        for role in SPECIALIST_ROLES:
            views = [
                view
                for view in self.snapshot.specialists
                if view.role == role and (self.civilization is None or view.civilization == self.civilization)
            ]
            rows = []
            for view in views:
                job = view.job or {}
                telemetry, usage = job.get("telemetry") or {}, job.get("usage") or {}
                rows.append(
                    (
                        view.civilization,
                        (
                            player_text(view.civilization, self.no_color, model=telemetry.get("model")),
                            view.status,
                            count(usage.get("input_tokens")),
                            count(usage.get("output_tokens")),
                            seconds(telemetry.get("elapsedMs")),
                        ),
                    )
                )
            self.update_table(f"{role}-agents", rows)
            self.query_one(f"#{role}-agents", DataTable).styles.height = min(
                4 if self.size.height < 30 else 7, len(views) + 2
            )
            if not any(view.civilization == self.specialist_civilizations[role] for view in views):
                self.specialist_civilizations[role] = views[0].civilization if views else None
            view = next((view for view in views if view.civilization == self.specialist_civilizations[role]), None)
            heading = Text()
            if view:
                heading.append_text(player_text(view.civilization, self.no_color))
                heading.append(f" · {role.capitalize()} · {view.status}")
                if view.schedule and view.schedule.get("reasons"):
                    heading.append("\n" + visible_text("; ".join(view.schedule["reasons"])), style="dim")
                job = view.job or {}
                telemetry = job.get("telemetry") or {}
                if telemetry.get("model"):
                    heading.append(
                        "\n"
                        + visible_text(
                            f"{telemetry.get('provider') or 'unknown provider'} / {telemetry['model']} · "
                            f"{telemetry.get('reasoningEffort') or 'default'} reasoning"
                        ),
                        style="dim",
                    )
                if view.job:
                    heading.append("\n" + usage_text(job.get("usage") or {}), style="dim")
                    heading.append(
                        " · Usage " + ("complete" if telemetry.get("usageComplete") else "partial"), style="dim"
                    )
                notice = job.get("modelStatus") or {}
                if telemetry.get("phase") not in {
                    "ROLE_FINISHED",
                    "WAITING_NEGOTIATION",
                    "WAITING_ROLE",
                    "COMPLETED",
                } and notice.get("state") in {"waiting", "retrying", "warning", "failed"}:
                    heading.append("\n" + visible_text(status_text(notice)), style="yellow")
                for error in (job.get("error"), view.error):
                    if error:
                        heading.append("\n" + visible_text(error), style="yellow")
            else:
                heading.append(f"No {role} agents for this selection.", style="dim")
            status = self.query_one(f"#{role}-status", Static)
            status.styles.max_height = 5 if self.size.height < 30 else 7
            status.update(heading)
            if self.query_one("#main-tabs", TabbedContent).active != f"{role}-tab":
                continue
            selection = (
                self.snapshot.epoch,
                view.civilization if view else None,
                (view.job or {}).get("id") if view else None,
            )
            entries = tuple(entry for entry in view.activity if self.verbose or not entry.verbose) if view else ()
            content = (selection, entries)
            previous = self.last_specialists.get(role)
            if content == previous and not force:
                continue
            self.last_specialists[role] = content
            log = self.query_one(f"#{role}-activity", RichLog)
            offset = log.scroll_y
            changed = previous is None or previous[0] != selection
            follow = self.follow_log and (changed or log.is_vertical_scroll_end)
            text = Text()
            for entry in entries:
                text.append(
                    f"{event_time(entry.time)} · T{entry.turn} · {visible_text(entry.label)}\n", style="bold cyan"
                )
                text.append(visible_text(entry.text) + "\n\n")
            if not entries:
                text.append(
                    "Waiting for recorded specialist activity. Military activates during wars."
                    if role == "military"
                    else "Waiting for recorded economy activity.",
                    style="dim",
                )
            log.clear()
            log.write(text, scroll_end=False)
            if follow:
                self.call_after_refresh(log.scroll_end, animate=False)
            else:
                self.call_after_refresh(log.scroll_to, y=0 if changed else offset, animate=False)

    def render_strategy(self, *, force=False):
        views = [
            v for v in self.snapshot.strategies if self.civilization is None or v.civilization == self.civilization
        ]
        self.update_table(
            "strategies",
            [
                (
                    v.civilization,
                    (
                        player_text(v.civilization, self.no_color, model=self.snapshot.player_model(v.civilization)),
                        v.status,
                        v.report["turn"] if v.report else "—",
                        v.delivered_turn if v.delivered_turn is not None else "—",
                        seconds(((v.job or {}).get("telemetry") or {}).get("elapsedMs")),
                    ),
                )
                for v in views
            ],
        )
        self.query_one("#strategies", DataTable).styles.height = min(4 if self.size.height < 30 else 7, len(views) + 2)
        if not any(v.civilization == self.strategy_civilization for v in views):
            self.strategy_civilization = views[0].civilization if views else None
        view = next((v for v in views if v.civilization == self.strategy_civilization), None)
        compact = self.size.height < 30
        heading = Text()
        if view:
            heading.append_text(player_text(view.civilization, self.no_color))
            heading.append(f" · {view.status}")
            if view.pending:
                heading.append(f" · attempt {view.pending['attempts']}")
                heading.append_text(progress_text(view))
                if view.pending.get("error"):
                    heading.append("\nLast failure: " + visible_text(view.pending["error"]), style="yellow")
            if view.report:
                if compact and view.pending:
                    heading.append(
                        f"\nLatest report T{view.report['turn']} · preloaded T{view.delivered_turn if view.delivered_turn is not None else '—'}"
                    )
                else:
                    heading.append(f"\nLatest adviser report · based on turn {view.report['turn']} · ", style="bold")
                    heading.append(
                        f"provided to gameplay on turn {view.delivered_turn}."
                        if view.delivered_turn is not None
                        else "awaiting the next gameplay turn."
                    )
                    heading.append(" Recommendations remain advisory.", style="dim")
            if view.job:
                usage = view.job.get("usage") or {}
                if compact and view.pending:
                    heading.append(
                        f"\nReview tokens · input {number(usage.get('input_tokens'))} · output {number(usage.get('output_tokens'))}",
                        style="dim",
                    )
                else:
                    heading.append("\nReview usage · " + usage_text(usage), style="dim")
        else:
            heading.append("No agent strategy reports for this selection.", style="dim")
        self.query_one("#strategy-status", Static).update(heading)
        self.query_one("#strategy-status", Static).styles.max_height = 5 if compact else 7
        if self.query_one("#main-tabs", TabbedContent).active != "strategy-tab":
            return
        selection = (self.snapshot.epoch, self.strategy_civilization)
        new_selection = selection != self.last_strategy_selection
        self.last_strategy_selection = selection
        tabs = self.query_one("#strategy-content", TabbedContent)
        before = tabs.active
        if new_selection:
            tabs.active = "strategy-trace-tab" if view and view.pending else "strategy-report-tab"
        if tabs.active != before:
            # The tab activation renders after the newly visible pane has a layout.
            return
        if tabs.active == "strategy-trace-tab":
            self.render_strategy_trace(view, force=force)
            return
        content = (
            *selection,
            view.report if view else None,
            view.error if view else None,
            bool(view and view.pending),
        )
        if content == self.last_strategy and not force:
            return
        new_report = self.last_strategy is None or content[:2] != self.last_strategy[:2]
        self.last_strategy = content
        log = self.query_one("#strategy", RichLog)
        offset = log.scroll_y
        log.clear()
        log.write(
            report_text(view) if view else Text("Select an agent to read its latest completed report."),
            scroll_end=False,
        )
        self.call_after_refresh(log.scroll_to, y=0 if new_report else offset, animate=False)

    def render_strategy_trace(self, view, *, force=False):
        if self.query_one("#strategy-content", TabbedContent).active != "strategy-trace-tab":
            return
        key = (self.snapshot.epoch, self.strategy_civilization, (view.job or {}).get("id") if view else None)
        content = (key, view.trace if view else None)
        if content == self.last_strategy_trace and not force:
            return
        new_attempt = self.last_strategy_trace is None or self.last_strategy_trace[0] != key
        self.last_strategy_trace = content
        log = self.query_one("#strategy-trace", RichLog)
        offset = log.scroll_y
        follow = self.follow_log and (new_attempt or log.is_vertical_scroll_end)
        log.clear()
        log.write(trace_text(view) if view else Text("Select an agent to view its review trace."), scroll_end=follow)
        if not follow:
            self.call_after_refresh(log.scroll_to, y=0 if new_attempt else offset, animate=False)

    def render_diplomacy(self, *, force=False):
        groups = conversations(self.snapshot.diplomacy, self.civilization)
        self.update_table(
            "conversations",
            [
                (
                    c.key,
                    (
                        visible_text(c.title),
                        visible_text(c.status),
                        c.events[-1].turn if c.events[-1].turn is not None else "—",
                    ),
                )
                for c in groups
            ],
        )
        self.query_one("#conversations", DataTable).styles.height = min(
            4 if self.size.height < 30 else 8, len(groups) + 2
        )
        if not any(c.key == self.conversation for c in groups):
            self.conversation = groups[0].key if groups else None
        selected = next((c for c in groups if c.key == self.conversation), None)
        help_text = "Select a conversation · shared statements and confirmed game outcomes · social promises are labeled separately."
        if not self.snapshot.diplomacy_available:
            help_text = "Diplomacy records unavailable. Restart the host with the current build to publish saved communications."
        self.query_one("#diplomacy-help", Static).update(Text(help_text))
        if self.query_one("#main-tabs", TabbedContent).active != "diplomacy-tab":
            return
        content = (self.snapshot.epoch, selected)
        if content == self.last_diplomacy and not force:
            return
        new_selection = (
            self.last_diplomacy is None
            or self.last_diplomacy[0] != self.snapshot.epoch
            or ((self.last_diplomacy[1].key if self.last_diplomacy[1] else None) != self.conversation)
        )
        self.last_diplomacy = content
        log = self.query_one("#diplomacy", RichLog)
        offset = log.scroll_y
        follow = self.follow_log and (new_selection or log.is_vertical_scroll_end)
        text = Text()
        if selected:
            for event, detail in selected.transcript():
                label = (
                    "Social promise"
                    if event.kind in {"offer", "consent"}
                    else "Trade"
                    if event.kind == "trade"
                    else "Diplomacy"
                )
                text.append(f"T{event.turn if event.turn is not None else '?'} · {label}\n", style="bold cyan")
                text.append(detail + "\n\n")
        else:
            text.append("No recorded diplomatic conversations for this selection.", style="dim")
        log.clear()
        log.write(text, scroll_end=False)
        if follow:
            self.call_after_refresh(log.scroll_end, animate=False)
        else:
            self.call_after_refresh(log.scroll_to, y=offset, animate=False)

    def render_activity(self, *, follow_override=None):
        if self.query_one("#main-tabs", TabbedContent).active != "activity-tab":
            return
        entries = tuple(
            e
            for e in self.snapshot.activity
            if (self.civilization is None or e.civilization == self.civilization or self.civilization in e.participants)
            and (self.verbose or not e.verbose)
        )
        if entries == self.last_activity and follow_override is None:
            return
        new_selection = self.last_activity is None
        self.last_activity = entries
        log = self.query_one("#activity", RichLog)
        offset = log.scroll_y
        follow = self.follow_log and (new_selection or log.is_vertical_scroll_end)
        if follow_override is not None:
            follow = follow_override
        text = Text()
        for entry in entries:
            if entry.time:
                text.append(f"{event_time(entry.time)}  ", style="dim")
            text.append_text(player_text(entry.civilization, self.no_color))
            text.append(
                f" · T{entry.turn} · {visible_text(entry.label)}\n",
                style="bold"
                if self.no_color and entry.label in {"Action", "Error", "Warning"}
                else {"Action": "bright_green", "Error": "bold bright_red", "Warning": "yellow"}.get(
                    entry.label, "dim"
                ),
            )
            text.append(visible_text(entry.text) + "\n\n")
        if not entries:
            text.append("Waiting for recorded activity. Select a player to filter its history.", style="dim")
        log.clear()
        log.write(text, scroll_end=False)
        if follow:
            self.call_after_refresh(log.scroll_end, animate=False)
        else:
            self.call_after_refresh(log.scroll_to, y=offset, animate=False)

    async def on_data_table_row_selected(self, event: DataTable.RowSelected):
        for role in SPECIALIST_ROLES:
            if event.data_table.id == f"{role}-agents":
                self.specialist_civilizations[role] = str(event.row_key.value)
                self.render_specialists()
                return
        if event.data_table.id == "strategies":
            self.strategy_civilization = str(event.row_key.value)
            self.render_strategy()
            return
        if event.data_table.id == "conversations":
            self.conversation = str(event.row_key.value)
            self.render_diplomacy()
            return
        if event.data_table.id == "players":
            self.civilization = str(event.row_key.value)
        else:
            return
        self.last_activity = None
        self.render_records()
        await self.poll_records()

    async def action_all_players(self):
        self.civilization, self.last_activity = None, None
        self.render_records()
        await self.poll_records()

    async def action_live(self):
        self.last_activity = None
        self.follow_log = True
        self.query_one("#activity", RichLog).scroll_end(animate=False)
        self.query_one("#strategy-trace", RichLog).scroll_end(animate=False)
        for role in SPECIALIST_ROLES:
            self.query_one(f"#{role}-activity", RichLog).scroll_end(animate=False)
        await self.poll_records()

    async def action_freeze(self):
        self.frozen = not self.frozen
        self.render_records()
        await self.poll_records()

    def action_follow(self):
        self.follow_log = not self.follow_log
        if self.follow_log:
            self.query_one("#activity", RichLog).scroll_end(animate=False)
            self.query_one("#diplomacy", RichLog).scroll_end(animate=False)
            self.query_one("#strategy-trace", RichLog).scroll_end(animate=False)
            for role in SPECIALIST_ROLES:
                self.query_one(f"#{role}-activity", RichLog).scroll_end(animate=False)
        self.render_records()

    def action_verbose(self):
        self.verbose = not self.verbose
        self.last_activity = None
        self.render_records()

    def action_diplomacy(self):
        self.query_one("#main-tabs", TabbedContent).active = "diplomacy-tab"
        self.query_one("#conversations", DataTable).focus()

    def action_strategy(self):
        self.query_one("#main-tabs", TabbedContent).active = "strategy-tab"
        self.query_one("#strategies", DataTable).focus()

    def action_economy(self):
        self.query_one("#main-tabs", TabbedContent).active = "economy-tab"
        self.query_one("#economy-agents", DataTable).focus()

    def action_military(self):
        self.query_one("#main-tabs", TabbedContent).active = "military-tab"
        self.query_one("#military-agents", DataTable).focus()

    def game_subtitle(self):
        prefix = f"[{self.game_index + 1}/{len(self.games)}] {self.game_id} · " if len(self.games) > 1 else ""
        return visible_text(prefix + str(self.game_directory))

    def render_game_switcher(self):
        banner = self.query_one("#game-switcher", Static)
        banner.display = len(self.games) > 1
        if len(self.games) <= 1:
            return
        statuses = {}
        try:
            manifest = json.loads((self.state_directory / "batch.json").read_text())
            statuses = {
                row["id"]: row.get("status", "unknown")
                for row in manifest.get("runs", [])
                if isinstance(row, dict) and isinstance(row.get("id"), str)
            }
        except (OSError, ValueError, TypeError):
            pass
        text = Text(f"Game {self.game_index + 1}/{len(self.games)} · {self.game_id}", style="bold")
        text.append("  ·  [ previous  ] next", style="dim")
        text.append("\n")
        for index, (game_id, _) in enumerate(self.games):
            if index:
                text.append("  ")
            style = "bold bright_cyan" if index == self.game_index else "dim"
            text.append(f"{game_id} {statuses.get(game_id, 'unknown')}", style=style)
        banner.update(text)

    async def select_game(self, offset):
        if len(self.games) <= 1:
            return
        self.game_index = (self.game_index + offset) % len(self.games)
        self.game_id, self.game_directory = self.games[self.game_index]
        self.reader = WatchReader(self.game_directory)
        self.sub_title = self.game_subtitle()
        self.civilization = self.conversation = self.strategy_civilization = None
        self.snapshot = WatchSnapshot({}, {}, (), (), (), None)
        self.table_values.clear()
        self.last_activity = self.last_diplomacy = self.last_strategy_selection = None
        self.last_strategy = self.last_strategy_trace = None
        self.specialist_civilizations = dict.fromkeys(SPECIALIST_ROLES)
        self.last_specialists.clear()
        self.frozen = False
        self.render_records()
        await self.poll_records()

    async def action_previous_game(self):
        await self.select_game(-1)

    async def action_next_game(self):
        await self.select_game(1)


def parser(*, add_help=True):
    result = argparse.ArgumentParser(add_help=add_help, description=__doc__)
    result.add_argument(
        "--state-dir",
        type=Path,
        default=Path(os.environ.get("UNCIV_HOST_DIR", Path.home() / ".local/state/unciv-host/vanilla-full")),
        help="Match directory or batch root",
    )
    result.add_argument(
        "--verbose", action="store_true", help="Show startup context, tool details, and exposed reasoning summaries"
    )
    result.add_argument(
        "--local", action="store_true", help="Use the installed dashboard instead of the game's pinned container image"
    )
    return result


def run(args):
    root = args.state_dir.expanduser().resolve()
    if not root.is_dir():
        print(f"Match directory does not exist: {root}", file=sys.stderr)
        return 2
    from civ_agent.container_support import IN_CONTAINER, read_json, watch_mounts

    batch = (root / "batch.json").is_file()
    if not args.local and not batch and not IN_CONTAINER and (root / "container.json").exists():
        import subprocess

        manifest = read_json(root / "container.json")
        arguments = [
            "podman",
            "run",
            "--rm",
            "-it",
            "--network=none",
            "--userns=keep-id",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,size=64m,mode=1777",
            *watch_mounts(root),
            manifest["images"]["runtime"],
            "watch",
        ]
        if args.verbose:
            arguments.append("--verbose")
        return subprocess.call(arguments)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(
            "The dashboard requires an interactive terminal. Run civ-agent match watch in a terminal attached to this match directory.",
            file=sys.stderr,
        )
        return 2
    try:
        app = MatchWatch(root, verbose=args.verbose)
    except (OSError, ValueError, TypeError) as error:
        print(f"Cannot open match dashboard: {error}", file=sys.stderr)
        return 2
    app.run()
    return 0
