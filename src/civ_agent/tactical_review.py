"""A compact review of permitted observations, not a combat planner or hidden-state oracle."""

from pydantic import Field

from civ_agent.game_commands import CityAttack, Position
from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel


class Threat(BoundaryModel):
    position: Position
    name: str
    civilization: str
    health: int


class NearbyThreat(Threat):
    distance: int


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


class CapturableCivilian(BoundaryModel):
    position: Position
    name: str
    civilization: str
    atWar: bool
    capture: CivilianCapture
    guard: Threat | None = None


class TacticalReview(BoundaryModel):
    turn: int
    militaryCount: int
    visibleHostiles: list[Threat]
    civiliansNearHostiles: list[UnitRisk]
    militaryNearHostiles: list[UnitRisk]
    cityBombardments: list[Bombardment]
    recentLosses: list[Loss]
    capturableCivilians: list[CapturableCivilian]
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
            captives.append(
                CapturableCivilian(
                    position=tile["position"],
                    **{k: unit[k] for k in ("name", "civilization", "atWar", "capture")},
                    guard=(
                        Threat(
                            position=tile["position"],
                            **{k: guard[k] for k in ("name", "civilization", "health")},
                        )
                        if guard
                        else None
                    ),
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
        capturableCivilians=captives,
        captureConditions=(
            "unitIfKept is the resulting unit type, including any conversion; recovering our own Settler preserves it. "
            "A rescued foreign civilian may offer a return/keep choice. "
            "Capture requires entry by a military unit after any hostile guard is cleared; ranged fire alone does not capture. "
            "Units at peace cannot be captured without war. Inspect current combat/movement options for availability."
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
