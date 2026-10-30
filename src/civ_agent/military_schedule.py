"""Checkpointed military planning and execution wakeups from permitted facts."""

import copy

from civ_agent.decisions_ledger import fingerprint
from civ_agent.decisions_tasks import TASK_VERSION
from civ_agent.protocol import JsonObject
from civ_agent.war_state import goal_versions, war_opponents


def _units(view):
    return (view.get("map") or {}).get("units", view.get("units", []))


def _needs_orders(unit):
    return bool(unit.get("idle")) and float(str(unit.get("movement", "0")).partition("/")[0]) > 0


def military_facts(view: JsonObject, state: JsonObject, civilization: str | None = None) -> JsonObject:
    """No world access, inferred enemy totals, turn clocks or stored research progress."""
    escorts = state.get("escorts", {})
    units = {}
    for unit in _units(view):
        if not unit.get("military") or unit["handle"] in escorts:
            continue
        combat = unit.get("combat") or {}
        upgrades = (unit.get("economy") or {}).get("upgrades", unit.get("upgrades", []))
        units[unit["handle"]] = {
            "name": unit.get("name"),
            "health": unit.get("health"),
            "position": unit.get("position"),
            "needsOrders": _needs_orders(unit),
            "order": (combat.get("order") or {}).get("action", unit.get("order")),
            "destination": (unit.get("movementOrder") or {}).get("destination"),
            "strength": combat.get("strength", unit.get("strength")),
            "rangedStrength": combat.get("rangedStrength", unit.get("rangedStrength")),
            "upgrades": sorted(u.get("targetName", u.get("name", "")) for u in upgrades if u.get("available")),
        }
    civilization = civilization or (view.get("civilizationProfile") or {}).get("name") or view.get("civilization")
    contacts, capture_targets, foreign_cities = [], [], []
    for tile in (view.get("map") or {}).get("tiles", []):
        if not tile.get("visible"):
            continue
        for unit in tile.get("units", []):
            if civilization is not None and unit.get("civilization") == civilization:
                continue
            if unit.get("military"):
                contacts.append(
                    {
                        "civilization": unit.get("civilization"),
                        "name": unit.get("name"),
                        "health": unit.get("health"),
                        "atWar": unit.get("atWar", False),
                        "position": tile.get("position"),
                    }
                )
            elif unit.get("capture"):
                capture_targets.append(
                    {
                        "civilization": unit.get("civilization"),
                        "name": unit.get("name"),
                        "position": tile.get("position"),
                        "capture": copy.deepcopy(unit["capture"]),
                        "atWar": unit.get("atWar", False),
                    }
                )
        if tile.get("cityName") and (civilization is None or tile.get("owner") != civilization):
            foreign_cities.append(
                {
                    key: copy.deepcopy(tile.get(key))
                    for key in ("position", "owner", "cityName", "cityHealth", "cityMaxHealth", "cityDefensiveStrength")
                }
            )
    contacts.sort(key=fingerprint)
    capture_targets.sort(key=fingerprint)
    foreign_cities.sort(key=fingerprint)
    cities = {
        c["handle"]: {
            "health": c.get("health"),
            "position": (c.get("development") or {}).get("position", c.get("position")),
        }
        for c in view.get("cities", [])
    }
    economy = view.get("economy") or {}
    material = {
        "forces": {h: {k: u[k] for k in ("name", "strength", "rangedStrength", "upgrades")} for h, u in units.items()},
        "contacts": contacts,
        "captureTargets": capture_targets,
        "visibleForeignCities": foreign_cities,
        "borderForces": [
            {
                key: copy.deepcopy(force[key])
                for key in (
                    "name",
                    "civilization",
                    "atWar",
                    "position",
                    "health",
                    "strength",
                    "rangedStrength",
                    "distanceToTerritory",
                )
                if key in force
            }
            for force in (view.get("militaryReadiness") or {}).get("visibleBorderForces", [])
        ],
        "recruitment": [
            {"city": row.get("city"), "units": sorted(row.get("units", []))}
            for row in (view.get("militaryReadiness") or {}).get("recruitment", [])
        ],
        "strategicResources": [
            {k: r.get(k) for k in ("name", "available", "imported", "exported")}
            for r in economy.get("resources", [])
            if r.get("type") == "Strategic"
        ],
        "unitSupply": (economy.get("unitSupply") or {}).get("productionPenaltyPercent"),
        "wars": sorted(war_opponents(view)),
        "goals": goal_versions(state),
        "operations": {
            key: {
                field: copy.deepcopy(operation.get(field))
                for field in ("id", "opponents", "plan", "reconsiderWhen", "reviewTurn")
            }
            for key, operation in state.get("operations", {}).items()
            if operation.get("status", "legacy") == "active"
        },
        "legacyOperations": sum(
            operation.get("status", "legacy") == "legacy" for operation in state.get("operations", {}).values()
        ),
    }
    attacks_available = any(city.get("attacks") for city in view.get("cities", [])) or any(
        (unit.get("combat") or {}).get("attacks") for unit in _units(view) if unit.get("handle") in units
    )
    return {"units": units, "cities": cities, "attacksAvailable": attacks_available, "material": material}


class MilitarySchedule:
    def __init__(self, memory, interval=5):
        self.memory, self.interval = memory, interval

    def saved(self):
        with self.memory.transaction() as db:
            return self.memory._meta(db, "military:schedule", {})

    def assess(self, view, turn, state):
        saved, facts = self.saved(), military_facts(view, state, self.memory.civilization)
        last, full_turn = saved.get("lastReviewTurn"), saved.get("lastFullReviewTurn")
        due = full_turn + self.interval if full_turn is not None else None
        baseline = saved.get("baseline") or {}
        old_units, units = baseline.get("units", {}), facts["units"]
        reasons, full = [], False
        if units or facts["cities"] or facts["material"]["wars"]:
            if full_turn is None:
                reasons.append("First military planning review")
                full = True
            elif due is not None and turn >= due:
                reasons.append("Periodic military planning review due")
                full = True
            previous_wars = (baseline.get("material") or {}).get("wars", [])
            if baseline and previous_wars != facts["material"]["wars"]:
                reasons.append("War started or ended")
                full = True
            if facts["material"]["wars"] and last != turn:
                reasons.append("Wartime execution requires review")
            if facts["attacksAvailable"] and last != turn:
                reasons.append("Native attacks require tactical review")
            if old_units.keys() - units.keys():
                reasons.append("Combat units lost or reassigned")
            if any(
                isinstance(u["health"], (int, float))
                and isinstance(old_units.get(h, {}).get("health"), (int, float))
                and u["health"] < old_units[h]["health"]
                for h, u in units.items()
            ):
                reasons.append("Combat units took damage")
            if baseline.get("cities", {}).keys() - facts["cities"].keys():
                reasons.append("Owned city lost")
                full = True
            for handle, city in facts["cities"].items():
                previous = baseline.get("cities", {}).get(handle, {})
                if (
                    isinstance(city["health"], (int, float))
                    and isinstance(previous.get("health"), (int, float))
                    and city["health"] < previous["health"]
                ):
                    reasons.append("Owned city took damage")
            old_hostiles = [c for c in (baseline.get("material") or {}).get("contacts", []) if c["atWar"]]
            hostiles = [c for c in facts["material"]["contacts"] if c["atWar"]]
            if hostiles and hostiles != old_hostiles:
                reasons.append("Visible hostile forces changed")
            hostile_civilians = [c for c in facts["material"]["captureTargets"] if c["atWar"]]
            previous_civilians = [
                c for c in (baseline.get("material") or {}).get("captureTargets", []) if c.get("atWar")
            ]
            if hostile_civilians and hostile_civilians != previous_civilians:
                reasons.append("Visible hostile capture opportunities changed")
            for handle, unit in units.items():
                old = old_units.get(handle)
                if unit["needsOrders"] and (old is None or not old.get("needsOrders")):
                    reasons.append("Combat unit needs orders: " + handle)
                elif old and old.get("destination") and not unit.get("destination"):
                    reasons.append("Military movement completed or was interrupted: " + handle)
            for operation in state.get("operations", {}).values():
                date = operation.get("reviewTurn")
                if date is not None and date <= turn and (last is None or last < date):
                    reasons.append("Planned military review due: " + operation["id"])
                    full = True
        return {
            "lastReviewTurn": last,
            "lastFullReviewTurn": full_turn,
            "nextReviewTurn": due,
            "atWar": bool(facts["material"]["wars"]),
            "fullReview": full,
            "reasons": list(dict.fromkeys(reasons)),
            "materialChanged": bool(saved.get("planningBaseline") and saved["planningBaseline"] != facts["material"]),
        }

    def complete(self, view, turn, state, observed=None, *, full=True):
        facts = military_facts(view, state, self.memory.civilization)
        if observed is not None and facts != military_facts(observed, state, self.memory.civilization):
            return False
        with self.memory.transaction() as db:
            if self.memory._meta(db, "epoch") != view.get("epoch"):
                raise ValueError("STALE_EPOCH")
            saved = self.memory._meta(db, "military:schedule", {})
            saved.update(lastReviewTurn=turn, baseline=facts)
            if full:
                saved.update(lastFullReviewTurn=turn, planningBaseline=facts["material"])
                saved.pop("triage", None)
            self.memory._set(db, "military:schedule", saved)
        return True

    def triage_input(self, view, turn, state, status) -> JsonObject | None:
        saved = self.saved()
        if status.get("fullReview") or not status.get("materialChanged"):
            return None
        material = military_facts(view, state, self.memory.civilization)["material"]
        signature = fingerprint(
            {"baseline": saved["planningBaseline"], "current": material, "nextReviewTurn": status["nextReviewTurn"]}
        )
        prior = saved.get("triage", {})
        if prior.get("signature") == signature:
            # Reconcile only an uncertain original request; do not reclassify a wait.
            return copy.deepcopy(prior["evidence"]) if prior.get("status") == "pending" else None
        evidence: JsonObject = {
            "version": TASK_VERSION,
            "player": self.memory.civilization,
            "militaryReview": {
                "baselineTurn": saved["lastFullReviewTurn"],
                "currentTurn": turn,
                "nextReviewTurn": status["nextReviewTurn"],
                "baseline": copy.deepcopy(saved["planningBaseline"]),
                "current": material,
                "coverage": "Permitted observations only; missing or unseen enemy forces remain unknown.",
            },
        }
        with self.memory.transaction() as db:
            saved = self.memory._meta(db, "military:schedule", {})
            saved["triage"] = {"signature": signature, "evidence": copy.deepcopy(evidence), "status": "pending"}
            self.memory._set(db, "military:schedule", saved)
        return evidence

    def triage_result(self, evidence, result, view=None, state=None):
        accepted = any(a["name"] == "military_review" and a["choice"] == "review" for a in result.get("answers", []))
        with self.memory.transaction() as db:
            saved = self.memory._meta(db, "military:schedule", {})
            triage = saved.get("triage", {})
            if triage.get("evidence") != evidence:
                return False
            if (
                view is not None
                and military_facts(view, state or {"episodes": {}}, self.memory.civilization)["material"]
                != evidence["militaryReview"]["current"]
            ):
                triage.update(status="stale", requestId=result.get("requestId"))
                self.memory._set(db, "military:schedule", saved)
                return False
            pending = result.get("fallback") in {"completion_unknown", "broker_unavailable", "malformed_broker_output"}
            triage.update(
                status="pending" if pending else "review" if accepted else "wait",
                requestId=result.get("requestId"),
                fallback=result.get("fallback"),
            )
            self.memory._set(db, "military:schedule", saved)
        return accepted
