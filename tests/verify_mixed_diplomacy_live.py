"""Read-only verification of the guided mixed-player live exercise's private artifacts."""

import argparse
import json
import os
import sqlite3
from pathlib import Path

from civ_agent.protocol import JsonObject


def verify(root: Path) -> JsonObject:
    run = json.loads((root / "mixed-live.json").read_text())
    checkpoint = json.loads((root / "exploration.json").read_text())
    game = json.loads(checkpoint["gameJson"])
    communication = checkpoint["communication"]
    civs = {c["civID"]: c for c in game["civilizations"]}
    checks: dict[str, bool] = {}
    checks["two_rounds_completed_and_paused"] = (
        run.get("completed") is True
        and game["turns"] == 2
        and game["currentPlayer"] == "Rome"
        and checkpoint["handoff"] == 8
        and checkpoint["paused"]
        and checkpoint["communicationPaused"]
    )
    checks["mixed_controllers"] = (
        set(communication["humanPlayers"]) == {"France", "Egypt"}
        and all(civs[n]["playerType"] == "Human" for n in ("Rome", "Greece", "France", "Egypt"))
        and civs["China"].get("playerType", "AI") == "AI"
        and game["gameParameters"]["numberOfCityStates"] == 1
    )
    settings = []
    receipts = {}
    for name in ("Rome", "Greece"):
        service = root / "agent-services" / name
        for category in ("activations", "chat-jobs"):
            settings.extend(json.loads(f.read_text())["effectiveSettings"] for f in (service / category).glob("*.json"))
        activations = [a for a in run["activations"] if a["civilization"] == name]
        checks[f"{name}_completed_both_model_turns"] = (
            {a["turn"] for a in activations} == {0, 1}
            and len(activations) == 2
            and all(a["phase"] == "COMPLETED" for a in activations)
        )
        for turn in (0, 1):
            path = root / "agents" / name / f"turn_{turn}" / "receipts.jsonl"
            receipts[name, turn] = [json.loads(line)["receipt"] for line in path.read_text().splitlines()]
            checks[f"{name}_turn_{turn}_native_handoff"] = any(
                r.get("kind") == "END_TURN" and r.get("isYourTurn") is False for r in receipts[name, turn]
            )
        review_jobs = {a["activationId"] for a in activations if a["turn"] == 1}
        with sqlite3.connect((service / "civilization" / "memory.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
            plans = [
                (json.loads(body), job)
                for body, job in db.execute("SELECT body,job FROM records WHERE category='strategy'")
            ]
        checks[f"{name}_updated_private_diplomatic_strategy"] = any(
            "China" in body.get("subjects", []) and job in review_jobs for body, job in plans
        )
    checks["all_gameplay_and_chat_models_match"] = bool(settings) and all(
        s == {"model": "gpt-5.6-luna", "reasoningEffort": "medium"} for s in settings
    )

    native_actions = [
        r["nativeOutcome"] for rows in receipts.values() for r in rows if isinstance(r.get("nativeOutcome"), dict)
    ]
    checks["one_explicit_denunciation_without_war"] = (
        sum(a["action"] == "DENOUNCE" and a["target"] == "Greece" for a in native_actions) == 1
        and civs["Rome"]["diplomacy"]["Greece"]["flagsCountdown"]["Denunciation"] > 0
        and civs["Rome"]["diplomacy"]["Greece"]["diplomaticStatus"] == "Peace"
    )
    checks["native_demand_and_owned_responses"] = all(
        any(
            a["author"] == author and a["target"] == target and a["action"] == action and a["selection"] == choice
            for a in native_actions
        )
        for author, target, action, choice in (
            ("Rome", "China", "ISSUE_DEMAND", "DoNotSettleNearUs"),
            ("Greece", "Rome", "RESPOND_DIPLOMACY", "Acknowledge"),
            ("Rome", "China", "RESPOND_DIPLOMACY", "Acknowledge"),
        )
    )
    proposals = [versions[-1] for versions in communication["trades"]["proposals"].values()]
    human = next(p for p in proposals if set((p["author"], p["partner"])) == {"Rome", "France"})
    gift = next(p for p in proposals if p["author"] == "Rome" and p["partner"] == "China")
    friendship = next(p for p in proposals if p["author"] == "Greece" and p["partner"] == "China")
    checks["human_trade_exact_shared_consent"] = (
        human["mode"] == "SHARED"
        and human["status"] == "ENACTED"
        and {c["author"] for c in human["consents"] if c["decision"] == "accept"} == {"Rome", "France"}
        and any(t["proposal"] == human["proposal"] for t in run["humanTrades"])
    )
    for name, proposal in (("Rome", gift), ("Greece", friendship)):
        checks[f"{name}_native_request_returned_pending_without_pause"] = any(
            (t := r.get("trade", {})).get("proposal") == proposal["proposal"]
            and t.get("status") == "PENDING"
            and t.get("mode") == "NATIVE_AI"
            and t.get("awaitingNativeTurn") is True
            and t.get("pausesGameplay") is False
            for r in receipts[name, 0]
        )
    checks["native_gift_actually_enacted"] = gift["status"] == "ENACTED" and gift["outcome"]["author"] == "China"
    friendship_active = (
        civs["Greece"]["diplomacy"]["China"].get("flagsCountdown", {}).get("DeclarationOfFriendship", 0) > 0
    )
    checks["native_friendship_result_matches_engine"] = friendship["status"] in {"ENACTED", "DECLINED"} and (
        friendship_active == (friendship["status"] == "ENACTED")
    )
    checks["private_ai_proposals_absent_from_human_inboxes"] = all(
        p["proposal"] not in json.dumps(communication["streams"].get(human, []))
        for p in (gift, friendship)
        for human in ("France", "Egypt")
    )
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "verifiedModelInvocations": len(settings),
        "nativeGift": {k: gift[k] for k in ("proposal", "status", "clauses")},
        "nativeFriendship": {k: friendship[k] for k in ("proposal", "status")},
        "limits": [
            "Scripted human controllers",
            "No live process restart or fault injection",
            "No direct city-state actions",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    result = verify(args.state_dir.resolve())
    encoded = json.dumps(result, indent=2) + "\n"
    (args.state_dir / "mixed-live-verification.json").write_text(encoded)
    print(encoded, end="")
    raise SystemExit(0 if result["passed"] else 1)
