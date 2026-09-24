"""Audit native agreement endings, durable receipts and private planning evidence."""

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

from civ_agent.commands.verify_trade_cycle import clauses
from civ_agent.protocol import JsonObject
from civ_agent.tls import write_private_json

# Independent read-only audit of the persisted revision graph.
REACHABLE = """WITH RECURSIVE reachable(key,version) AS (
    SELECT key,version FROM heads UNION
    SELECT r.key,r.parent FROM records r JOIN reachable a USING(key,version) WHERE r.parent IS NOT NULL
) """

MAP_NAME = "Agreement Lifecycle"
PLAYERS = ("Rome", "Greece", "France")
MINE = (-2, -2)
# Bundled Vanilla rules, not a shortened duration invented by this exercise.
DEAL_DURATIONS = {"Quick": 25, "Standard": 30, "Epic": 45, "Marathon": 90}


def parser(*, add_help=True):
    result = argparse.ArgumentParser(add_help=add_help)
    result.add_argument("--state-dir", type=Path, required=True)
    result.add_argument("--checkpoint", type=Path, help="Default: STATE_DIR/exploration.json")
    result.add_argument("--before-restart", type=Path, help="Archived checkpoint at the first completed round")
    return result


def _native(saved):
    game = json.loads(saved["gameJson"])
    if game["tileMap"]["mapParameters"]["name"] != MAP_NAME:
        raise ValueError("This checkpoint is not the agreement lifecycle scenario")
    return game, {c["civName"]: c for c in game["civilizations"] if c.get("civName") in PLAYERS}


def _terms(items):
    return Counter((i.get("type", "Gold"), i["name"], i.get("amount", 1), i.get("duration", -1)) for i in items)


def _record_terms(record, player, partner):
    return Counter(
        (a, b, *term)
        for side, a, b in (("give", player, partner), ("receive", partner, player))
        for term in _terms(record["initial"].get(side, [])).elements()
    )


def _records(ledger, player):
    return ledger.get("agreements", {}).get("players", {}).get(player, {}).get("records", {})


def _relation(civs, player, partner):
    return civs.get(player, {}).get("diplomacy", {}).get(partner, {})


def _at_war(civs, player, partner):
    # Native serialization omits War, the enum default.
    relation = civs.get(player, {}).get("diplomacy", {}).get(partner)
    return relation is not None and relation.get("diplomaticStatus", "War") == "War"


def _zero_effects(effects):
    relation = effects.get("relationship", {})
    return (
        effects.get("goldPerTurnWithPartner") == 0
        and not any(r["net"] for r in effects.get("resourcesWithPartner", []))
        and relation.get("youMayEnterTheirTerritory") is False
        and relation.get("theyMayEnterYourTerritory") is False
        and relation.get("atWar") is False
    )


def _ending(record, reason):
    events = record.get("history", [])
    return (
        record.get("status") == "ENDED"
        and record.get("current") == {"give": [], "receive": []}
        and record.get("nativeTerms") == {"give": [], "receive": []}
        and record.get("nativeSlot") is None
        and len(events) == 1
        and events[0].get("change") == "ENDED"
        and all(events[0].get(k) == record.get(k) for k in ("agreement", "partner", "kind"))
        and events[0].get("reason") == reason
        and events[0].get("status") == "ENDED"
        and events[0].get("turn") == record.get("endedTurn")
        and events[0].get("version") == record.get("version")
        and events[0].get("after") == record["current"]
        and events[0].get("effects") == record["effects"]
        and _zero_effects(record["effects"])
    )


def _proposal(ledger, duration):
    proposals = ledger["trades"]["proposals"]
    if len(proposals) != 1:
        return None, {"step": "NEGOTIATED_RESOURCE_TRADE", "status": "PENDING" if not proposals else "FAILED"}
    pid, versions = next(iter(proposals.items()))
    final = versions[-1]
    expected_resource = ("Rome", "Greece", "Strategic_Resource", "Iron", 1, duration)
    exact = versions[0]["author"] == "Rome"
    for index, proposal in enumerate(versions):
        items = clauses(proposal)
        cash = [c for c in items.elements() if c[2] == "Gold_Per_Turn"]
        exact &= (
            proposal["proposal"] == pid
            and proposal["version"] == index + 1
            and proposal["turn"] == 0
            and {proposal["author"], proposal["partner"]} == {"Rome", "Greece"}
            and proposal.get("friendship") is None
            and (index == len(versions) - 1 or proposal["status"] == "SUPERSEDED")
            and len(list(items.elements())) == 2
            and items[expected_resource] == 1
            and len(cash) == 1
            and cash[0][:4] == ("Greece", "Rome", "Gold_Per_Turn", "Gold per turn")
            and 2 <= cash[0][4] <= 4
            and cash[0][5] == duration
        )
    receipts = [
        r
        for r in ledger["requests"].values()
        if (r["response"].get("trade") or {}).get("proposal") == pid and "error" not in r["response"]
    ]
    publications = [r for r in receipts if r["request"]["op"] == "trade_offer"]
    acceptances = sorted(
        (
            r
            for r in receipts
            if r["request"]["op"] == "trade_decide"
            and r["request"].get("decision") == "accept"
            and r["request"].get("version") == final["version"]
        ),
        key=lambda r: r["audit"]["timeMillis"],
    )
    frozen = (
        len(publications) == len(versions)
        and bool(publications)
        and Counter(r["response"]["trade"]["version"] for r in publications) == Counter(range(1, len(versions) + 1))
        and all(
            r["audit"]["activeCivilization"] == "Rome" and r["audit"]["handoff"] == publications[0]["audit"]["handoff"]
            for r in publications + acceptances[:1]
        )
    )
    outcome = final.get("outcome")
    confirmed = bool(
        outcome
        and final["status"] == "ENACTED"
        and acceptances
        and outcome["action"] == "ENACT_TRADE"
        and outcome["version"] == final["version"]
        and outcome["clauses"] == final["clauses"]
        and not outcome.get("wars")
        and len(final["consents"]) == 2
        and {c["author"] for c in final["consents"] if c["decision"] == "accept"} == {"Rome", "Greece"}
        and [o for o in ledger["outcomes"] if o.get("proposal") == pid] == [outcome]
        and acceptances[0]["response"].get("outcome") == outcome
    )
    settlement = acceptances[0]["audit"].get("settlement") if acceptances else None
    # This deal has no immediate payment: accepting or replaying it changes no treasury.
    unchanged = bool(
        settlement
        and settlement["before"] == settlement["after"]
        and set(settlement["before"]["gold"]) == set(PLAYERS)
        and not any(w["atWar"] for w in settlement["before"]["wars"])
    )
    passed = exact and frozen and confirmed and unchanged
    return final, {
        "step": "NEGOTIATED_RESOURCE_TRADE",
        "status": "PASSED" if passed else "PENDING" if final["status"] == "PENDING" and exact else "FAILED",
        "proposal": pid,
        "versions": len(versions),
        "exactTerms": exact,
        "gameplayHeld": frozen,
        "engineConfirmed": confirmed,
        "noImmediatePayment": unchanged,
    }


def _archive(saved, game, earlier, matches):
    old_game, civs = _native(earlier)
    old, now = earlier["communication"], saved["communication"]
    mine = next((t for t in old_game["tileMap"]["tileList"] if _position(t) == MINE), {})
    identity = (
        earlier["epoch"] == saved["epoch"]
        and old_game["gameId"] == game["gameId"]
        and old_game.get("turns", 0) == 1
        and old_game.get("currentPlayer") == "Rome"
        and earlier.get("handoff") == 3
        and mine.get("resource") == "Iron"
        and mine.get("resourceAmount") == 1
        and mine.get("improvement") == "Mine"
        and not mine.get("improvementIsPillaged", False)
        and not _at_war(civs, "Rome", "France")
        and not _at_war(civs, "France", "Rome")
        and old["trades"]["proposals"] == now["trades"]["proposals"]
        and old["outcomes"] == now["outcomes"][: len(old["outcomes"])]
        and all(now["requests"].get(k) == v for k, v in old["requests"].items())
        and all(now["streams"].get(p, [])[: len(s)] == s for p, s in old["streams"].items())
    )
    stable = bool(matches)
    for (player, partner), record in matches.items():
        prior = _records(old, player).get(record["agreement"])
        stable &= bool(
            prior
            and all(prior[k] == record[k] for k in ("agreement", "partner", "kind", "observedTurn", "initial"))
            and record["history"][: len(prior["history"])] == prior["history"]
        )
        if not prior:
            continue
        if {player, partner} == {"Rome", "Greece"}:
            trades = _relation(civs, player, partner).get("trades", [])
            initial = prior["initial"]
            remaining = {
                side: Counter(
                    (kind, name, amount, duration - 1)
                    for kind, name, amount, duration in _terms(initial[side]).elements()
                )
                for side in ("give", "receive")
            }
            gpt = sum(i["amount"] for i in initial["receive"] if i["type"] == "Gold_Per_Turn") - sum(
                i["amount"] for i in initial["give"] if i["type"] == "Gold_Per_Turn"
            )
            stable &= (
                prior["status"] == "ACTIVE"
                and len(trades) == 1
                and all(_terms(prior["nativeTerms"][side]) == remaining[side] for side in ("give", "receive"))
                and _terms(trades[0].get("ourOffers", [])) == _terms(prior["nativeTerms"]["give"])
                and _terms(trades[0].get("theirOffers", [])) == _terms(prior["nativeTerms"]["receive"])
                and prior["effects"].get("goldPerTurnWithPartner") == gpt
                and prior["effects"].get("resourcesWithPartner")
                == [{"name": "Iron", "net": -1 if player == "Rome" else 1}]
            )
        else:
            stable &= _ending(prior, "EXPIRED") and prior == record
    for player in PLAYERS:
        old_records = _records(old, player)
        stable &= set(old_records) == set(_records(now, player))
        current_receipts = {r["request"]["requestId"]: r for r in saved["players"].get(player, {}).get("receipts", [])}
        stable &= all(
            current_receipts.get(r["request"]["requestId"]) == r
            for r in earlier["players"].get(player, {}).get("receipts", [])
        )
    return bool(identity and stable)


def _read_memory(path):
    if not path.is_file():
        return None
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        result: JsonObject = {
            name: [dict(r) for r in db.execute("SELECT * FROM " + name)]
            for name in ("records", "calls", "events", "inbox", "outgoing", "meta")
        }
        result["reachable"] = [
            {**dict(r), "body": json.loads(r["body"])}
            for r in db.execute(
                REACHABLE + "SELECT r.rowid AS ordinal,r.* FROM records r JOIN reachable USING(key,version)"
            )
        ]
        result["meta"] = {r["key"]: json.loads(r["value"]) for r in result["meta"]}
        return result


def _memory_preserved(old, now):
    if (
        not old
        or not now
        or any(old["meta"].get(k) != now["meta"].get(k) for k in ("epoch", "storeId", "civilization"))
    ):
        return False
    for table in ("records", "events", "inbox", "outgoing"):
        if not all(row in now[table] for row in old[table]):
            return False
    current = {(r["job"], r["id"]): r for r in now["calls"]}
    return all(
        (new := current.get((row["job"], row["id"]))) is not None
        and all(new[k] == v for k, v in row.items() if k != "result" or v is not None)
        for row in old["calls"]
    )


def _memory(root, player, epoch, agreements, resource_record):
    try:
        data = _read_memory(root / "agent-services" / player / "civilization" / "memory.sqlite3")
        old = _read_memory(root / "before-restart-memory" / (player + ".sqlite3"))
    except (sqlite3.Error, ValueError, KeyError):
        return {"available": False, "preserved": False, "error": "Unreadable private memory evidence"}
    if not data:
        return {"available": False, "preserved": False}
    rows = data["reachable"]
    calls = [
        {**r, "arguments": json.loads(r["arguments"]), "result": json.loads(r["result"])}
        for r in data["calls"]
        if r["result"] is not None
    ]
    read_ids = {
        r["arguments"].get("agreement") for r in calls if r["name"] == "agreement_read" and "agreement" in r["result"]
    }
    events = [json.loads(r["body"]) for r in data["events"] if r["kind"] == "agreement"]
    expected = [e for e in agreements if e["kind"] == "agreement"]
    report = {
        "available": data["meta"].get("civilization") == player and data["meta"].get("epoch") == epoch,
        "preserved": _memory_preserved(old, data),
        "agreementListUsed": any(r["name"] == "agreement_list" and "agreements" in r["result"] for r in calls),
        "agreementReads": sorted(read_ids),
        "allEventsIngestedOnce": len(events) == len(expected) and all(events.count(e) == 1 for e in expected),
        "sourcedPlanReviewed": resource_record is None,
    }
    if resource_record is None:
        report["endedAgreementRead"] = any(r["result"].get("agreement", {}).get("status") == "ENDED" for r in calls)
        return report
    key = "observation:agreement/" + resource_record["agreement"]
    evidence = {(r["key"], r["version"]): r for r in rows}

    def sourced(body, status):
        return any(
            (r := evidence.get((ref["key"], ref["version"])))
            and r["key"] == key
            and not r["body"].get("requiresRefresh")
            and r["body"].get("agreement", {}).get("status") == status
            for ref in body.get("_assumptions", []) + body.get("_evidence", [])
        )

    ended = [r for r in rows if r["key"] == key and r["body"].get("agreement", {}).get("status") == "ENDED"]
    report["endedAgreementRead"] = bool(ended)
    for plan in (r for r in rows if r["category"] == "plan" and sourced(r["body"], "ACTIVE")):
        revisions = sorted((r for r in rows if r["key"] == plan["key"]), key=lambda r: r["ordinal"])
        for flagged in (r for r in rows if r["category"] == "notice" and r["body"].get("record") == plan["key"]):
            if flagged["ordinal"] <= plan["ordinal"]:
                continue
            related = any(
                ref["key"] == key
                or (r := evidence.get((ref["key"], ref["version"])))
                and r["body"].get("kind") == "agreement"
                and r["body"].get("body", {}).get("agreement") == resource_record["agreement"]
                for ref in flagged["body"].get("sources", [])
            )
            if not related:
                continue
            for reviewed in revisions:
                body = reviewed["body"]
                if reviewed["ordinal"] <= flagged["ordinal"] or not reviewed["job"]:
                    continue
                # A read may race ahead of event ingestion and itself trigger the flag.
                fresh = any(plan["ordinal"] < r["ordinal"] < reviewed["ordinal"] for r in ended)
                revision = (
                    body.get("state") == "active"
                    and body.get("_seenNotice", 0) >= flagged["version"]
                    and sourced(body, "ENDED")
                )
                retirement = body.get("state") == "retired" and bool(body.get("assessment", "").strip())
                if fresh and (revision or retirement):
                    report["sourcedPlanReviewed"] = True
                    report["plan"] = plan["key"]
    return report


def _position(tile):
    return tile.get("position", {}).get("x", 0), tile.get("position", {}).get("y", 0)


def audit(root: Path, checkpoint_path: Path | None = None, before_restart: Path | None = None):
    raw = (checkpoint_path or root / "exploration.json").read_bytes()
    saved = json.loads(raw)
    game, civs = _native(saved)
    ledger = saved["communication"]
    duration = DEAL_DURATIONS[game["gameParameters"].get("speed", "Quick")]
    final, proposal_report = _proposal(ledger, duration)
    matches = {}
    reports = [proposal_report]
    prepared = Counter(
        {
            ("Rome", "France", "Gold_Per_Turn", "Gold per turn", 1, 1): 1,
            ("France", "Rome", "Agreement", "Open Borders", 1, 1): 1,
        }
    )
    for a, b, reason in (
        ("Rome", "France", "EXPIRED"),
        ("France", "Rome", "EXPIRED"),
        ("Rome", "Greece", "RESOURCE_UNAVAILABLE"),
        ("Greece", "Rome", "RESOURCE_UNAVAILABLE"),
    ):
        expected = prepared if reason == "EXPIRED" else clauses(final) if final else Counter()
        records = [
            r
            for r in _records(ledger, a).values()
            if r["partner"] == b and r["kind"] == "TRADE" and _record_terms(r, a, b) == expected
        ]
        record = records[0] if len(records) == 1 else None
        if record:
            matches[a, b] = record
        passed = bool(record and _ending(record, reason))
        if passed and record:
            # France ends the round: the global counter advances before host reconciliation.
            passed = record["endedTurn"] == ((1 if a == "France" else 0) if reason == "EXPIRED" else 2)
        reports.append(
            {
                "step": reason,
                "player": a,
                "partner": b,
                "agreement": record["agreement"] if record else None,
                "status": "PASSED" if passed else "PENDING" if not record or record["status"] == "ACTIVE" else "FAILED",
            }
        )
    streams = ledger["streams"]
    privacy = True
    for player in PLAYERS:
        expected = [e for r in _records(ledger, player).values() for e in r.get("history", [])]
        events = [e for e in streams.get(player, []) if e["kind"] == "agreement"]
        privacy &= (
            len(events) == len(expected)
            and len({e["id"] for e in streams.get(player, [])}) == len(streams.get(player, []))
            and all(e["wake"] and expected.count(e["body"]) == 1 for e in events)
            and all(e["partner"] != player and e["partner"] in PLAYERS for e in expected)
        )
    privacy &= all(
        e["kind"] not in {"trade", "consent", "engine"}
        or e["body"].get("proposal") not in ledger["trades"]["proposals"]
        for e in streams.get("France", [])
    )
    french = saved["players"].get("France", {})
    commands = french.get("receipts", [])
    pillages = [r for r in commands if r["request"]["kind"] == "PILLAGE" and r["receipt"]["kind"] == "PILLAGE"]
    declarations = [
        r
        for r in commands
        if r["request"]["kind"] == "DECLARE_WAR" and r["request"].get("target") == "Rome" and r["receipt"]["turn"] == 1
    ]
    tiles = game["tileMap"]["tileList"]
    mine = next((t for t in tiles if _position(t) == MINE), {})
    native_cut = (
        len(pillages) == 1
        and pillages[0]["receipt"]["turn"] == 1
        and len(declarations) == 1
        and mine.get("resource") == "Iron"
        and mine.get("improvement") == "Mine"
        and mine.get("improvementIsPillaged") is True
        # The committed receipt records the unit at the action's location. Later
        # movement or death must not erase evidence of a completed pillage.
        and any(
            unit.get("handle") == pillages[0]["request"].get("target") and _position(unit) == MINE
            for unit in (pillages[0]["receipt"].get("economy") or {}).get("units", [])
        )
        and all(
            r["request"]["requestId"] == r["receipt"]["requestId"]
            and r["request"].get("epoch") == r["receipt"].get("epoch") == saved["epoch"]
            for r in pillages + declarations
        )
        and _at_war(civs, "France", "Rome")
        and _at_war(civs, "Rome", "France")
        and not _at_war(civs, "Rome", "Greece")
        and not _at_war(civs, "Greece", "Rome")
        and not _at_war(civs, "France", "Greece")
        and not _at_war(civs, "Greece", "France")
    )
    earlier = json.loads(before_restart.read_bytes()) if before_restart else None
    memory = {
        p: _memory(
            root,
            p,
            saved["epoch"],
            streams.get(p, []),
            matches.get((p, "Greece" if p == "Rome" else "Rome")) if p != "France" else None,
        )
        for p in PLAYERS
    }
    checks = {
        "threeHumanPlayers": len(civs) == 3
        and {c["civName"] for c in game["civilizations"] if c.get("playerType") == "Human"} == set(PLAYERS),
        "allMilestones": all(r["status"] == "PASSED" for r in reports),
        "noOpenNegotiation": ledger["trades"].get("activeProposal") is None,
        "exactAgreementCount": sum(len(_records(ledger, p)) for p in PLAYERS) == len(matches) == 4,
        "recipientPrivateEvents": privacy,
        "nativePillageAndWars": native_cut,
        "noRemainingNativeTrades": all(
            not _relation(civs, a, b).get("trades") for a in PLAYERS for b in PLAYERS if a != b
        ),
        "preservedAcrossRestartArchive": bool(earlier and _archive(saved, game, earlier, matches)),
        "twoRoundsAfterRestartArchive": bool(earlier and game.get("turns", 0) >= 3 and saved.get("handoff", 0) >= 9),
        "privateMemoryPreserved": all(m.get("available") and m.get("preserved") for m in memory.values()),
        "agreementToolsAndEventsUsed": all(
            m.get("agreementListUsed")
            and m.get("agreementReads")
            and m.get("endedAgreementRead")
            and m.get("allEventsIngestedOnce")
            for m in memory.values()
        ),
        "sourcedPlansReviewed": all(memory[p].get("sourcedPlanReviewed") for p in ("Rome", "Greece")),
    }
    return {
        "verified": all(checks.values()),
        "variant": "agreement-lifecycle",
        "checks": checks,
        "steps": reports,
        "memoryEvidence": memory,
        "gameTurn": game.get("turns", 0),
        "checkpointSha256": hashlib.sha256(raw).hexdigest(),
        "evidenceNote": "Checks native state, authenticated receipts, recipient events and reachable private memory revisions. Baseline archives prove preserved data, not that processes restarted; review operator-process-log.json for stops and starts. Plan checks prove an agreement-sourced plan was flagged, refreshed and revised or explicitly retired; assess the strategic quality and explanations in private transcripts separately. Missing milestones or evidence never count as verified.",
    }


def run(args):
    root = args.state_dir.expanduser().resolve()
    try:
        report = audit(root, args.checkpoint, args.before_restart)
        path = root / "verification" / "agreement-scenario.json"
        write_private_json(path, report)
        for name, passed in report["checks"].items():
            print(f"{'PASS' if passed else 'NOT SATISFIED'} {name}")
        print(report["evidenceNote"])
        print(f"Report: {path}")
        return 0 if report["verified"] else 1
    except (OSError, ValueError, KeyError, TypeError, IndexError, sqlite3.Error):
        print("Cannot verify this match. Check the agreement scenario, checkpoint and restart evidence paths.")
        return 2
