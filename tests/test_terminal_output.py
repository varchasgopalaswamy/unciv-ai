"""Terminal presentation preserves audit data, player identity, and accounting scope."""

import io
from concurrent.futures import ThreadPoolExecutor

from civ_agent.monitoring import MonitorStore, path_for
from civ_agent.terminal_output import TerminalOutput, color_enabled, player_style, visible_text
from civ_agent.turn_display import display_turn, turn_metrics


def event(sequence, kind, **data):
    return {"sequence": sequence, "kind": kind, "data": data}


def test_color_policy_and_literal_untrusted_text(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    output = io.StringIO()
    assert not color_enabled("auto", output)
    renderer = TerminalOutput(color="always", file=output, width=120)
    renderer.line("Action", "[bold red]literal[/bold red]\x1b[2J\x1b]0;attack\x07", civilization="Rome")
    rendered = output.getvalue()
    assert "\x1b[" in rendered
    assert "[bold red]literal[/bold red]" in visible_text(rendered)
    assert "\x1b[2J" not in rendered and "attack" not in rendered
    monkeypatch.setenv("NO_COLOR", "")
    assert not color_enabled("always", output)
    plain = io.StringIO()
    TerminalOutput(color="always", file=plain).line("Error", "Visible without color")
    assert "\x1b" not in plain.getvalue()


def test_benchmark_player_colors_are_distinct():
    colors = [player_style(name) for name in ("Inca", "France", "Greece", "Arabia", "China", "Persia")]
    assert colors == [
        "bright_yellow",
        "bright_blue",
        "bright_cyan",
        "bright_green",
        "bright_red",
        "bright_magenta",
    ]
    assert len(set(colors)) == 6


def test_background_output_cannot_join_another_players_partial_line():
    output = io.StringIO()
    renderer = TerminalOutput(color="never", file=output)
    renderer.stream("rome-job", "message", "Rome", "Assistant", "Planning")
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(renderer.line, "Done", "Turn 1 complete", civilization="Greece").result()
    renderer.stream("rome-job", "message", "Rome", "Assistant", " the next move.\nSecond line", final=True)
    assert output.getvalue().splitlines() == [
        "  [Rome] Assistant: Planning",
        "  [Greece] Done: Turn 1 complete",
        "  [Rome] Assistant:  the next move.",
        "    Second line",
    ]


def test_turn_summary_uses_matching_host_totals_without_double_counting(tmp_path):
    flight = {"epoch": "epoch", "civilization": "Rome", "gameTurn": 4, "handoff": 12, "activationId": "job"}
    metrics = {
        "turn": {key: value for key, value in flight.items() if key != "activationId"},
        "jobs": ["job"],
        "accountingComplete": True,
        "gameplayElapsedMs": 12400,
        "finalizationElapsedMs": 300,
        "usage": {
            "input_tokens": 24000,
            "cached_input_tokens": 19000,
            "output_tokens": 900,
            "reasoning_output_tokens": 400,
        },
        "hostCalls": {
            "tool:player_command": {"calls": 3},
            "tool:player_turn": {"calls": 2},
            "command:END_TURN": {"calls": 1},
        },
        "modelRequests": 7,
        "memoryEdits": {
            "batches": 3,
            "accepted": 5,
            "rejected": 2,
            "rejectedBatches": 1,
            "rejectionReasons": {"SOURCE_NOT_DELIVERED": 2},
            "complete": True,
        },
    }
    (tmp_path / "telemetry").mkdir()
    MonitorStore(path_for(tmp_path)).put("turns", f"{metrics['turn']['epoch']}/{metrics['turn']['handoff']}", metrics)
    output = io.StringIO()
    display_turn(TerminalOutput(color="never", file=output), tmp_path, flight, {})
    text = output.getvalue()
    assert "Game API 5" in text and "Model requests 7" in text
    assert "Memory edits 5 accepted" in text and "2 rejected" in text and "SOURCE_NOT_DELIVERED: 2" in text
    assert "12.4s gameplay" in text and "0.3s bookkeeping" in text
    assert "Input 24,000 (cached 19,000)" in text
    assert "Output 900 (reasoning 400)" in text and "Turn usage complete" in text
    for key, other in (
        ("epoch", "old"),
        ("civilization", "Greece"),
        ("gameTurn", 5),
        ("handoff", 13),
        ("activationId", "other"),
    ):
        assert turn_metrics(tmp_path, {**flight, key: other}) is None
    # Earlier attempts may have reported tokens even if this attempt has not.
    metrics["accountingComplete"] = False
    MonitorStore(path_for(tmp_path)).put("turns", f"{metrics['turn']['epoch']}/{metrics['turn']['handoff']}", metrics)
    output.seek(0)
    output.truncate()
    display_turn(TerminalOutput(color="never", file=output), tmp_path, flight, {"usageReported": False})
    assert "Input 24,000" in output.getvalue() and "Turn usage partial" in output.getvalue()


def test_missing_accounting_is_unavailable_not_zero_and_partial_usage_is_labelled(tmp_path):
    flight = {"civilization": "Rome", "gameTurn": 2}
    output = io.StringIO()
    renderer = TerminalOutput(color="never", file=output)
    display_turn(renderer, tmp_path, flight, {"usage": {"input_tokens": 0}, "usageReported": False})
    assert "Usage unavailable" in output.getvalue()
    assert "Game API unavailable" in output.getvalue()
    assert "Memory edits unavailable" in output.getvalue()
    output.seek(0)
    output.truncate()
    display_turn(
        renderer,
        tmp_path,
        flight,
        {"usageReported": True, "usageComplete": False, "usage": {"input_tokens": 120}, "gameplayElapsedMs": 2000},
    )
    assert "Input 120" in output.getvalue() and "Activation usage partial" in output.getvalue()
    assert "2.0s gameplay (activation)" in output.getvalue()
