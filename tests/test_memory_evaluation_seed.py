import json

import pytest

from civ_agent.civilization_state import CivilizationState
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
