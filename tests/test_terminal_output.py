"""Terminal presentation preserves audit data, player identity, and accounting scope."""

import asyncio
import io
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from civ_agent.agent_history import HistoryStore
from civ_agent.monitoring import MonitorStore, path_for
from civ_agent.protocol import JsonObject
from civ_agent.runner_history import EventHistory
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


def test_quiet_output_keeps_receipts_errors_and_identical_audit_records(tmp_path):
    events = [
        event(0, "developer_message", source="service_startup", text="REPEATED INSTRUCTIONS"),
        event(1, "user_message", source="service_startup", text="PRELOADED READS"),
        event(2, "action", itemId="action:one", description="Founded Rome at (1, 2)."),
        event(3, "tool_call", itemId="read", tool="unit_details", arguments={}),
        event(4, "tool_result", itemId="read", result={"isError": False, "structuredContent": {"units": []}}),
        event(
            5,
            "tool_result",
            itemId="failed",
            result={"isError": True, "structuredContent": {"error": "DETAIL_REQUIRED"}},
        ),
        event(6, "assistant_message", itemId="m", text="Next I will research Mining."),
    ]
    histories = []
    for verbose in (False, True):
        output = io.StringIO()
        renderer = TerminalOutput(color="never", file=output)
        history = EventHistory(
            tmp_path / str(verbose), "epoch", "Rome", "job", game_turn=0, verbose=verbose, renderer=renderer
        )
        history.append(events)
        text = output.getvalue()
        assert ("PRELOADED READS" in text) is verbose
        assert ("REPEATED INSTRUCTIONS" in text) is verbose
        assert ("unit_details" in text) is verbose
        assert "Founded Rome at (1, 2)." in text and "DETAIL_REQUIRED" in text
        assert str(history.store.path) in text
        assert "Next I will research Mining." in text
        histories.append(history)
    assert histories[0].events == histories[1].events
    quiet = io.StringIO()
    resumed = EventHistory(
        tmp_path / "False", "epoch", "Rome", "job", game_turn=0, renderer=TerminalOutput(color="never", file=quiet)
    )
    resumed.append(events + [event(7, "action", itemId="action:two", description="Research queue: Mining.")])
    assert "Founded" not in quiet.getvalue() and "Research queue: Mining." in quiet.getvalue()


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


def test_gateway_action_event_requires_a_receipt_and_does_not_repeat_replays(tmp_path):
    from civ_agent.civilization_state import CivilizationState
    from civ_agent.proposal_gateway import ProposalGateway
    from civ_agent.service_io import ServiceIO
    from tests.test_agent_service import StartupHost

    class ReceiptHost(StartupHost):
        async def tool(self, name, args):
            result: JsonObject = await super().tool(name, args)
            if name == "player_turn" and self.executed:
                result["turnReview"]["actions"] = [{"kind": "FOUND_CITY", "description": "Founded Rome at (1, 2)."}]
            return result

    async def run():
        io_worker = ServiceIO()
        memory = CivilizationState(tmp_path / "memory", "Rome")
        gateway = ProposalGateway(memory, io_worker, tmp_path / "workspace")
        gateway.history = HistoryStore(tmp_path / "history", "service-token")
        record = {"activationId": "job", "phase": "RUNNING"}
        gateway.history.begin(record, None, "player-token")
        host = ReceiptHost(
            SimpleNamespace(service=SimpleNamespace(active_id="job", records={"job": {"epoch": "g1", "gameTurn": 0}}))
        )
        host.view["map"]["units"][0]["canFoundCity"] = True
        try:
            job = await gateway.open("job", "gameplay", "g1", host, {}, 0)
            view = await gateway.call(job, "player_turn", {}, "view")
            settlement = view["units"][0]["inspections"]["FOUND_CITY"]
            action = {"kind": "FOUND_CITY", "target": "unit1", "intent": {"reason": "Settle here"}}
            # Missing detail must not generate a successful action message.
            assert "error" in await gateway.call(job, "player_command", action, "rejected")
            await gateway.call(job, settlement["tool"], settlement["arguments"], "settlement")
            result = await gateway.call(job, "player_command", action, "accepted")
            assert "error" not in result
            assert result == await gateway.call(job, "player_command", action, "accepted")
            events = [json.loads(line) for line in gateway.history.logs["job"].path.read_text().splitlines()]
            actions = [event for event in events if event["kind"] == "action"]
            assert len(actions) == 1
            assert actions[0]["data"]["description"] == "Founded Rome at (1, 2)."
            assert "description" not in result  # Presentation adds nothing to the model-facing payload.
        finally:
            await io_worker.close()

    asyncio.run(run())
