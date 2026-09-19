"""Audit war milestones from native checkpoints and canonical host receipts."""

import argparse
import hashlib
import json
from pathlib import Path

from civ_agent.commands.verify_trade_cycle import clauses
from civ_agent.tls import write_private_json

MAPS = {"Three-Cornered War: paid": "war-paid", "Three-Cornered War: joint": "war-joint"}
PLAYERS = {"Rome", "Greece", "France"}


def parser(*, add_help=True):
    result = argparse.ArgumentParser(add_help=add_help)
    result.add_argument("--state-dir", type=Path, required=True)
    result.add_argument("--checkpoint", type=Path)
    result.add_argument(
        "--before-restart", type=Path, help="Archived completed-round checkpoint before restarting Run A"
    )
    return result


def _native(saved):
    game = json.loads(saved["gameJson"])
    return game, {c["civName"]: c for c in game["civilizations"] if c.get("civName") in PLAYERS}


def _at_war(civs, a, b):
    # Unciv omits enum defaults: DiplomaticStatus.War is the default.
    relation = civs[a].get("diplomacy", {}).get(b)
    return relation is not None and relation.get("diplomaticStatus", "War") == "War"


def _terms(proposal, step, initial=False):
    items = list(clauses(proposal).elements())
    wars = [c for c in items if c[2] == "WarDeclaration"]
    cash = [c for c in items if c[2] == "Gold"]
    declarers = {"Greece"} if step == "PAID_WAR" else {"Rome"} if step == "JOIN_WAR" else {"Rome", "Greece"}
    expected = {(c, "Greece" if c == "Rome" else "Rome", "WarDeclaration", "France", 1, -1) for c in declarers}
    if set(wars) != expected or len(wars) != len(expected) or len(items) != len(wars) + len(cash):
        return False
    if step == "JOINT_WAR":
        return len(cash) <= 1 and all(
            {c[0], c[1]} == {"Rome", "Greece"} and c[3] == "Gold" and c[4] > 0 and c[5] == -1 for c in cash
        )
    payer, payee, low, high = ("Rome", "Greece", 40, 60) if step == "PAID_WAR" else ("Greece", "Rome", 10, 20)
    if initial and step == "PAID_WAR":
        low = high = 30
    return (
        len(cash) == 1
        and cash[0][:4] == (payer, payee, "Gold", "Gold")
        and low <= cash[0][4] <= high
        and cash[0][5] == -1
    )


def _settlement(evidence, proposal, step):
    if not evidence:
        return False
    before, after = evidence["before"], evidence["after"]
    expected_gold = dict(before["gold"])
    for clause in proposal["clauses"]:
        if clause["item"]["type"] == "Gold":
            expected_gold[clause["from"]] -= clause["item"]["amount"]
            expected_gold[clause["to"]] += clause["item"]["amount"]

    def wars(state):
        return {(w["declaringCivilization"], w["targetCivilization"]) for w in state["wars"] if w["atWar"]}

    prior = {("Greece", "France"), ("France", "Greece")} if step == "JOIN_WAR" else set()
    expected = prior | {(c["from"], "France") for c in proposal["clauses"] if c["item"]["type"] == "WarDeclaration"}
    expected |= {(b, a) for a, b in list(expected)}
    return (
        set(before["gold"]) == PLAYERS
        and after["gold"] == expected_gold
        and wars(before) == prior
        and wars(after) == expected
    )


def audit(root: Path, checkpoint_path: Path | None = None, before_restart: Path | None = None):
    raw = (checkpoint_path or root / "exploration.json").read_bytes()
    saved = json.loads(raw)
    game, civs = _native(saved)
    variant = MAPS[game["tileMap"]["mapParameters"]["name"]]
    ledger = saved["communication"]
    proposals = ledger["trades"]["proposals"]
    ids = list(dict.fromkeys(e["body"]["proposal"] for e in ledger["streams"].get("Rome", []) if e["kind"] == "trade"))
    steps = ("PAID_WAR", "JOIN_WAR") if variant == "war-paid" else ("JOINT_WAR",)
    reports: list[dict[str, object]] = []
    last_turn = -1
    for index, step in enumerate(steps):
        if index >= len(ids):
            reports.append({"step": step, "status": "PENDING"})
            continue
        proposal_id = ids[index]
        versions = proposals[proposal_id]
        final = versions[-1]
        last_turn = max(last_turn, final["turn"])
        mutations = [
            r
            for r in ledger["requests"].values()
            if (r["response"].get("trade") or {}).get("proposal") == proposal_id and "error" not in r["response"]
        ]
        publications = [r for r in mutations if r["request"]["op"] == "trade_offer"]
        acceptances = sorted(
            (r for r in mutations if r["request"]["op"] == "trade_decide" and r["request"].get("decision") == "accept"),
            key=lambda r: r["audit"]["timeMillis"],
        )
        active = "Greece" if step == "JOIN_WAR" else "Rome"
        frozen = (
            len(publications) == len(versions)
            and bool(publications)
            and all(
                r["audit"]["activeCivilization"] == active
                and r["audit"]["handoff"] == publications[0]["audit"]["handoff"]
                for r in publications + acceptances[:1]
            )
        )
        exact = versions[0]["author"] == active and all(
            p["version"] == i + 1
            and p["proposal"] == proposal_id
            and {p["author"], p["partner"]} == {"Rome", "Greece"}
            and p["turn"] == final["turn"]
            and p["deadlineMillis"] == versions[0]["deadlineMillis"]
            and (i == len(versions) - 1 or p["status"] == "SUPERSEDED")
            and _terms(p, step, initial=i == 0)
            for i, p in enumerate(versions)
        )
        if step == "PAID_WAR" and final["status"] == "ENACTED":
            exact = exact and len(versions) >= 2 and versions[1]["author"] == "Greece" and _terms(final, step)
        outcome = final.get("outcome")
        expected_wars = [
            {"declaringCivilization": c["from"], "targetCivilization": "France", "atWar": True}
            for c in final["clauses"]
            if c["item"]["type"] == "WarDeclaration"
        ]
        confirmed = bool(
            final["status"] == "ENACTED"
            and outcome
            and acceptances
            and outcome["action"] == "ENACT_TRADE"
            and outcome["clauses"] == final["clauses"]
            and outcome["version"] == final["version"]
            and outcome["wars"] == expected_wars
            and {c["author"] for c in final["consents"] if c["decision"] == "accept"} == {"Rome", "Greece"}
            and [o for o in ledger["outcomes"] if o.get("proposal") == proposal_id] == [outcome]
            and acceptances[0]["request"]["version"] == final["version"]
            and acceptances[0]["response"].get("outcome") == outcome
        )
        settlement = acceptances[0]["audit"].get("settlement") if acceptances else None
        native_settlement = _settlement(settlement, final, step)
        confirmed = confirmed and native_settlement
        reports.append(
            {
                "step": step,
                "status": "PASSED"
                if exact and frozen and confirmed
                else "PENDING"
                if exact and frozen and final["status"] == "PENDING"
                else "FAILED",
                "proposal": proposal_id,
                "version": final["version"],
                "turn": final["turn"],
                "exactTerms": exact,
                "gameplayHeld": frozen,
                "engineConfirmed": confirmed,
                "nativeSettlement": native_settlement,
                "payments": [c for c in final["clauses"] if c["item"]["type"] == "Gold"],
            }
        )
    checks = {
        "allMilestones": all(r["status"] == "PASSED" for r in reports),
        "noExtraProposals": len(ids) == len(steps) and set(ids) == set(proposals),
        "noOpenNegotiation": ledger["trades"].get("activeProposal") is None,
        "nativeWars": len(civs) == 3
        and all(_at_war(civs, a, "France") and _at_war(civs, "France", a) for a in ("Rome", "Greece")),
        "alliesAtPeace": len(civs) == 3 and not _at_war(civs, "Rome", "Greece") and not _at_war(civs, "Greece", "Rome"),
        "targetHasNoPrivateTradeEvents": all(
            e["kind"] != "trade" and e["body"].get("proposal") not in ids for e in ledger["streams"].get("France", [])
        ),
        "twoFurtherRounds": last_turn >= 0 and game.get("turns", 0) >= last_turn + 3,
    }
    if variant == "war-paid":
        checks["preservedAcrossRestartArchive"] = False
        checks["twoRoundsAfterRestartArchive"] = False
        if before_restart:
            earlier = json.loads(before_restart.read_bytes())
            old_game, _ = _native(earlier)
            old = earlier["communication"]
            checks["preservedAcrossRestartArchive"] = (
                earlier["epoch"] == saved["epoch"]
                and old_game["gameId"] == game["gameId"]
                and old_game.get("currentPlayer") == "Rome"
                and old_game.get("turns", 0) > last_turn
                and old["trades"]["proposals"] == proposals
                and old["outcomes"] == ledger["outcomes"]
                and all(ledger["requests"].get(k) == v for k, v in old["requests"].items())
            )
            checks["twoRoundsAfterRestartArchive"] = (
                checks["preservedAcrossRestartArchive"] and game.get("turns", 0) >= old_game.get("turns", 0) + 2
            )
    return {
        "verified": all(checks.values()),
        "variant": variant,
        "checks": checks,
        "steps": reports,
        "gameTurn": game.get("turns", 0),
        "checkpointSha256": hashlib.sha256(raw).hexdigest(),
        "evidenceNote": "Checks native wars and durable consent/outcomes. An archive proves preserved state and continued rounds, not that processes restarted. Host-only settlement snapshots check immediate balance changes and declaration order. Review live transcripts separately for inspected warnings, alert handling, sourced plans and disclosures; this report does not certify those.",
    }


def run(args):
    root = args.state_dir.expanduser().resolve()
    try:
        report = audit(root, args.checkpoint, args.before_restart)
        path = root / "verification" / "war-scenario.json"
        write_private_json(path, report)
        for name, passed in report["checks"].items():
            print(f"{'PASS' if passed else 'NOT SATISFIED'} {name}")
        print(report["evidenceNote"])
        print(f"Report: {path}")
        return 0 if report["verified"] else 1
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        print("Cannot verify this match. Check the war scenario and checkpoint paths.")
        return 2
