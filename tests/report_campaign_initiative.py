"""Read-only public decision/action evidence for a paired campaign evaluation."""

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path

from tests.report_strategy_positions import native_position, position_changes


def campaign_evidence(root):
    """Keep recommendations, accepted goals and executed actions distinct."""
    timeline, reviews, states, consultations = [], [], {}, []
    for path in sorted(root.glob("agent-services/*/civilization/memory.sqlite3")):
        civilization = path.parent.parent.name
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            query = """SELECT c.rowid AS ordering,c.*,j.game_turn,j.purpose
                       FROM calls c LEFT JOIN jobs j ON j.id=c.job ORDER BY c.rowid"""
            for row in db.execute(query):
                if row["name"] not in {
                    "player_command",
                    "war_goals",
                    "military_operation",
                    "role_message",
                    "trade_offer",
                    "trade_decide",
                    "chat_send",
                    "economy_request",
                }:
                    continue
                arguments = json.loads(row["arguments"])
                if row["name"] == "war_goals" and arguments.get("action", "read") == "read":
                    continue
                result = json.loads(row["result"]) if row["result"] else {}
                timeline.append(
                    {
                        "civilization": civilization,
                        "turn": row["game_turn"],
                        "role": row["purpose"],
                        "job": row["job"],
                        "callId": row["id"],
                        "order": row["ordering"],
                        "tool": row["name"],
                        "arguments": arguments,
                        "result": {
                            k: v
                            for k, v in result.items()
                            if k not in {"stateUpdate", "coordinationUpdate", "memoryUpdates"}
                        },
                    }
                )
            state = db.execute("SELECT value FROM meta WHERE key='war:state'").fetchone()
            states[civilization] = json.loads(state[0]) if state else {}
            for row in db.execute("SELECT job,turn,advice FROM reviews WHERE status='COMPLETED' ORDER BY turn"):
                reviews.append(
                    {
                        "civilization": civilization,
                        "job": row["job"],
                        "turn": row["turn"],
                        "advice": json.loads(row["advice"]),
                    }
                )
            for row in db.execute("SELECT key,value FROM meta WHERE key LIKE 'peace:%'"):
                consultations.append(
                    {"civilization": civilization, "key": row["key"], "record": json.loads(row["value"])}
                )
    timeline.sort(key=lambda row: (row["turn"] if row["turn"] is not None else -1, row["civilization"], row["order"]))
    commands = [r for r in timeline if r["tool"] == "player_command"]
    # A receipt's actionSummary establishes execution; a call without one is not success.
    executed = [r for r in commands if r["result"].get("actionSummary")]
    captures = [
        r
        for r in executed
        if r["arguments"].get("kind") == "ATTACK" and (r["result"].get("combat") or {}).get("defenderCaptured")
    ]
    cities = [
        r
        for r in executed
        if r["arguments"].get("kind") == "RESOLVE_CAPTURED_CITY" and "city of" in r["result"].get("actionSummary", "")
    ]
    initial = native_position(root / "initial-game.json", states) if (root / "initial-game.json").exists() else []
    final = native_position(root / "exploration.json", states) if (root / "exploration.json").exists() else []
    return {
        "stateDirectory": str(root.resolve()),
        "scope": "Public tool arguments/results, authored strategic advice, war coordination and native outcomes; no private model reasoning",
        "commandAttempts": dict(Counter(r["arguments"].get("kind") for r in commands)),
        "commandReceipts": dict(Counter(r["arguments"].get("kind") for r in executed)),
        "declarations": [r for r in executed if r["arguments"].get("kind") == "DECLARE_WAR"],
        "captureCombatReceipts": captures,
        "cityDispositionReceipts": cities,
        "warStates": states,
        "peaceConsultations": consultations,
        "timeline": timeline,
        "strategicReviews": reviews,
        "initialNativePositions": initial,
        "finalNativePositions": final,
        "outcomes": position_changes(initial, final) if initial and final else [],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state_dir", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = campaign_evidence(args.state_dir)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {"commandReceipts": result["commandReceipts"], "cityDispositions": len(result["cityDispositionReceipts"])}
        )
    )


if __name__ == "__main__":
    main()
