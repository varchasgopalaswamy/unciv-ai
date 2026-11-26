"""Round capture drains tool writes, while model work can continue across rounds."""

import json

import pytest
from sqlalchemy import select

from civ_agent import memory_schema as tables
from civ_agent.civilization_state import StateConflict
from tests.domain_fixtures import create, setup


def note(text):
    return {"changes": [{"op": "create", "content": {"type": "note", "text": text}}]}


@pytest.mark.parametrize("complete_after_capture", [False, True])
def test_restore_keeps_captured_memory_and_receipts_and_marks_unfinished_reflection(tmp_path, complete_after_capture):
    memory = setup(tmp_path)
    memory.call("game", "end", "player_command", {"kind": "END_TURN"})
    request = memory.queue("game", "player_command", {"kind": "END_TURN"}, "end")
    memory.receipt(request["id"], {"kind": "END_TURN", "isYourTurn": False, "actionSummary": "Ended turn."})
    key = create(memory, {"type": "note", "text": "Committed before capture"})
    saved = tmp_path / "round.json"
    checksum = memory.backup(saved)
    memory.patch("game", [{"op": "amend", "id": key, "fields": {"text": "ABANDONED FUTURE"}}])
    memory.finish_job("game", complete_after_capture)
    # Restore is repeatable, including after a completion in the abandoned timeline.
    for _ in range(2):
        assert memory.restore(saved, checksum, "restored") == []
        with memory.transaction() as db:
            assert (
                db.execute(select(tables.jobs.c.status).where(tables.jobs.c.id == "game")).scalar_one() == "INTERRUPTED"
            )
        summary = memory.summary(4)
        assert len(summary["actions"]) == 1 and summary["actions"][0]["outcome"] == "confirmed"
        assert "ABANDONED FUTURE" not in json.dumps(summary)
        assert "Committed before capture" in json.dumps(summary)
    memory.pause(False)
    memory.start_job("next", "gameplay", {}, game_turn=5)
    actions = memory.history(4, job="next")["actions"]
    assert isinstance(actions, list)
    assert len(actions) == 1
    assert actions[0]["command"] == summary["actions"][0]["command"]
    assert actions[0]["receipt"] == summary["actions"][0]["receipt"]
    with pytest.raises(StateConflict):
        memory.patch("game", [{"op": "create", "content": {"type": "note", "text": "Late old write"}}])
    memory.export(tmp_path / "workspace")
    assert "ABANDONED FUTURE" not in (tmp_path / "workspace/turn_4/summary.json").read_text()
