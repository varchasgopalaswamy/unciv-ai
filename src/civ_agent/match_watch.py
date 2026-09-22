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

from civ_agent.model_status import status_text
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
    #players { height: 7; border: round $primary; margin: 0 1; }
    #metrics { height: auto; max-height: 5; padding: 0 2; }
    #scope { height: 1; padding: 0 2; color: $text-muted; }
    TabbedContent { height: 1fr; margin: 0 1; }
    TabPane { padding: 0; }
    RichLog { height: 1fr; padding: 0 1; }
    #turns, #calls { height: 1fr; }
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

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("Waiting for match records…", id="status", markup=False)
        yield DataTable(id="players", cursor_type="row", zebra_stripes=True)
        yield Static(id="metrics", markup=False)
        yield Static(id="scope", markup=False)
        with TabbedContent(initial="activity-tab"):
            with TabPane("Activity", id="activity-tab"):
                yield RichLog(id="activity", wrap=True, min_width=1, max_lines=6000, markup=False)
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
        }.items():
            self.query_one(f"#{identity}", DataTable).add_columns(*columns)
        self.query_one("#players", DataTable).border_title = "Players · Enter to filter activity"
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
            log = self.query_one("#activity", RichLog)
            follow = self.follow_log and log.is_vertical_scroll_end
            self.call_after_refresh(self.render_activity, follow_override=follow)

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
        current = flight or (snapshot.latest_turn() or {}).get("turn", {})
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
        self.query_one("#players", DataTable).styles.height = min(10, max(4, len(rows) + 3))

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

    def render_activity(self, *, follow_override=None):
        entries = tuple(
            e
            for e in self.snapshot.activity
            if (self.civilization is None or e.civilization == self.civilization)
            and (self.verbose or not e.verbose)
            and (self.selected is None or (e.civilization, e.turn) == self.selected)
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
        if event.data_table.id == "players":
            self.civilization = str(event.row_key.value)
            self.selected = None
        elif event.data_table.id == "turns":
            self.selected = self.turn_rows[str(event.row_key.value)]
            self.civilization = self.selected[0]
            self.query_one(TabbedContent).active = "activity-tab"
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
        await self.poll_records()

    async def action_freeze(self):
        self.frozen = not self.frozen
        self.render_records()
        await self.poll_records()

    def action_follow(self):
        self.follow_log = not self.follow_log
        if self.follow_log:
            self.query_one("#activity", RichLog).scroll_end(animate=False)
        self.render_records()

    def action_verbose(self):
        self.verbose = not self.verbose
        self.last_activity = None
        self.render_records()


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
