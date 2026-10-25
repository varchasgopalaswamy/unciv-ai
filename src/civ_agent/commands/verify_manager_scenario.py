"""Audit controlled economy/war exercises using receipts, memory and native saves."""

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from civ_agent.economy_roles import CITY_COMMANDS
from civ_agent.protocol import JsonObject
from civ_agent.tls import write_private_json

MAPS = {"Economy manager acceptance": "economy-manager", "Military manager acceptance": "military-manager"}
PLAYERS = ("Rome", "Greece")


def parser(*, add_help=True):
    result = argparse.ArgumentParser(add_help=add_help)
    result.add_argument("--state-dir", type=Path, required=True)
    result.add_argument(
        "--save-baseline", action="store_true", help="Save a completed, stopped first-stage boundary before game resume"
    )
    return result


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read_memory(path):
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        meta = {row["key"]: json.loads(row["value"]) for row in db.execute("SELECT * FROM meta")}
        jobs = {row["id"]: dict(row) for row in db.execute("SELECT jobs.* FROM jobs JOIN live_jobs USING(id)")}
        calls = [
            {**dict(row), "arguments": json.loads(row["arguments"]), "result": json.loads(row["result"])}
            for row in db.execute("""SELECT rowid AS ordinal,* FROM calls
                WHERE result IS NOT NULL AND job IN (SELECT id FROM live_jobs) ORDER BY rowid""")
        ]
        actions = [
            {**dict(row), "payload": json.loads(row["payload"]), "result": json.loads(row["result"])}
            for row in db.execute("""SELECT * FROM outbox
                WHERE result IS NOT NULL AND job IN (SELECT id FROM live_jobs)""")
        ]
        records = [
            {**dict(row), "body": json.loads(row["body"])}
            for row in db.execute("""WITH RECURSIVE reachable(key,version) AS (
            SELECT key,version FROM heads UNION SELECT r.key,r.parent FROM records r JOIN reachable a USING(key,version) WHERE r.parent IS NOT NULL
            ) SELECT r.* FROM records r JOIN reachable USING(key,version)""")
        ]
        heads = [
            {**dict(row), "body": json.loads(row["body"])}
            for row in db.execute("SELECT r.* FROM records r JOIN heads USING(key,version)")
        ]
    return {"meta": meta, "jobs": jobs, "calls": calls, "actions": actions, "records": records, "heads": heads}


def success(result):
    return "error" not in result


def code(result):
    error = result.get("error")
    return error.get("code") if isinstance(error, dict) else error


def fingerprints(memory):
    return {f"{r['key']}@{r['version']}": digest(r["body"]) for r in memory["records"] if r["category"] == "city_plan"}


def native_units(game, player):
    """Unciv serializes units on their tiles, rather than on civilizations."""
    return [
        unit
        for tile in game["tileMap"].get("tileList", [])
        for unit in (tile.get("militaryUnit"), tile.get("civilianUnit"), *(tile.get("airUnits") or []))
        if unit is not None and unit.get("owner") == player
    ]


def verify(root, *, save_baseline=False):
    root = root.resolve()
    raw = (root / "exploration.json").read_bytes()
    saved = json.loads(raw)
    game = json.loads(saved["gameJson"])
    variant = MAPS[game["tileMap"]["mapParameters"]["name"]]
    memories = {
        player: read_memory(root / "agent-services" / player / "civilization" / "memory.sqlite3") for player in PLAYERS
    }
    if any(m["meta"].get("epoch") != saved["epoch"] or m["meta"].get("civilization") != p for p, m in memories.items()):
        raise ValueError("Memory identity or epoch does not match the native checkpoint")
    turn = game.get("turns", 0)
    if game.get("currentPlayer") != "Rome":
        raise ValueError("Audit at a completed round boundary with Rome next")
    output = root / "verification"
    baseline_path = output / "manager-baseline.json"
    if save_baseline:
        if turn < 2 or any(j["status"] == "RUNNING" for m in memories.values() for j in m["jobs"].values()):
            raise ValueError("Stop after at least two completed rounds before saving the restart baseline")
        if baseline_path.exists():
            raise ValueError("A restart baseline already exists; preserve it or choose a fresh test directory")
        write_private_json(
            baseline_path,
            {
                "variant": variant,
                "epoch": saved["epoch"],
                "gameId": game["gameId"],
                "turn": turn,
                "checkpointSha256": hashlib.sha256(raw).hexdigest(),
                "players": {
                    p: {"storeId": m["meta"].get("storeId"), "plans": fingerprints(m), "jobs": sorted(m["jobs"])}
                    for p, m in memories.items()
                },
            },
        )
    baseline = json.loads(baseline_path.read_text()) if baseline_path.exists() else None
    checks = {}
    evidence: JsonObject = {}
    for player, m in memories.items():
        plans = [r for r in m["heads"] if r["category"] == "city_plan" and r["body"].get("state") == "active"]
        calls = m["calls"]
        ends = [
            c
            for c in calls
            if c["name"] == "player_command" and c["arguments"].get("kind") == "END_TURN" and success(c["result"])
        ]
        overrides = {
            (c["job"], c["arguments"]["city"]) for c in calls if c["name"] == "city_override" and success(c["result"])
        }
        city_actions = [
            a
            for a in m["actions"]
            if a["transport"] == "player_command" and a["payload"].get("kind") in CITY_COMMANDS and success(a["result"])
        ]
        checks[player + ":cityPlans"] = len(plans) == 2 and all(
            r["body"].get("reasons") and r["body"].get("reconsiderWhen") for r in plans
        )
        checks[player + ":confirmedEconomyActions"] = any(
            r["body"].get("_confirmedActions") or r["body"].get("confirmedActions") for r in plans
        )
        checks[player + ":roleOwnership"] = bool(city_actions) and all(
            m["jobs"][a["job"]]["purpose"] == "economy" or (a["job"], a["payload"].get("target")) in overrides
            for a in city_actions
        )
        checks[player + ":turnBarrier"] = len(ends) >= turn and all(
            all(
                c["result"].get("economyCoordination", {}).get("rolesFinished", {}).get(r) is True
                for r in ("gameplay", "economy")
            )
            and all(c["result"]["economyCoordination"]["rolesFinished"].values())
            for c in ends
        )
        checks[player + ":plansInLaterPreload"] = any(
            c["id"].startswith("startup:memory:")
            and m["jobs"][c["job"]]["purpose"] == "economy"
            and m["jobs"][c["job"]]["game_turn"] > 0
            and any(r.get("type") == "city_plan" for r in c["result"].get("records", []))
            for c in calls
        )
        checks[player + ":restartHistory"] = bool(
            baseline
            and baseline.get("variant") == variant
            and baseline.get("epoch") == saved["epoch"]
            and baseline.get("gameId") == game["gameId"]
            and turn >= baseline["turn"] + 2
            and baseline["players"][player]["storeId"] == m["meta"].get("storeId")
            and baseline["players"][player]["plans"]
            and all(fingerprints(m).get(k) == v for k, v in baseline["players"][player]["plans"].items())
            and any(
                j not in baseline["players"][player]["jobs"]
                and row["purpose"] == "economy"
                and row["game_turn"] >= baseline["turn"]
                for j, row in m["jobs"].items()
            )
        )
        evidence[player] = {
            "cityPlans": [r["key"] for r in plans],
            "nativeCityActions": len(city_actions),
            "nativeHandoffs": len(ends),
        }
    native_civs = {c["civName"]: c for c in game["civilizations"]}
    for player in PLAYERS:
        cities = native_civs[player].get("cities", [])
        checks[player + ":productiveCities"] = len(cities) == 2 and all(
            city.get("cityConstructions", {}).get("constructionQueue", [])
            and city["cityConstructions"]["constructionQueue"][0] != "The Pyramids"
            for city in cities
        )
        checks[player + ":populationPreserved"] = len(cities) == 2 and all(
            city.get("population", {}).get("population", 1) >= 4 for city in cities
        )
        evidence[player]["nativeCities"] = [
            {
                "name": city.get("name"),
                "population": city.get("population", {}).get("population", 1),
                "avoidGrowth": city.get("avoidGrowth", False),
                "queue": city.get("cityConstructions", {}).get("constructionQueue", []),
            }
            for city in cities
        ]
    rome = memories["Rome"]
    if variant == "economy-manager":
        purchases = [
            a
            for a in rome["actions"]
            if a["payload"].get("kind") == "BUY_CONSTRUCTION" and a["payload"].get("selection") == "Worker"
        ]
        blocked = [a for a in purchases if code(a["result"]) == "OPTION_UNAVAILABLE"]
        checks["reservedPurchaseRejected"] = len(blocked) == 1 and any(
            r["category"] == "observation"
            and (r["body"].get("economy") or {}).get("committedGold", 0) == 480
            and (r["body"].get("economy") or {}).get("availableGold", 999) <= 20
            for r in rome["records"]
        )
        checks["purchaseAfterRelease"] = any(success(a["result"]) for a in purchases)
        checks["urgentRequest"] = any(
            c["name"] == "economy_request" and c["arguments"].get("priority") == "urgent" and success(c["result"])
            for c in rome["calls"]
        )
        checks["economyReopened"] = any(
            sum(
                c["name"] == "finish_role" and c["job"] == j and c["result"].get("roleFinished", False)
                for c in rome["calls"]
            )
            >= 2
            for j, row in rome["jobs"].items()
            if row["purpose"] == "economy"
        )
        checks["explicitOverride"] = any(c["name"] == "city_override" and success(c["result"]) for c in rome["calls"])
        seeded = [
            v[-1]
            for v in saved["communication"]["trades"]["proposals"].values()
            if v[0].get("nativeRequestId") and v[0].get("author") == "Rome" and v[0].get("partner") == "France"
        ]
        checks["preparedOfferWithdrawn"] = len(seeded) == 1 and seeded[0]["status"] == "WITHDRAWN"
        checks["noMilitaryInPeace"] = all(
            j["purpose"] != "military" for m in memories.values() for j in m["jobs"].values()
        )
        evidence["expectedRejection"] = [code(a["result"]) for a in blocked]
    else:
        for player, m in memories.items():
            state = m["meta"].get("war:state", {})
            episodes = list(state.get("episodes", {}).values())
            checks[player + ":militaryRan"] = any(
                j["purpose"] == "military" and j["status"] == "COMPLETED" for j in m["jobs"].values()
            )
            checks[player + ":goalsDiscussedAndRevised"] = any(
                e["version"] >= 2 and e["history"] and any(h.get("responses") for h in e["history"]) for e in episodes
            )
            checks[player + ":operationNotes"] = bool(state.get("operations"))
            checks[player + ":warEnded"] = bool(episodes) and all(not e["active"] for e in episodes)
            consultations = [v for k, v in m["meta"].items() if k.startswith("peace:")]
            checks[player + ":peaceReviewedAndApproved"] = any(
                v.get("status") == "complete" and v.get("overview") and v.get("approval", {}).get("approve")
                for v in consultations
            )
            checks[player + ":peacefulContinuation"] = any(
                j["purpose"] == "gameplay" and j["game_turn"] >= 3 and j["status"] == "COMPLETED"
                for j in m["jobs"].values()
            ) and not any(j["purpose"] == "military" and j["game_turn"] >= 3 for j in m["jobs"].values())
        assignments = [
            c
            for c in rome["calls"]
            if c["name"] == "escort_assignment" and success(c["result"]) and not c["arguments"].get("release")
        ]
        escort_units = {c["arguments"]["unit"] for c in assignments}
        civilians = {c["arguments"].get("civilian") for c in assignments}
        moved = {
            a["payload"].get("target")
            for a in rome["actions"]
            if a["payload"].get("kind") in {"MOVE_UNIT", "SET_UNIT_DESTINATION"}
            and success(a["result"])
            and rome["jobs"][a["job"]]["purpose"] == "gameplay"
        }
        checks["mainMovedCivilianAndEscort"] = bool(escort_units) and escort_units <= moved and civilians <= moved
        checks["militaryRespectedEscort"] = bool(escort_units) and not any(
            a["payload"].get("target") in escort_units
            and success(a["result"])
            and rome["jobs"][a["job"]]["purpose"] == "military"
            for a in rome["actions"]
        )
        civs = {c["civName"]: c for c in game["civilizations"]}
        relation = civs["Rome"].get("diplomacy", {}).get("Greece")
        checks["escortSurvived"] = {"Road Crew", "Escort Guard"} <= {
            u.get("instanceName") for u in native_units(game, "Rome")
        }
        evidence["survivingUnits"] = {
            p: [
                {"name": u.get("name"), "label": u.get("instanceName"), "health": u.get("health", 100)}
                for u in native_units(game, p)
            ]
            for p in PLAYERS
        }
        checks["nativePeace"] = relation is not None and relation.get("diplomaticStatus", "War") != "War"
    return {
        "verified": all(checks.values()),
        "status": "PASSED" if all(checks.values()) else "PENDING" if turn < 6 else "FAILED",
        "variant": variant,
        "gameTurn": turn,
        "checks": checks,
        "evidence": evidence,
        "checkpointSha256": hashlib.sha256(raw).hexdigest(),
        "baselineSaved": save_baseline,
        "limitations": "Audits native effects and stored role evidence. Reasons, goal progress and tactical judgments remain model assessments. Restart evidence proves preserved history and later fresh jobs, not an OS-process restart. This is not a win-rate or play-quality benchmark.",
    }


def run(args):
    try:
        report = verify(args.state_dir, save_baseline=args.save_baseline)
        write_private_json(args.state_dir / "verification" / "manager-scenario.json", report)
    except (OSError, ValueError, KeyError, sqlite3.Error):
        print("Cannot audit this manager exercise; check scenario identity, completed round and private memory files.")
        return 1
    print(json.dumps(report, indent=2))
    return 0 if report["verified"] or args.save_baseline else 2
