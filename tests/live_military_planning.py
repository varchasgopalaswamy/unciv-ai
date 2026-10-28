"""Opt-in military scheduling scenarios against the live Winnow service.

Run with --output pointing at a fresh directory. Synthetic permitted player
observations are explicit fixtures; the authenticated broker, model calls,
confidence policy and checkpointed scheduling implementation are real.
"""

import argparse
import asyncio
import copy
import hashlib
import json
import secrets
from pathlib import Path

from aiohttp import web

from civ_agent.civilization_state import CivilizationState
from civ_agent.decisions_broker import DecisionsBroker, RoutingClient
from civ_agent.decisions_policy import DecisionsSettings
from civ_agent.diplomacy_decisions import DecisionsClient
from civ_agent.military_schedule import MilitarySchedule
from civ_agent.protocol import JsonObject
from civ_agent.tls import private_directory, write_private_json


def opening() -> JsonObject:
    return {
        "epoch": "military-live-evaluation",
        "civilization": "Rome",
        "turn": 40,
        "cities": [{"handle": "rome", "health": 200}],
        "diplomacy": {"knownCivilizations": [{"civilizationId": "Greece", "atWar": False}]},
        "economy": {
            "unitSupply": {"productionPenaltyPercent": 0},
            "resources": [{"name": "Iron", "type": "Strategic", "available": 0}],
        },
        "militaryReadiness": {
            "visibleBorderForces": [],
            "recruitment": [{"city": "rome", "units": ["Archer", "Spearman"]}],
        },
        "map": {
            "units": [
                {
                    "handle": f"guard-{i}",
                    "name": "Spearman",
                    "military": True,
                    "health": 100,
                    "movement": "2/2",
                    "idle": False,
                    "position": {"x": i, "y": 0},
                    "combat": {"strength": 11, "order": {"action": "Fortify"}},
                }
                for i in range(3)
            ],
            "tiles": [],
        },
    }


def scenarios():
    quiet = opening()
    scout = copy.deepcopy(quiet)
    scout["militaryReadiness"]["visibleBorderForces"] = [
        {
            "name": "Scout",
            "civilization": "Greece",
            "atWar": False,
            "position": {"x": 8, "y": 0},
            "health": 100,
            "strength": 5,
            "distanceToTerritory": 6,
        }
    ]
    moving_scout = copy.deepcopy(scout)
    moving_scout["militaryReadiness"]["visibleBorderForces"][0]["position"]["x"] = 9
    moving_scout["militaryReadiness"]["visibleBorderForces"][0]["distanceToTerritory"] = 7
    buildup = copy.deepcopy(quiet)
    buildup["militaryReadiness"]["visibleBorderForces"] = [
        {
            "name": name,
            "civilization": "Greece",
            "atWar": False,
            "position": {"x": i + 1, "y": 2},
            "health": 100,
            "strength": 34 if name == "Rifleman" else 14,
            "rangedStrength": 20 if name == "Cannon" else 0,
            "distanceToTerritory": 1,
        }
        for i, name in enumerate(["Rifleman", "Rifleman", "Cannon", "Cannon"])
    ]
    opportunity_before = copy.deepcopy(quiet)
    opportunity_before["map"]["tiles"] = [
        {
            "visible": True,
            "position": {"x": 3, "y": 1},
            "owner": "Greece",
            "cityName": "Sparta",
            "cityHealth": 200,
            "cityMaxHealth": 200,
            "cityDefensiveStrength": 18,
            "units": [{"name": "Spearman", "civilization": "Greece", "military": True, "health": 100, "atWar": False}],
        }
    ]
    opportunity_after = copy.deepcopy(opportunity_before)
    opportunity_after["map"]["tiles"][0].update(cityHealth=35, units=[])
    upgrade = copy.deepcopy(quiet)
    upgrade["economy"]["resources"][0]["available"] = 6
    for unit in upgrade["map"]["units"]:
        unit["economy"] = {"upgrades": [{"targetName": "Pikeman", "available": True}]}
    penalty = copy.deepcopy(quiet)
    penalty["economy"]["unitSupply"]["productionPenaltyPercent"] = -70
    recruitment = copy.deepcopy(quiet)
    recruitment["militaryReadiness"]["recruitment"][0]["units"].append("Trireme")
    return [
        (
            "distant-scout-patrol",
            scout,
            moving_scout,
            "wait",
            "A lone peaceful scout moves farther from the border; routine patrol need not reopen planning.",
        ),
        (
            "peaceful-border-buildup",
            quiet,
            buildup,
            "review",
            "Visible modern forces mass beside the border; readiness and deployments need attention before turn 45.",
        ),
        (
            "damaged-neighbor-city",
            opportunity_before,
            opportunity_after,
            "review",
            "A known nearby city is now badly damaged and its observed defender gone; assess the opportunity, not an automatic war.",
        ),
        (
            "new-upgrade-window",
            quiet,
            upgrade,
            "review",
            "The existing army can now upgrade and strategic resources became available; reconsider readiness and investment.",
        ),
        (
            "severe-supply-penalty",
            quiet,
            penalty,
            "review",
            "A new 70% production penalty changes the cost of keeping the present force and recruiting.",
        ),
        (
            "additional-recruitment-choice",
            quiet,
            recruitment,
            "wait",
            "One additional build option alone provides no deadline, new threat or demonstrated campaign need.",
        ),
    ]


def schedule_for(root, baseline):
    memory = CivilizationState(private_directory(root), "Rome")
    memory.bind(baseline["epoch"])
    schedule = MilitarySchedule(memory, 5)
    state = {"episodes": {}, "escorts": {}, "operations": {}, "messages": []}
    assert schedule.complete(baseline, 40, state)
    return schedule, state


def deterministic_scenarios(root):
    baseline = opening()
    results: list[JsonObject] = []
    schedule, state = schedule_for(root / "cadence", baseline)
    quiet = schedule.assess(baseline, 41, state)
    results.append(
        {
            "id": "quiet-turn-dormant",
            "passed": not quiet["reasons"] and schedule.triage_input(baseline, 41, state, quiet) is None,
        }
    )
    assert schedule.complete(baseline, 42, state, full=False)
    due = MilitarySchedule(schedule.memory, 5).assess(baseline, 45, state)
    results.append(
        {
            "id": "periodic-after-targeted-pass-and-restart",
            "passed": due["fullReview"] and due["lastFullReviewTurn"] == 40 and due["nextReviewTurn"] == 45,
        }
    )
    for name, changed in (
        ("damage-wakes-immediately", lambda v: v["map"]["units"][0].update(health=45)),
        ("war-wakes-immediately", lambda v: v["diplomacy"]["knownCivilizations"][0].update(atWar=True)),
        (
            "barbarian-bombardment-wakes-immediately",
            lambda v: v["cities"][0].update(attacks=[{"destination": {"x": 1, "y": 1}}]),
        ),
    ):
        schedule, state = schedule_for(root / name, baseline)
        current = copy.deepcopy(baseline)
        changed(current)
        status = schedule.assess(current, 41, state)
        results.append({"id": name, "passed": bool(status["reasons"]), "schedule": status})
    return results


async def run(args):
    root = args.output.resolve()
    if root.exists():
        raise ValueError("Use a fresh output directory; preserve previous attempts")
    private_directory(root)
    directory = private_directory(root / "broker")
    token = secrets.token_urlsafe(32)
    write_private_json(
        directory / "decisions-clients.json", {"military-live": hashlib.sha256(token.encode()).hexdigest()}
    )
    write_private_json(directory / "client.json", {"token": token})
    settings = DecisionsSettings(decisions_enabled=True, decisions_backend="jev")
    upstream = DecisionsClient(backend="jev", endpoint=args.endpoint)
    broker = DecisionsBroker(directory, upstream)
    broker.ledger.register("military-live", root.name, "Rome", settings.model_dump())
    runner = web.AppRunner(broker.application(), access_log=None)
    await runner.setup()
    # Unix-domain paths have a short system limit; keep the ephemeral socket in /tmp.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="military-live-") as ipc:
        socket = Path(ipc) / "broker.sock"
        await web.UnixSite(runner, str(socket)).start()
        client = RoutingClient(socket, directory / "client.json", backend="jev")
        report: JsonObject = {
            "backend": "jev",
            "model": "Winnow-12B",
            "fixtureType": "synthetic permitted observations",
            "acceptanceProbability": settings.decisions_acceptance_probability,
            "deterministic": deterministic_scenarios(root / "deterministic"),
            "live": [],
        }
        try:
            report["healthBefore"] = await client.health()
            for name, baseline, current, expected, rationale in scenarios():
                schedule, state = schedule_for(root / name, baseline)
                current["turn"] = 41
                status = schedule.assess(current, 41, state)
                evidence = schedule.triage_input(current, 41, state, status)
                assert evidence is not None
                write_private_json(root / name / "evidence.json", evidence)
                result = await client.batch(evidence, timeout=settings.decisions_timeout_seconds)
                accepted_review = schedule.triage_result(evidence, result, current, state)
                answers = result.get("answers", [])
                choice = next((a["choice"] for a in answers if a["name"] == "military_review"), None)
                entry = {
                    "id": name,
                    "expected": expected,
                    "rationale": rationale,
                    "acceptedChoice": choice,
                    "wakesPlanning": accepted_review,
                    "passed": choice == expected,
                    "result": result,
                }
                if choice == "wait":
                    entry["unchangedWaitCached"] = (
                        schedule.triage_input(current, 42, state, schedule.assess(current, 42, state)) is None
                    )
                    entry["passed"] &= entry["unchangedWaitCached"]
                report["live"].append(entry)
                write_private_json(root / "results.json", report)
                print(
                    json.dumps(
                        {k: entry[k] for k in ("id", "expected", "acceptedChoice", "wakesPlanning", "passed")}
                        | {
                            "latencyMs": result.get("latencyMs"),
                            "fallback": result.get("fallback"),
                            "questionResults": result.get("questionResults"),
                        }
                    ),
                    flush=True,
                )
                if result.get("providerError"):
                    break
            report["healthAfter"] = await client.health()
            report["passed"] = len(report["live"]) == len(scenarios()) and all(
                entry["passed"] for entry in report["live"] + report["deterministic"]
            )
            write_private_json(root / "results.json", report)
            return 0 if report["passed"] else 1
        finally:
            await runner.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--endpoint", default="http://127.0.0.1:8091/v1/systemone")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
