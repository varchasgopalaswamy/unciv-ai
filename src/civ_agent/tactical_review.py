"""A compact review of permitted observations, not a combat planner or hidden-state oracle."""

from typing import Literal

from pydantic import Field

from civ_agent.game_commands import CityAttack, Position
from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel


class MilitaryUnit(BoundaryModel):
    handle: str
    name: str
    position: Position
    health: int
    strength: int
    rangedStrength: int
    order: str | None
    availableAttacks: int


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
    militaryOnSameTile: bool
    nearbyHostiles: list[NearbyThreat]


class Bombardment(BoundaryModel):
    city: str
    defender: str
    minDamage: int
    maxDamage: int
    maxRetaliation: int
    command: CityAttack


class Loss(BoundaryModel):
    report: str
    turn: int
    text: str


class TacticalReview(BoundaryModel):
    turn: int
    militaryCount: int
    military: list[MilitaryUnit]
    visibleHostiles: list[Threat]
    civiliansNearHostiles: list[UnitRisk]
    injuredMilitaryNearHostiles: list[UnitRisk]
    cityBombardments: list[Bombardment]
    recentLosses: list[Loss]
    cityCount: int
    citiesProducingSettlers: list[str]
    settlerUnits: list[str]
    treasury: int | None
    goldPerTurn: int | None
    happiness: int | None
    limits: str = (
        "Proximity includes visible hostile military within two hexes, using the engine's map distances. "
        "It does not predict enemy reach or reveal hidden units. An empty threat list does not establish safety. "
        "Review city bombardments even when no units are idle. Copy commands only after reading their offers; add intent."
    )


class PlanReview(BoundaryModel):
    id: str
    subjects: list[str]
    objective: str
    needsReview: list[str]


class StrategyReview(BoundaryModel):
    plans: list[PlanReview] = Field(default_factory=list)
    missingSubjects: list[str] = Field(default_factory=list)
    instruction: str = (
        "Keep separate defense, expansion and economy plans with measurable successCriteria and a reviewAfterTurn. "
        "Reconsider flagged plans using current evidence; revise or retire them. Deliberately delaying expansion "
        "or an available attack is allowed: record the reason and when to reconsider."
    )


class TacticalReviewRequired(BoundaryModel):
    error: Literal["TACTICAL_REVIEW_REQUIRED"] = "TACTICAL_REVIEW_REQUIRED"
    applied: Literal[False] = False
    recoverable: Literal[True] = True
    concerns: list[Literal["cityBombardments", "civiliansNearHostiles", "injuredMilitaryNearHostiles"]]
    instruction: str = (
        "Review tacticalReview. Act or supply a brief deferReason explaining why these concerns can wait. "
        "No attack or movement is mandatory."
    )


def tactical_review(snapshot: JsonObject) -> TacticalReview:
    units = snapshot["map"]["units"]
    military = [u for u in units if u["military"]]
    military_tiles = [u["position"] for u in military]
    threats = [
        Threat(position=tile["position"], name=u["name"], civilization=u["civilization"], health=u["health"])
        for tile in snapshot["map"]["tiles"]
        if tile["visible"]
        for u in tile["units"]
        if u["military"] and u["atWar"]
    ]
    civilians, injured = [], []
    for u in units:
        if not u["nearbyHostiles"] or u["military"] and u["health"] >= 100:
            continue
        risk = UnitRisk(
            handle=u["handle"],
            name=u["name"],
            position=u["position"],
            health=u["health"],
            militaryOnSameTile=u["position"] in military_tiles,
            nearbyHostiles=[
                NearbyThreat(
                    position=h["position"],
                    distance=h["distance"],
                    **{k: h["unit"][k] for k in ("name", "civilization", "health")},
                )
                for h in u["nearbyHostiles"]
            ],
        )
        (injured if u["military"] else civilians).append(risk)
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
    economy = snapshot.get("economy") or {}
    return TacticalReview(
        turn=snapshot["turn"],
        militaryCount=len(military),
        military=[
            MilitaryUnit(
                handle=u["handle"],
                name=u["name"],
                position=u["position"],
                health=u["health"],
                strength=u["combat"]["strength"],
                rangedStrength=u["combat"]["rangedStrength"],
                order=(u["combat"].get("order") or {}).get("action"),
                availableAttacks=len(u["combat"]["attacks"]),
            )
            for u in military
        ],
        visibleHostiles=threats,
        civiliansNearHostiles=civilians,
        injuredMilitaryNearHostiles=injured,
        cityBombardments=[
            Bombardment(
                city=c["name"],
                defender=a["defender"],
                minDamage=a["minDamageToDefender"],
                maxDamage=a["maxDamageToDefender"],
                maxRetaliation=a["maxDamageToAttacker"],
                command=a["command"],
            )
            for c in snapshot["cities"]
            for a in c["attacks"]
        ],
        recentLosses=losses,
        cityCount=len(snapshot["cities"]),
        citiesProducingSettlers=[c["name"] for c in snapshot["cities"] if "Settler" in c["constructionQueue"]],
        settlerUnits=[u["handle"] for u in units if u["name"] == "Settler"],
        treasury=economy.get("treasury"),
        goldPerTurn=economy.get("goldPerTurn"),
        happiness=economy.get("happiness"),
    )


def strategy_review(records: JsonObject) -> StrategyReview:
    plans = [
        PlanReview(
            id=key,
            subjects=r["body"].get("subjects", []),
            objective=r["body"]["objective"],
            needsReview=r["body"].get("needsReview", []),
        )
        for key, r in records.items()
        if r["category"] == "plan" and r["body"].get("state") == "active"
    ]
    covered = {s for p in plans for s in p.subjects}
    return StrategyReview(
        plans=plans, missingSubjects=[s for s in ("defense", "expansion", "economy") if s not in covered]
    )
