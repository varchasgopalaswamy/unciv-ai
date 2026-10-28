import json

import pytest

from civ_agent.civilization_state import CivilizationState
from civ_agent.tls import private_directory
from civ_agent.war_state import WarStore
from tests.memory_evaluation_seed import seed_memory


def seed_file(tmp_path, **changes):
    source = tmp_path / "seed.json"
    source.write_text(
        json.dumps(
            {
                "civilization": "Rome",
                "records": [
                    {"turn": 2, "content": {"type": "strategy", "assessment": "Science looks promising"}},
                    {"turn": 8, "content": {"type": "handoff", "text": "Check a possible settlement"}},
                ],
                **changes,
            }
        )
    )
    return source


def test_isolated_seed_preserves_author_turns_and_next_turn_handoff(tmp_path):
    root = tmp_path / "match"
    source = seed_file(tmp_path)
    seed_memory(root, source, "Rome", "g1", 9)
    memory = CivilizationState(root / "agent-services/Rome/civilization", "Rome")
    try:
        snapshot = memory.start_job("real-turn", "gameplay", {}, game_turn=9)
        records = [snapshot["records"][key]["body"] for key in snapshot["contextIds"]]
        assert [r["updatedTurn"] for r in records] == [2, 8]
        assert [r["type"] for r in records] == ["strategy", "handoff"]
        with pytest.raises(ValueError, match="fresh private store"):
            seed_memory(root, source, "Rome", "g1", 9)
        assert json.loads((root / "memory-seed.json").read_text())["sha256"]
    finally:
        memory.engine.dispose()


@pytest.mark.parametrize(
    "changes,error",
    [
        ({"civilization": "France"}, "another civilization"),
        ({"records": [{"turn": 9, "content": {"type": "note", "text": "Future"}}]}, "precede"),
    ],
)
def test_seed_rejects_wrong_player_or_future_data_before_creating_store(tmp_path, changes, error):
    root = tmp_path / "match"
    with pytest.raises(ValueError, match=error):
        seed_memory(root, seed_file(tmp_path, **changes), "Rome", "g1", 9)
    assert not root.exists()


def campaign_seed(tmp_path):
    return seed_file(
        tmp_path,
        records=[],
        wars=[
            {
                "opponent": "France",
                "startedTurn": 4,
                "goals": [
                    {
                        "outcome": "Defend Rome",
                        "priority": 1,
                        "completion": "No invading troops threaten the capital",
                        "acceptableCosts": "Preserve the field army",
                        "reconsiderWhen": "The invading force withdraws or is defeated",
                    }
                ],
                "reason": "France invaded on turn four",
            }
        ],
        operations=[
            {
                "id": "capital-defense",
                "opponents": ["France"],
                "plan": "Repel the force approaching Rome",
                "progress": "No enemy units remain inside our borders",
                "reconsiderWhen": "The defense objective is complete",
                "reviewTurn": 9,
            }
        ],
    )


def campaign_position():
    return {
        "epoch": "prepared-epoch",
        "turn": 9,
        "civilizationProfile": {"name": "Rome"},
        "diplomacy": {"knownCivilizations": [{"civilizationId": "France", "atWar": True}]},
    }


def test_seeded_war_uses_current_epoch_and_accepts_normal_goal_negotiation(tmp_path):
    root = tmp_path / "match"
    seed_memory(root, campaign_seed(tmp_path), "Rome", "active-epoch", 9, position=campaign_position())
    # The subsequent actual service launch validates these existing directories.
    private_directory(root / "agent-services")
    private_directory(root / "agent-services/Rome")
    memory = CivilizationState(root / "agent-services/Rome/civilization", "Rome")
    try:
        store = WarStore(memory)
        episode = next(iter(store.state()["episodes"].values()))
        assert episode["active"] and episode["startedTurn"] == 4 and episode["version"] == 1
        assert episode["goals"][0]["outcome"] == "Defend Rome"
        assert store.state()["operations"]["capital-defense"]["reviewTurn"] == 9
        store.synchronize("active-epoch", campaign_position())
        assert len(store.state()["episodes"]) == 1
        with pytest.raises(ValueError, match="STALE_EPOCH"):
            store.synchronize("prepared-epoch", campaign_position())
        goals = [{**episode["goals"][0], "outcome": "Advance toward the opposing city"}]
        proposed = store.apply(
            "active-epoch",
            "military",
            "war_goals",
            {"action": "propose", "opponent": "France", "version": 1, "goals": goals, "reason": "Defense complete"},
            "proposal",
        )["proposal"]
        discussion = {"opponent": "France", "version": 1, "proposal": proposed["id"]}
        store.apply(
            "active-epoch",
            "gameplay",
            "war_goals",
            {"action": "respond", **discussion, "agree": True, "reason": "New objective accepted"},
            "response",
        )
        finalized = store.apply(
            "active-epoch",
            "military",
            "war_goals",
            {"action": "finalize", **discussion},
            "finalize",
        )["episode"]
        assert finalized["version"] == 2 and finalized["goals"] == goals
    finally:
        memory.engine.dispose()


@pytest.mark.parametrize(
    "change,error", [("peace", "native opponent"), ("future", "precede"), ("player", "another civilization")]
)
def test_seed_rejects_false_war_or_future_history_without_creating_memory(tmp_path, change, error):
    root = tmp_path / "match"
    source = campaign_seed(tmp_path)
    position = campaign_position()
    if change == "peace":
        position["diplomacy"]["knownCivilizations"][0]["atWar"] = False
    elif change == "future":
        seed = json.loads(source.read_text())
        seed["wars"][0]["startedTurn"] = 9
        source.write_text(json.dumps(seed))
    else:
        position["civilizationProfile"]["name"] = "Greece"
    with pytest.raises(ValueError, match=error):
        seed_memory(root, source, "Rome", "g1", 9, position=position)
    assert not root.exists()
