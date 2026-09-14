"""Audit the complete trade exercise from one atomic checkpoint, without reading model transcripts."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from civ_agent.tls import write_private_json

MAP_NAME = "Complete trade cycle exercise"
PLAYERS = ("Rome", "Greece")
# Independent expectations for the Quick-speed, bundled Gods & Kings fixture.
STEPS = (
    ("PAID_PEACE", "ENACTED", ("Rome", "Greece")),
    ("DECLINE", "DECLINED", ("Rome",)),
    ("WITHDRAW", "WITHDRAWN", ("Rome",)),
    ("DEADLINE", "EXPIRED", ("Rome",)),
    ("GIFT", "ENACTED", ("Rome",)),
    ("RESOURCE_BARGAIN", "ENACTED", ("Rome", "Greece", "Rome", "Greece")),
    ("FRIENDSHIP", "ENACTED", ("Rome",)),
    ("EMBASSY_IN_ROME", "ENACTED", ("Rome",)),
    ("EMBASSY_IN_ATHENS", "ENACTED", ("Rome",)),
    ("GREEK_ACCESS", "ENACTED", ("Rome",)),
    ("FUNDED_TREATIES_AND_CITIES", "ENACTED", ("Rome",)),
)


def parser(*, add_help=True):
    result = argparse.ArgumentParser(add_help=add_help)
    result.add_argument("--state-dir", type=Path, required=True)
    result.add_argument("--checkpoint", type=Path, help="Default: STATE_DIR/exploration.json")
    return result


def terms(step, version, cost):
    """Expected clauses from Rome's perspective, independently of the proposing party."""

    def term(kind, name, amount=1):
        duration = -1 if kind in {"Gold", "City", "Embassy"} else 10 if name == "Peace Treaty" else 25
        return kind, name, amount, duration

    def gold(amount):
        return term("Gold", "Gold", amount)

    peace = term("Treaty", "Peace Treaty")
    embassy = term("Embassy", "Accept Embassy")
    borders = term("Agreement", "Open Borders")
    give, receive = [], []
    match step:
        case "PAID_PEACE":
            give, receive = [peace, gold(40 if version == 1 else 30)], [peace]
        case "DECLINE" | "WITHDRAW" | "DEADLINE" | "GIFT":
            give = [gold({"DECLINE": 7, "WITHDRAW": 11, "DEADLINE": 13, "GIFT": 17}[step])]
        case "RESOURCE_BARGAIN":
            give = [term("Luxury_Resource", "Silk"), term("Strategic_Resource", "Iron", 2)]
            receive = [
                term("Luxury_Resource", "Gold Ore"),
                term("Gold_Per_Turn", "Gold per turn", (4, 3, 2, 2)[version - 1]),
            ]
            if version > 1:
                receive.append(gold((0, 10, 20, 25)[version - 1]))
        case "EMBASSY_IN_ROME":
            give = [embassy]
        case "EMBASSY_IN_ATHENS":
            receive = [embassy]
        case "GREEK_ACCESS":
            give = [borders]
        case "FUNDED_TREATIES_AND_CITIES":
            treaties = [term("Treaty", "Research Agreement", cost), term("Treaty", "Defensive Pact")]
            give = [*treaties, gold(cost), term("City", "Antium")]
            receive = [*treaties, borders, term("City", "Sparta")]
        case "FRIENDSHIP":
            pass
        case _:
            raise ValueError("Unknown trade-cycle step")
    return Counter([("Rome", "Greece", *t) for t in give] + [("Greece", "Rome", *t) for t in receive])


def clauses(proposal):
    return Counter(
        (
            c["from"],
            c["to"],
            c["item"]["type"],
            c["city"]["name"] if c["item"]["type"] == "City" else c["item"]["name"],
            c["item"]["amount"],
            c["item"]["duration"],
        )
        for c in proposal["clauses"]
    )


def research_cost(proposal):
    return next((c["item"]["amount"] for c in proposal["clauses"] if c["item"]["name"] == "Research Agreement"), 1)


def audit(root: Path, checkpoint_path: Path | None = None):
    raw = (checkpoint_path or root / "exploration.json").read_bytes()
    saved = json.loads(raw)
    game = json.loads(saved["gameJson"])
    if game["tileMap"]["mapParameters"]["name"] != MAP_NAME:
        raise ValueError("This checkpoint is not the trade-cycle scenario")
    ledger = saved["communication"]
    proposals = ledger["trades"]["proposals"]
    stream = ledger["streams"].get("Rome", [])
    ids = list(dict.fromkeys(e["body"]["proposal"] for e in stream if e["kind"] == "trade"))
    receipts = list(ledger["requests"].values())
    reports: list[dict[str, object]] = []
    previous_turn = -1
    for index, (name, terminal, authors) in enumerate(STEPS):
        if index >= len(ids):
            reports.append({"step": name, "status": "PENDING", "reason": "No proposal yet"})
            continue
        proposal_id = ids[index]
        versions = proposals[proposal_id]
        final = versions[-1]
        cost = research_cost(final)
        exact = 0 < len(versions) <= len(authors) and cost > 0
        for i, p in enumerate(versions):
            exact = exact and (
                i < len(authors)
                and p["author"] == authors[i]
                and {p["author"], p["partner"]} == set(PLAYERS)
                and p["proposal"] == proposal_id
                and p["version"] == i + 1
                and p["turn"] == final["turn"]
                and p["counterofferLimit"] == 3
                and p["deadlineMillis"] == versions[0]["deadlineMillis"]
                and (i == len(versions) - 1 or p["status"] == "SUPERSEDED")
                and clauses(p) == terms(name, i + 1, cost)
                and p.get("friendship")
                == ({"name": "Declaration of Friendship", "duration": 30} if name == "FRIENDSHIP" else None)
                and p["consents"][0] == {"author": p["author"], "decision": "accept", "turn": p["turn"]}
            )
        mutations = [r for r in receipts if (r["response"].get("trade") or {}).get("proposal") == proposal_id]
        opens = [
            r
            for r in mutations
            if r["request"]["op"] in {"trade_offer", "friendship_offer"}
            and "proposal" not in r["request"]
            and "error" not in r["response"]
        ]
        # Find actual acceptance/terminal receipts from the negotiation's activation.
        decisions = [
            r
            for r in mutations
            if r["request"]["op"] == "trade_decide"
            and r["request"].get("version") == final["version"]
            and "error" not in r["response"]
        ]
        publications = [
            r
            for r in mutations
            if r["request"]["op"] in {"trade_offer", "friendship_offer"} and "error" not in r["response"]
        ]
        # A later acceptance retry may return the original outcome. Only publications
        # and the first terminal decision constitute this negotiation.
        transitions = publications + sorted(decisions, key=lambda r: r["audit"]["timeMillis"])[:1]
        frozen = (
            len(opens) == 1
            and len(publications) == len(versions)
            and all(
                r["audit"]["activeCivilization"] == "Rome" and r["audit"]["handoff"] == opens[0]["audit"]["handoff"]
                for r in transitions
            )
        )
        outcomes = [o for o in ledger["outcomes"] if o.get("proposal") == proposal_id]
        confirmed = final["status"] == terminal and len(versions) == len(authors)
        if terminal == "ENACTED":
            outcome = final.get("outcome")
            confirmed = (
                confirmed
                and bool(outcome)
                and outcomes == [outcome]
                and (
                    outcome["clauses"] == final["clauses"]
                    and outcome.get("friendship") == final.get("friendship")
                    and outcome["version"] == final["version"]
                    and outcome["action"] == ("DECLARE_FRIENDSHIP" if name == "FRIENDSHIP" else "ENACT_TRADE")
                    and {c["author"] for c in final["consents"] if c["decision"] == "accept"} == set(PLAYERS)
                    and any(r["response"].get("outcome") == outcome for r in decisions)
                )
            )
        else:
            confirmed = confirmed and not outcomes and final.get("outcome") is None
            if terminal == "EXPIRED":
                confirmed = confirmed and len(final["consents"]) == 1
            else:
                decision = "decline" if terminal == "DECLINED" else "withdraw"
                confirmed = confirmed and any(r["request"].get("decision") == decision for r in decisions)
        ordered = final["turn"] > previous_turn
        previous_turn = final["turn"]
        status = (
            "PASSED"
            if exact and frozen and ordered and confirmed
            else "PENDING"
            if exact and frozen and ordered and final["status"] == "PENDING"
            else "FAILED"
        )
        reports.append(
            {
                "step": name,
                "status": status,
                "proposal": proposal_id,
                "versions": len(versions),
                "observed": final["status"],
                "exactTerms": exact,
                "gameplayHeld": frozen,
                "oneStepPerRound": ordered,
                "engineConfirmed": confirmed,
            }
        )

    civilizations = {c["civName"]: c for c in game["civilizations"] if c.get("civName") in PLAYERS}
    relations = {c: civilizations[c].get("diplomacy", {}).get(other, {}) for c, other in (PLAYERS, PLAYERS[::-1])}
    cities = {city["id"]: (owner, city) for owner, civ in civilizations.items() for city in civ.get("cities", [])}
    handles = ledger["trades"]["cityHandles"]
    owners = {city["name"]: owner for owner, city in cities.values()}

    def native_contract(player, proposal):
        """Compare the complete native deal, resolving short city handles to engine IDs."""
        matches = 0
        for trade in relations[player].get("trades", []):
            actual = Counter()
            remaining = []
            for side, author in (("ourOffers", player), ("theirOffers", "Greece" if player == "Rome" else "Rome")):
                for item in trade.get(side, []):
                    kind, item_name = item.get("type", "Gold"), item["name"]
                    if kind == "City":
                        item_name = cities.get(item_name, (None, {"name": "missing"}))[1]["name"]
                    duration = item.get("duration", -1)
                    original = -1 if kind in {"Gold", "City", "Embassy"} else 10 if item_name == "Peace Treaty" else 25
                    expected = original - (game.get("turns", 0) - proposal["turn"])
                    remaining.append(
                        duration == -1 if original == -1 else 0 < duration <= original and abs(duration - expected) <= 1
                    )
                    actual[
                        (
                            author,
                            "Greece" if author == "Rome" else "Rome",
                            kind,
                            item_name,
                            item.get("amount", 1),
                            original,
                        )
                    ] += 1
            if actual == clauses(proposal) and all(remaining):
                matches += 1
        return matches == 1

    final = proposals[ids[10]][-1] if len(ids) > 10 else None
    resources = proposals[ids[5]][-1] if len(ids) > 5 else None
    access = proposals[ids[9]][-1] if len(ids) > 9 else None
    city_clauses = [c for c in final["clauses"] if c["item"]["type"] == "City"] if final else []
    owners_confirmed = len(city_clauses) == 2 and all(
        any(
            short == c["item"]["name"] and engine_id in cities and cities[engine_id][0] == c["to"]
            for engine_id, short in handles.items()
        )
        for c in city_clauses
    )
    initial_path = root / "round-checkpoints" / f"{saved['epoch']}-round-0.json"
    initial = json.loads(json.loads(initial_path.read_text())["host"]["gameJson"]) if initial_path.exists() else None
    starting = {c["civName"]: c for c in initial["civilizations"] if c.get("civName") in PLAYERS} if initial else {}
    original_cities = {city["id"]: city for civ in starting.values() for city in civ.get("cities", [])}
    needs_decision = {
        c["to"]
        for c in city_clauses
        for engine_id, short in handles.items()
        if short == c["item"]["name"]
        and original_cities.get(engine_id, {}).get("foundingCiv")
        and original_cities[engine_id]["foundingCiv"] not in {c["from"], c["to"]}
    }
    funding_path = root / "round-checkpoints" / f"{saved['epoch']}-round-{final['turn'] if final else -1}.json"
    funding_game = json.loads(json.loads(funding_path.read_text())["host"]["gameJson"]) if funding_path.exists() else {}
    funding_gold = {
        c["civName"]: c.get("gold", 0) for c in funding_game.get("civilizations", []) if c.get("civName") in PLAYERS
    }
    checks = {
        "preparedWar": len(starting) == 2
        and all(
            other in starting[c].get("diplomacy", {})
            and starting[c]["diplomacy"][other].get("diplomaticStatus", "War") == "War"
            for c, other in (PLAYERS, PLAYERS[::-1])
        ),
        "allElevenSteps": all(r["status"] == "PASSED" for r in reports),
        "noExtraProposals": set(ids) == set(proposals) and len(ids) <= len(STEPS),
        "noOpenNegotiation": ledger["trades"].get("activeProposal") is None,
        "nativePeaceAndDefensivePact": all(d.get("diplomaticStatus") == "DefensivePact" for d in relations.values()),
        "nativeFriendshipResearchDefense": all(
            d.get("flagsCountdown", {}).get(flag, 0) > 0
            for d in relations.values()
            for flag in ("DeclarationOfFriendship", "ResearchAgreement", "DefensivePact")
        ),
        "nativeMutualEmbassies": all(
            d.get("diplomaticModifiers", {}).get("SharedEmbassies", 0) > 0 for d in relations.values()
        ),
        "nativeResourceAndGptDeal": bool(resources and all(native_contract(p, resources) for p in PLAYERS)),
        "exactlyThreeActiveNativeDeals": all(len(d.get("trades", [])) == 3 for d in relations.values()),
        "nativeDirectionalBorders": bool(access and all(native_contract(p, access) for p in PLAYERS)),
        "nativeFundedTreatiesAndReciprocalBorders": bool(final and all(native_contract(p, final) for p in PLAYERS)),
        "researchFundingWasNecessary": bool(
            final
            and set(funding_gold) == set(PLAYERS)
            and funding_gold["Greece"] < research_cost(final) <= funding_gold["Rome"] // 2
        ),
        "nativeCityExchange": owners_confirmed
        and owners == {"Rome": "Rome", "Sparta": "Rome", "Athens": "Greece", "Antium": "Greece"},
        "nativeCitiesKeptAndDecisionsResolved": bool(final)
        and all(
            (
                p not in needs_decision
                or any(
                    r["request"]["kind"] == "RESOLVE_CAPTURED_CITY"
                    and r["request"].get("selection") == "KeepCity"
                    and r["receipt"]["turn"] >= final["turn"]
                    for r in saved["players"].get(p, {}).get("receipts", [])
                )
            )
            and not any(a["type"] == "CityTraded" for a in civilizations[p].get("popupAlerts", []))
            and all(
                not city.get("hasJustBeenConquered", False) and not city.get("isBeingRazed", False)
                for city in civilizations[p].get("cities", [])
            )
            for p in PLAYERS
        ),
        "twoFurtherRounds": bool(
            final and game.get("turns", 0) >= final["turn"] + 3 and saved.get("handoff", 0) >= 2 * (final["turn"] + 3)
        ),
    }
    return {
        "verified": all(checks.values()),
        "checks": checks,
        "steps": reports,
        "gameTurn": game.get("turns", 0),
        "checkpointSha256": hashlib.sha256(raw).hexdigest(),
        "treasuries": {p: c.get("gold", 0) for p, c in civilizations.items()},
        "cityOwners": owners,
        "evidenceNote": "Uses native state and host-authored receipts, not agent claims. Instant-only gifts and past peace are confirmed by durable engine outcomes; the scripted probe also checks their immediate balance changes. Native active deals retain remaining, not original, durations.",
    }


def run(args):
    root = args.state_dir.expanduser().resolve()
    try:
        report = audit(root, args.checkpoint.expanduser().resolve() if args.checkpoint else None)
        path = root / "verification" / "trade-cycle.json"
        write_private_json(path, report)
        for step in report["steps"]:
            print(f"{step['status']} {step['step']}")
        for name, passed in report["checks"].items():
            print(f"{'PASS' if passed else 'NOT SATISFIED'} {name}")
        print(f"Report: {path}")
        return 0 if report["verified"] else 1
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        print("Cannot verify this match. Check the trade-cycle scenario, native checkpoint and round-0 snapshot.")
        return 2
