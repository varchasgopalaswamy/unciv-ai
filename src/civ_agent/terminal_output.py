"""One append-only terminal renderer shared by the scheduler and its history workers."""

import hashlib
import os
import re
import sys
import threading
import unicodedata

from rich.console import Console
from rich.text import Text

ANSI = re.compile(r"\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-_])")
PLAYER_COLORS = (
    "bright_cyan",
    "bright_magenta",
    "bright_green",
    "bright_yellow",
    "bright_blue",
    "bright_red",
    "cyan",
    "magenta",
    "green",
    "yellow",
    "blue",
    "red",
)
KNOWN_PLAYER_COLORS = {
    "Inca": "bright_yellow",
    "France": "bright_blue",
    "Greece": "bright_cyan",
    "Arabia": "bright_green",
    "China": "bright_red",
    "Persia": "bright_magenta",
    "Rome": "magenta",
    "Egypt": "yellow",
}


def visible_text(value):
    value = ANSI.sub("", str(value)).replace("\r\n", "\n").replace("\r", "\n")
    return "".join(c for c in value if c in "\n\t" or unicodedata.category(c) not in {"Cc", "Cf", "Cs"})


def color_enabled(mode="auto", stream=None):
    if mode not in {"auto", "always", "never"}:
        raise ValueError("Invalid color mode")
    if mode == "never" or "NO_COLOR" in os.environ:
        return False
    return mode == "always" or (
        bool(getattr(stream or sys.stdout, "isatty", lambda: False)()) and os.environ.get("TERM") != "dumb"
    )


def player_style(civilization):
    return KNOWN_PLAYER_COLORS.get(
        civilization,
        PLAYER_COLORS[int.from_bytes(hashlib.sha256(civilization.encode()).digest()[:2]) % len(PLAYER_COLORS)],
    )


class TerminalOutput:
    def __init__(self, *, color="auto", file=None, width=None):
        enabled = color_enabled(color, file)
        self.console = Console(
            file=file,
            width=width,
            force_terminal=enabled,
            color_system="standard" if enabled else None,
            no_color=not enabled,
            markup=False,
            highlight=False,
        )
        self.lock = threading.RLock()
        self.open_key = None
        self.last_key = None

    def finish_line(self, owner=None):
        with self.lock:
            if self.open_key is not None and (owner is None or self.open_key[0] == owner):
                self.console.print()
                self.open_key = None

    def prefix(self, civilization, label):
        text = Text("  ")
        if civilization:
            text.append(f"[{visible_text(civilization)}] ", style="bold " + player_style(civilization))
        style = {"Error": "bold red", "Warning": "yellow", "Action": "green", "Done": "bold green"}.get(label, "dim")
        text.append(visible_text(label) + ": ", style=style)
        return text

    def line(self, label, value, *, civilization=None):
        with self.lock:
            self.finish_line()
            self.last_key = None
            lines = visible_text(value).splitlines() or [""]
            for index, line in enumerate(lines):
                text = self.prefix(civilization, label) if index == 0 else Text("    ")
                text.append(line)
                # Let terminals wrap visually; keep complete lines in redirected logs.
                self.console.print(text, soft_wrap=True)

    def notice(self, value, *, level="Status"):
        self.line(level, value)

    def turn(self, civilization, game_turn):
        with self.lock:
            self.finish_line()
            self.last_key = None
            self.console.print()
            self.console.rule(
                Text(f"Turn {game_turn} · {visible_text(civilization)}", style="bold " + player_style(civilization)),
                align="left",
                style=player_style(civilization),
            )

    def stream(self, owner, item, civilization, label, value, *, final=False):
        key = (owner, item)
        with self.lock:
            if self.open_key is not None and self.open_key != key:
                self.finish_line()
            for segment in visible_text(value).splitlines(keepends=True):
                text = Text()
                if self.open_key is None:
                    text = self.prefix(civilization, label) if self.last_key != key else Text("    ")
                text.append(segment)
                self.console.print(text, end="", soft_wrap=True)
                self.open_key = None if segment.endswith("\n") else key
                self.last_key = key
            if final:
                self.finish_line(owner)
            self.console.file.flush()
