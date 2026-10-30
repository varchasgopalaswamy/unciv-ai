"""Screening counters distinguish confirmed actions from rejected annotations."""

import json
import sqlite3

from tests.report_gameplay_followups import protocol_audit


def test_protocol_audit_counts_only_confirmed_orders_and_retains_exact_holds(tmp_path):
    path = tmp_path / "agent-services/Rome/civilization/memory.sqlite3"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE jobs (id TEXT, purpose TEXT, game_turn INTEGER)")
        db.execute("CREATE TABLE calls (job TEXT, name TEXT, arguments TEXT, result TEXT)")
        db.executemany("INSERT INTO jobs VALUES (?,?,?)", [("e", "economy", 4), ("m", "military", 4)])
        rows = [
            ("e", "player_command", {"kind": "START_WORKER_JOB"}, {"actionSummary": "Worker started Farm"}),
            ("e", "player_command", {"kind": "START_WORKER_JOB"}, {"error": "STALE_REVISION"}),
            ("e", "player_command", {"kind": "STOP_WORKER_JOB"}, {}),
            (
                "e",
                "player_command",
                {"kind": "SET_CITY_FOCUS", "intent": {"cityPlan": "mem1"}},
                {"actionSummary": "Selected food"},
            ),
            (
                "e",
                "player_command",
                {"kind": "SET_CITY_FOCUS", "intent": {"cityPlan": "mem1"}},
                {"error": "INVALID_TOOL_ARGUMENTS"},
            ),
            ("m", "military_review", {}, {"militaryCompletion": {"items": []}}),
            ("m", "finish_role", {}, {"error": "MILITARY_COMPLETION_REVIEW_REQUIRED"}),
            (
                "m",
                "finish_role",
                {
                    "militaryReview": {
                        "decisions": [{"targets": ["unit1"], "reason": "Accept the risk to protect our city."}]
                    }
                },
                {"roleFinished": True},
            ),
        ]
        db.executemany(
            "INSERT INTO calls VALUES (?,?,?,?)",
            [(job, name, json.dumps(args), json.dumps(result)) for job, name, args, result in rows],
        )
    audit = protocol_audit(tmp_path)
    assert audit["confirmedWorkerOrders"] == [{"role": "economy", "kind": "START_WORKER_JOB", "count": 1}]
    assert audit["confirmedPlanOnlyCommands"] == audit["rejectedPlanOnlyArguments"] == 1
    assert audit["completionReads"] == audit["completionRecoveries"] == 1
    assert audit["acceptedHoldDecisions"] == [
        {"turn": 4, "targets": ["unit1"], "reason": "Accept the risk to protect our city."}
    ]
