import copy

from civ_agent.mechanics_overview import exploration_summary, settlement_summary
from civ_agent.observations import DetailQuery, overview, project_detail
from civ_agent.protocol import JsonObject
from civ_agent.state_updates import apply_overview_update, overview_update, stamp
from tests.test_tactical_review import reviewed


def test_mechanics_are_preloaded_with_focused_details_and_stable_updates():
    snapshot = reviewed()
    names = [f"Technology {n}" for n in range(14)]
    snapshot["researchQueue"] = names[:2]
    snapshot["researchOptions"] = names
    snapshot["research"] = {
        "current": names[0],
        "sciencePerTurn": "23",
        "overflowScience": 4,
        "technologies": [
            {
                "name": name,
                "researched": False,
                "scienceStored": 5,
                "cost": 100,
                "turns": "5",
                "unlocks": {"units": ["Swordsman"]},
            }
            for name in names
        ],
    }
    snapshot["culture"] = {
        "storedCulture": 30,
        "culturePerTurn": 5,
        "nextPolicyCost": 50,
        "turnsToNextPolicy": 4,
        "branches": [{"name": "Tradition", "remaining": ["Legalism"]}],
    }
    snapshot["greatPeople"] = [
        {"name": "Great Scientist", "points": 45, "pointsPerTurn": 3, "threshold": 100, "turnsAtCurrentRate": 19}
    ]
    city = snapshot["cities"][0]
    city.setdefault("development", {}).update(
        yields=[{"stat": "Science", "amount": "12"}], focus="Food", avoidGrowth=False, unassignedPopulation=1
    )
    city.setdefault("management", {}).update(
        manualSpecialists=True,
        specialists=[
            {
                "name": "Scientist",
                "assigned": 1,
                "slots": 2,
                "yields": [{"stat": "Science", "amount": "3"}],
                "greatPersonPoints": {"Great Scientist": 3},
            }
        ],
        greatPeople=[{"name": "Great Scientist", "pointsPerTurn": 3}],
    )
    snapshot.setdefault("economy", {})["resources"] = [
        {
            "name": "Iron",
            "type": "Strategic",
            "available": -1,
            "used": 3,
            "imported": 0,
            "exported": 0,
            "offered": 0,
            "breakdown": [{"source": "Private detail", "amount": 2}],
        }
    ]
    snapshot.setdefault("diplomacy", {})["cityStates"] = [
        {
            "civilizationId": "Geneva",
            "bonuses": [{"relationship": "Friend", "description": "+3 Culture", "active": False}],
            "resources": [{"name": "Spices", "amount": 1}],
            "quests": [],
            "allyInfluence": 70,
        }
    ]
    saved = copy.deepcopy(snapshot)
    compact = overview(snapshot)
    assert len(compact["research"]["technologies"]) == 8
    assert compact["research"]["otherCandidateCount"] == 6
    assert compact["culture"] == snapshot["culture"] and compact["greatPeople"] == snapshot["greatPeople"]
    assert compact["cities"][0]["yields"] == {"Science": "12"}
    assert compact["cities"][0]["citizens"]["unassignedPopulation"] == 1
    assert compact["cities"][0]["citizens"]["specialists"][0]["slots"] == 2
    assert compact["economy"]["resources"][0]["available"] == -1
    assert "breakdown" not in compact["economy"]["resources"][0]
    assert compact["cityStates"][0]["bonuses"] == snapshot["diplomacy"]["cityStates"][0]["bonuses"]
    query = DetailQuery(tool="player_details", arguments={"section": "research"})
    assert len(project_detail(snapshot, query)["data"]["research"]["technologies"]) == 14
    before = stamp(compact, "job", "epoch", "1")
    snapshot["map"]["units"][0]["health"] -= 1
    after = stamp(overview(snapshot), "job", "epoch", "2")
    update = overview_update(before, after, [])
    assert not {"research", "culture", "greatPeople", "economy", "settlement", "exploration"} & update["set"].keys()
    assert apply_overview_update(before, update) == after
    snapshot["map"]["units"][0]["health"] += 1
    assert snapshot == saved


def test_exploration_uses_known_approaches_and_native_wrap_distances():
    snapshot = {
        "map": {
            "wrapOffset": {"x": 10, "y": 5},
            "tiles": [{"position": {"x": 0, "y": 0}, "terrain": "Coast"}],
            "frontier": [{"x": 9, "y": 4}],
            "units": [{"handle": "Scout", "position": {"x": 0, "y": 0}}],
        }
    }
    result = exploration_summary(snapshot)
    assert result["regions"] == [
        {
            "approach": {"x": 0, "y": 0},
            "terrain": "Coast",
            "frontierTiles": 1,
            "nearbyUnits": [{"unit": "Scout", "distance": 0}],
        }
    ]
    snapshot["map"]["frontier"] = []
    assert exploration_summary(snapshot)["regions"] == []


def test_expansion_groups_identical_budgets_and_counts_luxury_types_once():
    candidates: list[JsonObject] = [
        {
            "position": {"x": n * 4, "y": 0},
            "resources": [
                {"name": "Dyes", "type": "Luxury", "owner": None, "happinessIfConnected": 4},
                {"name": "Dyes", "type": "Luxury", "owner": None, "happinessIfConnected": 4},
                {"name": "Spices", "type": "Luxury", "owner": "Greece", "happinessIfConnected": 4},
                {"name": "Silver", "type": "Luxury", "owner": "Rome", "happinessIfConnected": 4},
                {"name": "Silk", "type": "Luxury", "owner": None, "happinessIfConnected": 0},
                {"name": "Wheat", "type": "Bonus", "owner": None},
            ],
            "happiness": {
                "populationCosts": [{"population": 1, "cityUnhappiness": 3, "populationUnhappiness": 1}],
                "buildings": [],
                "basis": "Costs before benefits",
            },
        }
        for n in range(5)
    ]
    source = {"candidates": candidates, "recommendations": [c["position"] for c in candidates[:3]]}
    for candidate in candidates:
        for i, resource in enumerate(candidate["resources"]):
            resource.update(
                ownership={None: "UNCLAIMED", "Rome": "OWN", "Greece": "FOREIGN"}[resource["owner"]],
                withinExistingCityRange=["city1"] if resource["owner"] == "Rome" else [],
                position={"x": i, "y": 0},
                observedTurn=2,
            )
    saved = copy.deepcopy(source)
    result = settlement_summary(source)
    assert len(result["candidates"]) == 3 and len(result["newCityHappiness"]) == 1
    assert all(c["unclaimedLuxuryHappinessIfConnected"] == 4 for c in result["candidates"])
    assert len(result["newCityHappiness"][0]["sites"]) == 3
    grouped = result["candidates"][0]["resources"]
    assert len(grouped["unclaimedPotential"][0]["tiles"]) == 2
    assert grouped["foreignOwned"][0]["resource"]["name"] == "Spices"
    assert grouped["existingCityOverlap"][0]["resource"]["name"] == "Silver"
    assert source == saved


def test_existing_city_happiness_uses_native_estimates_and_preserves_blockers():
    snapshot = reviewed()
    city = snapshot["cities"][0]

    def estimate(gain, reasons):
        return {
            "production": {"turnsIfStartedNow": 7},
            "queueUnavailableReasons": reasons,
            "completion": {"yields": {"change": {"Happiness": str(gain)}}, "maintenanceChange": "1"},
        }

    city["constructionEstimates"] = {"Colosseum": estimate(2, []), "Theatre": estimate(3, ["Requires Printing Press"])}
    city["constructionQueue"] = ["Colosseum"]
    city["constructionEstimates"]["Colosseum"]["whileBuilding"] = {"change": {}}
    result = overview(snapshot)["economy"]["happinessBudget"]["existingCityBuildings"]
    assert result == [
        {
            "city": city["handle"],
            "options": [
                {
                    "name": "Colosseum",
                    "happinessChange": "2",
                    "maintenanceChange": "1",
                    "turnsIfStartedNow": 7,
                    "queued": True,
                }
            ],
        }
    ]
