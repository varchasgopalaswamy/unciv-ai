import copy
import json

from civ_agent.civilization_state import CivilizationState
from civ_agent.observations import overview
from civ_agent.progress_history import HISTORY_TURNS, advance_history, progress_context
from civ_agent.protocol import JsonObject
from tests.test_decision_context import act, setup


def position(turn) -> JsonObject:
    return {
        "turn": turn,
        "economy": {
            "happiness": -6,
            "unitMaintenance": 12,
            "resources": [{"name": "Dyes", "available": 1, "imported": 1}],
        },
        "map": {
            "tiles": [
                {
                    "position": {"x": 1, "y": 2},
                    "resource": "Horses",
                    "owner": None,
                    "visible": True,
                    "observedTurn": turn,
                }
            ],
            "units": [{"handle": "scout", "name": "Scout", "position": {"x": 1, "y": 6}, "combat": {"order": None}}],
        },
        "cities": [
            {
                "handle": "city",
                "name": "Ecbatana",
                "constructionQueue": ["Science", "Library"],
                "development": {
                    "foodPerTurn": 1,
                    "constructions": [{"name": "Library", "index": 1, "perpetual": False, "productionStored": 26}],
                },
            }
        ],
    }


def test_stalled_scout_and_delayed_library_show_observations_beside_orders():
    history, actions = [], []
    snapshot = position(100)
    for turn in range(100, 121):
        snapshot = position(turn)
        history = advance_history(history, snapshot)
        assert advance_history(history, snapshot) == history
        if turn < 117:
            actions.append(
                {
                    "turn": turn,
                    "commandStatus": "confirmed",
                    "receipt": f"receipt{turn}",
                    "command": {"kind": "SET_UNIT_ORDER", "target": "scout", "selection": "EXPLORE"},
                }
            )
    result = progress_context(history, actions)
    assert result["exploration"]["change"] == 0
    assert result["repeatedCommands"][0]["count"] == 17
    scout = result["units"][0]
    assert scout["order"] is None and scout["samePositionSinceObservedTurn"] == 100
    assert scout["samePositionObservedTurns"] == 21
    assert result["cities"][0]["retainedProduction"] == [
        {
            "construction": "Library",
            "fromTurn": 100,
            "before": 26,
            "now": 26,
            "change": 0,
            "sameProductionSinceObservedTurn": 100,
            "sameProductionObservedTurns": 21,
            "activeNow": False,
        }
    ]
    moved = copy.deepcopy(snapshot)
    moved["map"]["units"][0]["position"]["x"] = 2
    history = advance_history(advance_history(history, moved), snapshot)
    assert progress_context(history, actions)["units"][0]["samePositionObservedTurns"] == 1
    snapshot["map"]["units"] = []
    result = progress_context(advance_history(history, snapshot), actions)
    assert result["unitsNoLongerOwned"] == [
        {
            "unit": "scout",
            "name": "Scout",
            "lastKnownPosition": {"x": 1, "y": 6},
            "lastObservedTurn": 119,
        }
    ]


def test_happiness_gaps_imports_and_last_observed_resource_ownership_are_factual():
    history = []
    for turn in (125, 126, 127, 129):
        view = position(turn)
        if turn == 126:
            view["map"]["tiles"][0]["visible"] = False
        if turn >= 127:
            view["map"]["tiles"][0]["owner"] = "Sydney"
            view["economy"]["resources"][0].update(available=0, imported=0)
        history = advance_history(history, view)
    result = progress_context(history, [])
    assert result["happiness"]["consecutiveObservedUnhappyTurns"] == 1
    site = next(f for f in result["changedFacts"] if f["kind"] == "resourceSites")
    assert site["before"]["owner"] is None and site["before"]["observedTurn"] == 125
    assert site["now"]["owner"] == "Sydney"
    assert any(f["subject"] == "Dyes" and f["now"]["imported"] == 0 for f in result["changedFacts"])
    for turn in range(130, 180):
        history = advance_history(history, position(turn))
    assert len(history) == HISTORY_TURNS + 1 and history[0]["turn"] == 159


def test_new_city_and_new_construction_get_their_own_observed_baselines():
    history = []
    for turn in range(100, 121):
        view = position(turn)
        if turn < 103:
            view["cities"] = []
        elif turn < 105:
            view["cities"][0]["constructionQueue"] = ["Nothing"]
            view["cities"][0]["development"]["constructions"] = []
        elif turn < 116:
            view["cities"][0]["constructionQueue"] = ["Library"]
            view["cities"][0]["development"]["constructions"][0]["productionStored"] = turn - 105
        history = advance_history(history, view)
    city = progress_context(history, [])["cities"][0]
    assert city["fromTurn"] == 103
    assert city["queueBefore"] == ["Nothing"]
    assert city["queueNow"] == ["Science", "Library"]
    assert city["retainedProduction"] == [
        {
            "construction": "Library",
            "fromTurn": 105,
            "before": 0,
            "now": 26,
            "change": 26,
            "sameProductionSinceObservedTurn": 116,
            "sameProductionObservedTurns": 5,
            "activeNow": False,
        }
    ]


def test_history_and_spending_survive_restart_and_rewind_without_retry_double_count(tmp_path):
    memory, view, key = setup(tmp_path)
    for turn in range(4, 8):
        view["turn"] = turn
        memory.observe_player(view, "game", overview(view))
        request = memory.queue(
            "game",
            "player_command",
            {"kind": "UPGRADE_UNIT", "target": "unit1", "selection": "Chu-Ko-Nu"},
            f"upgrade-{turn}",
            intent={"reason": "Investment", "strategy": key, "decision": "capacity"},
        )
        receipt = {"actionSummary": "Unit upgraded", "economy": {"goldChange": -170, "units": []}}
        memory.receipt(request["id"], receipt)
        memory.receipt(request["id"], receipt)
    choice = memory.decision_feedback(view)["choices"][0]
    assert choice["investment"]["goldSpent"] == 680
    assert choice["investment"]["confirmedActionsByKind"] == {"UPGRADE_UNIT": 4}
    baseline = memory.progress_feedback(view)
    assert baseline["unlinkedInvestment"]["goldSpent"] == 0
    saved = tmp_path / "checkpoint.json"
    checksum = memory.backup(saved)
    future = copy.deepcopy(view)
    future["turn"] = 15
    future["cities"][0]["name"] = "DISCARDED FUTURE"
    memory.observe_player(future, "game", overview(future))
    act(memory, key, "future")
    memory.engine.dispose()
    memory = CivilizationState(memory.root, "Rome")
    assert "DISCARDED" in json.dumps(memory.progress_feedback(future))
    memory.restore(saved, checksum, "g2")
    memory.pause(False)
    assert memory.progress_feedback(view) == baseline
    assert memory.decision_feedback(view)["choices"][0]["investment"]["goldSpent"] == 680
    memory.engine.dispose()


def test_turn_opening_context_stays_stable_while_history_records_current_results(tmp_path):
    memory, view, _ = setup(tmp_path)
    opening = overview(view)
    memory.observe_player(view, "game", opening)
    before = copy.deepcopy(opening["recentProgress"])
    view.setdefault("economy", {})["happiness"] = -4
    changed = overview(view)
    memory.observe_player(view, "game", changed)
    assert changed["recentProgress"] == before
    assert memory.progress_feedback(view)["happiness"]["now"] == -4
    view["turn"] += 1
    following = overview(view)
    memory.observe_player(view, "game", following)
    assert following["recentProgress"]["happiness"]["now"] == -4
    assert following["recentProgress"]["toTurn"] == view["turn"]
    memory.engine.dispose()
