"""A compact review of permitted observations, not a combat planner or hidden-state oracle."""

from typing import Literal

from pydantic import Field

from civ_agent.game_commands import CityAttack, MoveCommand, Position, UnitAttack
from civ_agent.mechanics_overview import region_distance
from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel


class Threat(BoundaryModel):
    position: Position
    name: str
    civilization: str
    health: int


class NearbyThreat(Threat):
    distance: int


class KnownCamp(BoundaryModel):
    position: Position
    visible: bool
    lastObservedTurn: int | None = None


class BarbarianSighting(BoundaryModel):
    position: Position
    name: str
    lastSeenTurn: int
    turnsAgo: int


class UnitRisk(BoundaryModel):
    handle: str
    name: str
    position: Position
    health: int
    otherMilitaryOnSameTile: bool
    nearbyHostiles: list[NearbyThreat]


class Bombardment(BoundaryModel):
    city: str
    defender: str
    minDamage: int
    maxDamage: int
    maxRetaliation: int
    command: CityAttack
    inspection: JsonObject


class Loss(BoundaryModel):
    report: str
    turn: int
    text: str


class CivilianCapture(BoundaryModel):
    isOurCapturedUnit: bool
    unitIfKept: str | None


class CaptureOrder(BoundaryModel):
    command: MoveCommand | UnitAttack
    inspection: JsonObject


class NearbyCaptureUnit(BoundaryModel):
    unit: str
    distanceInHexes: int
    movement: str


class PotentialCaptureTarget(BoundaryModel):
    position: Position
    name: str
    civilization: str
    capture: CivilianCapture
    requiresWarDeclaration: bool
    postDeclarationReachability: Literal["NOT_EVALUATED"] | None = None
    guard: Threat | None = None
    city: str | None = None
    directCaptureOrders: list[CaptureOrder]
    nearestMilitary: list[NearbyCaptureUnit]


class TacticalReview(BoundaryModel):
    turn: int
    militaryCount: int
    visibleHostiles: list[Threat]
    knownBarbarianCamps: list[KnownCamp]
    recentBarbarianSightings: list[BarbarianSighting]
    civilianProtectionBasis: str | None = None
    civiliansNearHostiles: list[UnitRisk]
    militaryNearHostiles: list[UnitRisk]
    cityBombardments: list[Bombardment]
    recentLosses: list[Loss]
    potentialCaptureTargets: list[PotentialCaptureTarget]
    captureConditions: str | None = None
    cityCount: int
    citiesProducingSettlers: list[str]
    settlerUnits: list[str]
    nonReconMilitaryCount: int | None = None
    limits: str = (
        "Proximity includes visible hostile military within two hexes, using the engine's map distances. "
        "It does not predict enemy reach or reveal hidden units. An empty threat list does not establish safety. "
        "These observations are advisory; no attack, movement or written deferral is required."
    )


class StrategyNotice(BoundaryModel):
    id: str
    needsReview: list[str]


class StrategyReview(BoundaryModel):
    assessments: list[StrategyNotice] = Field(default_factory=list)


def direct_capture_orders(military: list[JsonObject], position: JsonObject) -> list[CaptureOrder]:
    """Project current native offers for an unguarded enemy civilian; never infer a route from distance."""
    result = []
    destination = Position.model_validate(position)
    for unit in military:
        # A ranged unit can enter a civilian's tile, but shooting it is not capture.
        move = next((m for m in unit.get("moves", []) if m["destination"] == position), None)
        attack = next(
            (
                a
                for a in (unit.get("combat") or {}).get("attacks", [])
                if a["destination"] == position and (a.get("consequences") or {}).get("civilianOrCityCapturePossible")
            ),
            None,
        )
        if move is not None:
            command = MoveCommand(kind="MOVE_UNIT", target=unit["handle"], destination=destination)
            section = "movement"
        elif attack is not None:
            command = UnitAttack(
                kind="ATTACK", target=unit["handle"], destination=destination, attackFrom=attack["attackFrom"]
            )
            section = "combat"
        else:
            continue
        result.append(
            CaptureOrder(
                command=command,
                inspection={"tool": "unit_details", "arguments": {"target": unit["handle"], "sections": [section]}},
            )
        )
    return result


def tactical_review(snapshot: JsonObject) -> TacticalReview:
    units = snapshot["map"]["units"]
    military = [u for u in units if u["military"]]
    threats = [
        Threat(position=tile["position"], name=u["name"], civilization=u["civilization"], health=u["health"])
        for tile in snapshot["map"]["tiles"]
        if tile["visible"]
        for u in tile["units"]
        if u["military"] and u["atWar"]
    ]
    captives = []
    for tile in snapshot["map"]["tiles"]:
        if not tile["visible"]:
            continue
        for unit in tile["units"]:
            if unit["military"] or not unit.get("capture"):
                continue
            guard = next((u for u in tile["units"] if u["military"]), None)
            city = tile.get("cityName")
            nearest = sorted(
                (
                    NearbyCaptureUnit(
                        unit=u["handle"],
                        distanceInHexes=region_distance(
                            u["position"], tile["position"], snapshot["map"].get("wrapOffset")
                        ),
                        movement=u["movement"],
                    )
                    for u in military
                    if (u.get("combat") or {}).get("role") != "AIR"
                ),
                key=lambda u: (u.distanceInHexes, u.unit),
            )[:2]
            captives.append(
                PotentialCaptureTarget(
                    position=tile["position"],
                    **{k: unit[k] for k in ("name", "civilization", "capture")},
                    requiresWarDeclaration=not unit["atWar"],
                    postDeclarationReachability="NOT_EVALUATED" if not unit["atWar"] else None,
                    guard=(
                        Threat(
                            position=tile["position"],
                            **{k: guard[k] for k in ("name", "civilization", "health")},
                        )
                        if guard
                        else None
                    ),
                    city=city,
                    directCaptureOrders=(
                        direct_capture_orders(military, tile["position"])
                        if unit["atWar"] and guard is None and city is None
                        else []
                    ),
                    nearestMilitary=nearest,
                )
            )
    civilians, exposed_military = [], []
    for u in units:
        if not u["nearbyHostiles"]:
            continue
        risk = UnitRisk(
            handle=u["handle"],
            name=u["name"],
            position=u["position"],
            health=u["health"],
            otherMilitaryOnSameTile=any(
                m["handle"] != u["handle"] and m["position"] == u["position"] for m in military
            ),
            nearbyHostiles=[
                NearbyThreat(
                    position=h["position"],
                    distance=h["distance"],
                    **{k: h["unit"][k] for k in ("name", "civilization", "health")},
                )
                for h in u["nearbyHostiles"]
            ],
        )
        (exposed_military if u["military"] else civilians).append(risk)
    losses = []
    for r in snapshot["combatReports"]:
        lost_defender = (r.get("defender") or {}).get("isOwn") and r.get("outcome") in {"Destroyed", "Captured"}
        lost_attacker = (r.get("attacker") or {}).get("isOwn") and r.get("attackerDefeatedByDefender")
        if lost_defender or lost_attacker or r.get("ourDestroyed"):
            losses.append(
                Loss(
                    report=r["id"],
                    turn=r["turn"],
                    text=r.get("text") or "An owned combatant was lost; read the combat report.",
                )
            )
    return TacticalReview(
        nonReconMilitaryCount=(snapshot.get("militaryReadiness") or {}).get("unitsExcludingRecon"),
        turn=snapshot["turn"],
        militaryCount=len(military),
        visibleHostiles=threats,
        knownBarbarianCamps=[
            KnownCamp(position=t["position"], visible=t["visible"], lastObservedTurn=t.get("observedTurn"))
            for t in snapshot["map"]["tiles"]
            if t.get("improvement") == "Barbarian encampment"
        ],
        recentBarbarianSightings=[
            BarbarianSighting(**sighting, turnsAgo=snapshot["turn"] - sighting["lastSeenTurn"])
            for sighting in snapshot["map"].get("recentBarbarianSightings", [])
        ],
        civilianProtectionBasis=(
            "Settler protection is in units[].protection. Candidates refer to the roster's health, position and orders. "
            "Estimated travel uses native pathfinding; the Settler must stay at its current tile for rendezvous. "
            "Same-tile military stays behind unless paired with ESCORT; a formation or city can still be defeated. "
            "NO_VISIBLE_CAPTURE_THREAT does not establish safety. Unseen-tile counts cover two hexes; camp/sighting "
            "references cover five hexes. Sightings retain last-seen positions for up to five turns, not current "
            "locations or an army count. Visible hostiles are listed separately."
            if any(u.get("protection") for u in units) or snapshot["map"].get("recentBarbarianSightings")
            else None
        ),
        civiliansNearHostiles=civilians,
        militaryNearHostiles=exposed_military,
        cityBombardments=[
            Bombardment(
                city=c["name"],
                defender=a["defender"],
                minDamage=a["minDamageToDefender"],
                maxDamage=a["maxDamageToDefender"],
                maxRetaliation=a["maxDamageToAttacker"],
                command=a["command"],
                inspection={"tool": "city_details", "arguments": {"target": c["handle"], "sections": ["combat"]}},
            )
            for c in snapshot["cities"]
            for a in c["attacks"]
        ],
        recentLosses=losses,
        potentialCaptureTargets=captives,
        captureConditions=(
            "unitIfKept is the resulting unit type, including any conversion; recovering our own Settler preserves it. "
            "A rescued foreign civilian may offer a return/keep choice. "
            "directCaptureOrders lists current native move/melee offers against an unguarded civilian outside a city. "
            "Inspect the listed unit section before issuing its command; discoveries can interrupt a planned route. "
            "A guard or city requires combat; ranged fire alone does not capture. "
            "nearestMilitary gives up to two units by hex distance, not path length or reachability. "
            "For peaceful targets, post-declaration routes are not evaluated: declaring war does not establish "
            "a capture this turn. Reinspect movement/combat after any declaration."
            if captives
            else None
        ),
        cityCount=len(snapshot["cities"]),
        citiesProducingSettlers=[c["name"] for c in snapshot["cities"] if c["constructionQueue"][:1] == ["Settler"]],
        settlerUnits=[u["handle"] for u in units if u["name"] == "Settler"],
    )


def strategy_review(records: JsonObject) -> StrategyReview:
    assessments = [
        StrategyNotice(
            id=key,
            needsReview=r["body"].get("needsReview", []),
        )
        for key, r in records.items()
        if r["category"] == "strategy" and r["body"].get("state") == "active" and r["body"].get("needsReview")
    ]
    return StrategyReview(assessments=assessments)
