"""An attachable terminal dashboard for local match records."""

import argparse
import asyncio
import os
import sys
import time
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
from civ_agent.watch_state import WatchReader, WatchSnapshot, event_time


def count(value):
    return "—" if value is None else number(value)


def player_text(name, no_color=False):
    color = player_style(name)
    if not color.startswith("bright_"):
        color = "bright_" + color
    return Text(visible_text(name), style="bold" if no_color else "bold " + color)


def usage_text(usage):
    return (
        f"Input {number(usage.get('input_tokens'))} · cached {number(usage.get('cached_input_tokens'))}  |  "
        f"Output {number(usage.get('output_tokens'))} · reasoning {number(usage.get('reasoning_output_tokens'))}"
    )


class MatchWatch(App):
    TITLE = "Unciv · Match dashboard"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { background: $surface; }
    Header { background: $panel; }
    #status { height: auto; min-height: 2; max-height: 5; padding: 0 1; }
    #negotiation { height: auto; padding: 0 1; color: $warning; }
    #players { height: 7; border: round $primary; margin: 0 1; }
    #metrics { height: auto; max-height: 5; padding: 0 2; }
    #scope { height: 1; padding: 0 2; color: $text-muted; }
    TabbedContent { height: 1fr; margin: 0 1; }
    TabPane { padding: 0; }
    RichLog { height: 1fr; padding: 0 1; }
    #turns, #calls { height: 1fr; }
    #conversations { height: auto; max-height: 8; }
    #diplomacy-help { height: auto; padding: 0 1; color: $text-muted; }
    #strategies { height: auto; max-height: 7; }
    #strategy-status { height: auto; max-height: 7; padding: 0 1; }
    #call-help { height: auto; padding: 0 1; color: $text-muted; }
    Footer { background: $panel; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit viewer", priority=True),
        Binding("ctrl+c", "quit", show=False, priority=True),
        Binding("space", "freeze", "Freeze view", priority=True),
        Binding("a", "all_players", "All players"),
        Binding("l", "live", "Live turn"),
        Binding("f", "follow", "Follow log"),
        Binding("v", "verbose", "Verbose"),
        Binding("d", "diplomacy", "Diplomacy"),
        Binding("s", "strategy", "Strategy"),
    ]

    def __init__(self, state_directory: Path, *, verbose=False, refresh=1.0):
        super().__init__()
        self.reader = WatchReader(state_directory)
        self.sub_title = visible_text(str(state_directory))
        self.verbose = verbose
        self.refresh_seconds = refresh
        self.frozen = False
        self.follow_log = True
        self.civilization: str | None = None
        self.selected: tuple[str, int] | None = None
        self.snapshot = WatchSnapshot({}, {}, (), (), (), None)
        self.table_values = {}
        self.last_activity = None
        self.reading = False
        self.turn_rows: dict[str, tuple[str, int]] = {}
        self.conversation: str | None = None
        self.last_diplomacy = None
        self.strategy_civilization: str | None = None
        self.last_strategy_selection = None
        self.last_strategy = None
        self.last_strategy_trace = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
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
            with TabPane("Turns · Enter to inspect", id="turns-tab"):
                yield DataTable(id="turns", cursor_type="row", zebra_stripes=True)
            with TabPane("API calls", id="calls-tab"):
                yield Static(
                    "Host calls for the selected turn. Game API totals count tool: rows; command: rows are a breakdown.",
                    id="call-help",
                    markup=False,
                )
                yield DataTable(id="calls", cursor_type="row", zebra_stripes=True)
        yield Footer()

    async def on_mount(self):
        for identity, columns in {
            "players": ("Player", "Status", "Turn", "Score", "Input", "Cached", "Output", "Game API"),
            "turns": ("Turn", "Player", "Status", "Gameplay", "Bookkeeping", "Game API", "Input", "Output", "Usage"),
            "calls": ("API", "Calls", "Errors", "Replays", "Time"),
            "conversations": ("Participants", "Status", "Latest turn"),
            "strategies": ("Player", "Review status", "Report turn", "Preloaded turn", "Attempt time"),
        }.items():
            self.query_one(f"#{identity}", DataTable).add_columns(*columns)
        self.query_one("#players", DataTable).border_title = "Players · Enter to filter"
        self.query_one("#activity", RichLog).focus()
        await self.poll_records()
        self.set_interval(self.refresh_seconds, self.poll_records)

    async def poll_records(self):
        if self.frozen or self.reading:
            return
        self.reading = True
        selection = self.selected
        try:
            snapshot = await asyncio.to_thread(self.reader.read, selection)
            if selection == self.selected and not self.frozen:
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

    def on_resize(self):
        if self.is_mounted:
            self.resize_player_table()
            log = self.query_one("#activity", RichLog)
            follow = self.follow_log and log.is_vertical_scroll_end
            self.call_after_refresh(self.render_activity, follow_override=follow)
            self.call_after_refresh(self.render_diplomacy, force=True)
            self.call_after_refresh(self.render_strategy, force=True)

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated):
        if event.pane.id in {"strategy-report-tab", "strategy-trace-tab"}:
            self.call_after_refresh(self.render_strategy, force=True)
            return
        diplomatic = event.pane.id == "diplomacy-tab"
        reading = diplomatic or event.pane.id == "strategy-tab"
        self.resize_player_table()
        self.query_one("#metrics", Static).display = not reading
        self.query_one("#scope", Static).display = not reading
        if diplomatic:
            self.call_after_refresh(self.render_diplomacy, force=True)
        elif event.pane.id == "activity-tab":
            self.call_after_refresh(self.render_activity)
        elif event.pane.id == "strategy-tab":
            self.call_after_refresh(self.render_strategy, force=True)

    def resize_player_table(self):
        compact = self.size.height < 30 and self.query_one("#main-tabs", TabbedContent).active == "strategy-tab"
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

    def selected_record(self):
        if self.selected:
            return next(
                (
                    r
                    for r in reversed(self.snapshot.turns)
                    if (r["turn"]["civilization"], r["turn"]["gameTurn"]) == self.selected
                ),
                None,
            )
        return self.snapshot.latest_turn(self.civilization)

    def render_records(self):
        snapshot = self.snapshot
        run, result = snapshot.run, snapshot.result
        flight = run.get("inFlight") or {}
        review_wait = run.get("waitingForReview") or {}
        current = flight or review_wait or (snapshot.latest_turn() or {}).get("turn", {})
        status = str(run.get("status") or result.get("status") or "Waiting for match records").replace("_", " ")
        title = Text(
            f"{status.capitalize()}  ·  Turn {current.get('gameTurn', '—')}  ·  {visible_text(current.get('civilization', '—'))}",
            style="bold",
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
                f"\nWaiting for strategic review from turn {review_wait['review']['turn']} before this player's next turn.",
                style="yellow",
            )
        model_status = flight.get("modelStatus") or {}
        if (
            run.get("status") == "running"
            and flight.get("agentPhase") not in {"COMPLETED", "FAILED", "INTERRUPTED"}
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
            remaining = max(0, (negotiation.get("deadlineMillis", 0) - int(time.time() * 1000)) // 1000)
            timing = f"{remaining}s remaining" if remaining else "deadline elapsed; awaiting host outcome"
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
            if score.get("controller") in {"BUILT_IN_AI", "HUMAN"}:
                phase = "Built-in AI" if score["controller"] == "BUILT_IN_AI" else "Human"
            elif name in pending:
                phase = "Bookkeeping"
            elif review_wait.get("civilization") == name:
                phase = "Review wait"
            elif flight.get("civilization") == name:
                phase = str(flight.get("agentPhase", "Starting")).replace("_", " ").capitalize()
                if phase == "Running" and model_status.get("state") in {"waiting", "retrying"}:
                    phase = "Model " + model_status["state"]
                if run.get("status") != "running":
                    phase = status.capitalize()
            if score.get("defeated"):
                phase = "Defeated"
            usage = accounting.get("usage", {})
            rows.append(
                (
                    name,
                    (
                        player_text(name, self.no_color),
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
        if self.selected:
            accounting = self.selected_record() or {}
            usage = accounting.get("usage", {})
            complete = accounting.get("accountingComplete") is True
            label = f"Turn {self.selected[1]} · {scope}"
        metrics = f"{label}:  {usage_text(usage)}\n"
        metrics += f"Game API {count(api_calls(accounting.get('hostCalls')))} · Model requests unavailable"
        metrics += " · Usage " + ("complete" if complete else "partial / not yet finalized")
        metrics += " · Cached and reasoning tokens are included in input and output."
        metrics += "\n" + memory_edits_text(accounting.get("memoryEdits"))
        self.query_one("#metrics", Static).update(Text(metrics))
        inspection = f"Turn {self.selected[1]} · {scope}" if self.selected else f"Recent activity · {scope}"
        self.query_one("#scope", Static).update(
            Text(
                f"{inspection}  |  {'Verbose' if self.verbose else 'Concise'}  |  Follow {'on' if self.follow_log else 'off'}  |  — = unavailable"
            )
        )

        turn_rows = []
        self.turn_rows = {}
        for record in reversed(snapshot.turns):
            turn = record["turn"]
            if self.civilization and turn["civilization"] != self.civilization:
                continue
            key = str(turn["handoff"])
            self.turn_rows[key] = (turn["civilization"], turn["gameTurn"])
            turn_rows.append(
                (
                    key,
                    (
                        str(turn["gameTurn"]),
                        player_text(turn["civilization"], self.no_color),
                        str(turn.get("status", "—")),
                        seconds(record.get("gameplayElapsedMs")),
                        seconds(record.get("finalizationElapsedMs")),
                        count(api_calls(record.get("hostCalls"))),
                        count(record.get("usage", {}).get("input_tokens")),
                        count(record.get("usage", {}).get("output_tokens")),
                        "Complete" if record.get("accountingComplete") else "Partial",
                    ),
                )
            )
        self.update_table("turns", turn_rows)
        selected = self.selected_record() or {}
        calls = selected.get("hostCalls", {})
        self.update_table(
            "calls",
            [
                (
                    name,
                    (
                        visible_text(name),
                        count(value.get("calls")),
                        count(value.get("errors")),
                        count(value.get("replayed")),
                        seconds(value.get("elapsedMs")),
                    ),
                )
                for name, value in sorted(calls.items())
                if isinstance(value, dict)
            ],
        )
        turn = selected.get("turn", {})
        self.query_one("#call-help", Static).update(
            Text(
                f"Turn {turn.get('gameTurn', '—')} · {visible_text(turn.get('civilization', '—'))}. Host tool: calls count toward Game API; command: rows are a breakdown."
            )
        )
        self.render_activity()
        self.render_diplomacy()
        self.render_strategy()

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
                        player_text(v.civilization, self.no_color),
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
        groups = conversations(self.snapshot.diplomacy, self.civilization, self.selected[1] if self.selected else None)
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
        elif self.selected:
            help_text = f"Conversations active on turn {self.selected[1]}; full conversation history shown."
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
            and (self.selected is None or e.turn == self.selected[1])
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
            text.append("Waiting for recorded activity. Select a player or a turn to inspect its history.", style="dim")
        log.clear()
        log.write(text, scroll_end=False)
        if follow:
            self.call_after_refresh(log.scroll_end, animate=False)
        else:
            self.call_after_refresh(log.scroll_to, y=offset, animate=False)

    async def on_data_table_row_selected(self, event: DataTable.RowSelected):
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
            self.selected = None
        elif event.data_table.id == "turns":
            self.selected = self.turn_rows[str(event.row_key.value)]
            self.civilization = self.selected[0]
            self.query_one("#main-tabs", TabbedContent).active = "activity-tab"
        else:
            return
        self.last_activity = None
        self.render_records()
        await self.poll_records()

    async def action_all_players(self):
        self.civilization, self.selected, self.last_activity = None, None, None
        self.render_records()
        await self.poll_records()

    async def action_live(self):
        self.selected, self.last_activity = None, None
        self.follow_log = True
        self.query_one("#activity", RichLog).scroll_end(animate=False)
        self.query_one("#strategy-trace", RichLog).scroll_end(animate=False)
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


def parser(*, add_help=True):
    result = argparse.ArgumentParser(add_help=add_help, description=__doc__)
    result.add_argument(
        "--state-dir",
        type=Path,
        default=Path(os.environ.get("UNCIV_HOST_DIR", Path.home() / ".local/state/unciv-host/vanilla-full")),
    )
    result.add_argument(
        "--verbose", action="store_true", help="Show startup context, tool details, and exposed reasoning summaries"
    )
    return result


def run(args):
    root = args.state_dir.expanduser().resolve()
    if not root.is_dir():
        print(f"Match directory does not exist: {root}", file=sys.stderr)
        return 2
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(
            "The dashboard requires an interactive terminal. Run civ-agent match watch in a terminal attached to this match directory.",
            file=sys.stderr,
        )
        return 2
    MatchWatch(root, verbose=args.verbose).run()
    return 0
