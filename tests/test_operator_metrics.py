"""Operator metrics separate native API origins, model purposes and context usage."""

import asyncio
from dataclasses import replace

import pytest
from textual.widgets import Static

from civ_agent.match_watch import MatchWatch
from civ_agent.turn_display import api_origin_counts, api_origins_text, context_usage_text
from civ_agent.watch_state import request_counts
from tests.native_watch_fixtures import fixture


def test_api_origins_partition_tool_calls_without_double_counting_or_inventing_legacy_origins():
    calls = {
        "tool:player_turn": {"calls": 9},
        "tool:player_command": {"calls": 3},
        "origin:agent": {"calls": 5},
        "origin:background_poll": {"calls": 2},
        "origin:coordinator": {"calls": 1},
        "origin:unclassified": {"calls": 2},
    }
    assert api_origin_counts(calls) == {"agent": 5, "background_poll": 2, "coordinator": 1, "unknown": 4}
    legacy = api_origin_counts({"tool:player_turn": {"calls": 8}})
    assert legacy is not None and legacy["unknown"] == 8
    assert api_origin_counts({"tool:player_turn": {"calls": 1}, "origin:agent": {"calls": 2}}) is None
    assert "unknown 4" in api_origins_text(calls)


def test_model_counts_keep_reviews_and_communication_consultations_out_of_gameplay():
    jobs = [
        {"id": str(i), "civilization": "Rome", "purpose": purpose, "telemetry": {"modelRequests": i + 1}}
        for i, purpose in enumerate(("gameplay", "economy", "military", "review", "communication", "consultation"))
    ]
    result = request_counts([*jobs, jobs[0]])
    assert (result["gameplay"], result["review"], result["communication"]) == (6, 4, 11)
    assert result["players"]["Rome"] == {"gameplay": 6, "review": 4, "communication": 11}


@pytest.mark.parametrize(
    "value", [None, {}, {"used_tokens": True, "window_tokens": 100}, {"used_tokens": 5, "window_tokens": 0}]
)
def test_context_gauge_requires_numeric_saved_evidence(value):
    assert context_usage_text(value) is None


def test_metrics_ui_renders_saved_numeric_context_and_separate_api_model_counts(tmp_path):
    fixture(tmp_path)

    async def run():
        app = MatchWatch(tmp_path, refresh=60)
        async with app.run_test(size=(150, 45)):
            current = app.snapshot
            run = {
                **current.run,
                "requestCounts": {"gameplay": 7, "review": 3, "communication": 2},
                "inFlight": {
                    "civilization": "Rome",
                    "activeRoles": [
                        {
                            "purpose": "gameplay",
                            "phase": "RUNNING",
                            "context_usage": {
                                "used_tokens": 20000,
                                "window_tokens": 100000,
                                "fraction": 0.2,
                                "resolved": True,
                            },
                        }
                    ],
                },
            }
            result = {
                **current.result,
                "hostCalls": {
                    "tool:player_turn": {"calls": 10},
                    "origin:agent": {"calls": 3},
                    "origin:background_poll": {"calls": 2},
                    "origin:coordinator": {"calls": 1},
                },
                "modelRequests": 12,
            }
            app.snapshot = replace(current, run=run, result=result)
            app.render_records()
            text = str(app.query_one("#metrics", Static).render())
            assert "Game API 10" in text and "Model requests 12" in text
            assert "Gameplay 7" in text and "Review 3" in text and "Communication 2" in text
            assert "agent 3" in text and "background 2" in text and "unknown 4" in text
            assert "Context 20,000/100,000 tokens (20%)" in text

    asyncio.run(run())
