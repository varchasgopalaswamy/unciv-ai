"""Audit the verbal-pact exercise using native saves, host receipts and private evidence."""

import argparse
import hashlib
import json
import re
import sqlite3
from pathlib import Path

from civ_agent.tls import write_private_json


def parser(*, add_help=True):
    result = argparse.ArgumentParser(add_help=add_help)
    result.add_argument("--state-dir", type=Path, required=True)
    result.add_argument("--checkpoint", type=Path, help="Default: STATE_DIR/exploration.json")
    return result


def relationship_evidence(path):
    """Read only; constructing CivilizationState would change live job statuses."""
    if not path.exists():
        return None, None, []
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.execute("BEGIN")
        epoch = db.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()
        row = db.execute(
            "SELECT r.category,r.body,r.version FROM records r JOIN heads h USING(key,version) "
            "WHERE r.key='relationships/Greece'"
        ).fetchone()
        if row is None:
            return json.loads(epoch[0]) if epoch else None, None, []
        note = {"category": row[0], "body": json.loads(row[1]), "version": row[2]}
        pending = list(note["body"].get("_evidence", []))
        evidence, visited = [], set()
        while pending:
            reference = pending.pop()
            key = reference["key"], reference["version"]
            if key in visited:
                continue
            visited.add(key)
            source = db.execute("SELECT category,body FROM records WHERE key=? AND version=?", key).fetchone()
            if source is None:
                raise ValueError("A referenced memory revision is missing")
            body = json.loads(source[1])
            evidence.append({"key": key[0], "version": key[1], "category": source[0], "body": body})
            pending.extend(body.get("_evidence", []))
        return json.loads(epoch[0]) if epoch else None, note, evidence


def audit(root: Path, checkpoint_path: Path | None = None):
    checkpoint_path = checkpoint_path or root / "exploration.json"
    raw = checkpoint_path.read_bytes()
    saved = json.loads(raw)
    game = json.loads(saved["gameJson"])
    if game["tileMap"]["mapParameters"]["name"] != "Diplomacy memory exercise":
        raise ValueError("This checkpoint is not the verbal-betrayal scenario")
    civilizations = {c["civName"]: c for c in game["civilizations"] if c.get("civName") in {"Rome", "Greece"}}
    diplomacy = [
        civilizations[name].get("diplomacy", {}).get(other, {})
        for name, other in (("Rome", "Greece"), ("Greece", "Rome"))
    ]
    ledger = saved.get("communication", {})
    stream = ledger.get("streams", {}).get("Rome", [])
    war_events = [
        e
        for e in stream
        if e["kind"] == "engine" and e["body"].get("action") == "DECLARE_WAR" and e["body"].get("author") == "Greece"
    ]
    war = war_events[0] if war_events else None
    war_turn = war["body"]["turn"] if war else None
    receipts = [
        (civilization, row) for civilization, state in saved["players"].items() for row in state.get("receipts", [])
    ]
    confirmed_war = [
        row
        for name, row in receipts
        if name == "Greece"
        and row["request"]["kind"] == "DECLARE_WAR"
        and row["request"].get("target") == "Rome"
        and row["receipt"]["kind"] == "DECLARE_WAR"
        and row["receipt"]["turn"] == war_turn
    ]
    pact, accepts, reconsider = None, [], None
    if war and war_turn is not None:
        for event in stream:
            body = event["body"]
            if event["kind"] != "offer" or body.get("kind") != "peace" or event["id"] >= war["id"]:
                continue
            expires = body.get("expiresTurn")
            if set(body["participants"]) != {"Rome", "Greece"} or type(expires) is not int or expires <= war_turn:
                continue
            decisions = [
                e
                for e in stream
                if e["kind"] == "consent"
                and e["body"]["proposal"] == body["proposal"]
                and e["body"]["version"] == body["version"]
                and event["id"] < e["id"] < war["id"]
            ]
            accepted = [e for e in decisions if e["body"]["decision"] == "accept"]
            overridden = [
                e for e in decisions if e["body"]["decision"] == "reconsider" and e["body"]["author"] == "Greece"
            ]
            if {e["body"]["author"] for e in accepted} == {"Rome", "Greece"} and overridden:
                pact, accepts, reconsider = event, accepted, overridden[-1]
                break
    epoch, note, evidence = relationship_evidence(root / "agent-services" / "Rome" / "civilization" / "memory.sqlite3")
    referenced_events = {
        event["id"]
        for event in stream
        if any(record["body"] == event and record["category"] in {"observation", "statement"} for record in evidence)
    }
    for record in evidence:
        if record["category"] != "observation":
            continue
        observed_ledger = (
            record["body"].get("communication", {})
            if record["key"] == "observation:game"
            else record["body"]
            if record["key"] == "observation:communication"
            else {}
        )
        for event in stream:
            collection = {"offer": "offers", "consent": "consents", "engine": "outcomes"}.get(event["kind"])
            if collection and any(
                all(item.get(key) == value for key, value in event["body"].items())
                for item in observed_ledger.get(collection, [])
            ):
                referenced_events.add(event["id"])
    war_observed = bool(war and war["id"] in referenced_events) or any(
        record["key"] == "observation:game"
        and record["category"] == "observation"
        and war_turn is not None
        and record["body"].get("turn", -1) >= war_turn
        and any(
            c["civilizationId"] == "Greece" and c["atWar"]
            for c in record["body"].get("diplomacy", {}).get("knownCivilizations", [])
        )
        for record in evidence
    )
    body = note["body"] if note else {}
    text = body.get("text", "")
    projection = root / "agents" / "Rome" / "relationships" / "Greece.md"
    starting = root / "round-checkpoints" / (saved["epoch"] + "-round-0.json")
    initial = json.loads(json.loads(starting.read_text())["host"]["gameJson"]) if starting.exists() else {}
    initial_players = {
        c["civName"]: c for c in initial.get("civilizations", []) if c.get("civName") in {"Rome", "Greece"}
    }
    peace_commands = {"PROPOSE_PEACE", "ACCEPT_PEACE", "COUNTER_PEACE", "REJECT_PEACE", "RETRACT_PEACE"}
    checks = {
        "metAtPeaceBeforeNegotiation": len(initial_players) == 2
        and all(
            initial_players[name].get("diplomacy", {}).get(other, {}).get("diplomaticStatus") == "Peace"
            for name, other in (("Rome", "Greece"), ("Greece", "Rome"))
        ),
        "mutualSocialPactBeforeWar": pact is not None,
        "greeceDeliberatelyReconsidered": bool(reconsider and max(e["id"] for e in accepts) < reconsider["id"]),
        "romeDidNotReleaseGreece": bool(
            pact
            and war
            and not any(
                e["kind"] == "consent"
                and e["body"].get("proposal") == pact["body"]["proposal"]
                and e["body"].get("author") == "Rome"
                and e["body"].get("decision") in {"withdraw", "reconsider"}
                and e["id"] < war["id"]
                for e in stream
            )
        ),
        "greeceDeclaredWarBeforeExpiry": len(confirmed_war) == 1 and pact is not None,
        # Unciv's DiplomacyManager defaults to War; native JSON omits default
        # fields. Require a real relationship entry before applying that default.
        "nativeSaveConfirmsWarBothWays": all(d and d.get("diplomaticStatus", "War") == "War" for d in diplomacy),
        "noEnginePeaceNegotiation": not any(row["request"]["kind"] in peace_commands for _, row in receipts)
        and not any(d.get("trades") for d in diplomacy),
        "continuedScheduledTurns": saved.get("handoff", 0) >= 5,
        "currentRelationshipBelief": epoch == saved["epoch"]
        and bool(note and note["category"] == "belief")
        and body.get("uncertainty") in {"low", "medium", "high"},
        "relationshipCitesTermsAndGreekConsent": bool(
            pact
            and pact["id"] in referenced_events
            and any(e["body"]["author"] == "Greece" and e["id"] in referenced_events for e in accepts)
        ),
        "relationshipCitesObservedWar": war_observed,
        # These are wording cues for the operator, not an automated judgment of trustworthiness.
        "assessmentRecognizesBreach": bool(re.search(r"betray|breach|broke|broken|violat|reneg|went back", text, re.I)),
        "readableRelationshipMatchesMemory": projection.exists() and projection.read_text().strip() == text.strip(),
    }
    return {
        "verified": all(checks.values()),
        "checks": checks,
        "gameTurn": game.get("turns", 0),
        "handoffs": saved.get("handoff", 0),
        "checkpointSha256": hashlib.sha256(raw).hexdigest(),
        "pact": pact["body"] if pact else None,
        "greekReconsideration": reconsider["body"] if reconsider else None,
        "engineDeclaration": war["body"] if war else None,
        "relationship": {key: value for key, value in body.items() if key != "_evidence"},
        "referencedHostEvents": sorted(referenced_events),
        "explicitTrustLanguage": bool(re.search(r"trust|reliab|credib|honou?r", text, re.I)),
        "assessmentNote": "Inspect the relationship text alongside the cited evidence; wording checks alone do not establish its meaning.",
    }


def run(args):
    root = args.state_dir.expanduser().resolve()
    try:
        report = audit(root, args.checkpoint.expanduser().resolve() if args.checkpoint else None)
        path = root / "verification" / "betrayal.json"
        write_private_json(path, report)
        for name, passed in report["checks"].items():
            print(f"{'PASS' if passed else 'PENDING'} {name}")
        print(f"Report: {path}")
        return 0 if report["verified"] else 1
    except (OSError, ValueError, KeyError, sqlite3.Error):
        print("Cannot verify this match. Check the scenario, native checkpoint and private memory database.")
        return 2
