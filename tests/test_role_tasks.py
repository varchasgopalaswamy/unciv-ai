"""Native role input carries its actual trigger and exact current pending facts."""

import pytest
from pydantic import ValidationError

from civ_agent.orchestration.contracts import Scope, TurnTicket
from civ_agent.orchestration.role_tasks import NativeRoleTask, build_role_task


def ticket():
    return TurnTicket(
        scope=Scope(game="game", player="p0", epoch="epoch", activation="turn"), turn=7, handoff=9, index=0
    )


def test_initial_task_preserves_unknown_native_state_without_inventing_a_blocker():
    task = build_role_task(ticket(), "gameplay", 0, {})
    assert task.reason == "turn" and task.message is None
    assert task.pendingChoices == [] and task.canEndTurn is None and task.canDeferChoices is None
    assert task.turn == 7 and task.handoff == 9 and task.passNumber == 0
    assert NativeRoleTask.model_validate_json(task.model_dump_json()) == task


def test_reopened_task_carries_reason_current_native_choices_and_actual_pass():
    pending = [{"kind": "Construction", "supported": True, "details": {"city": "city1"}}]
    view = {"pending": pending, "canEndTurn": False, "turnControls": {"canDeferChoices": True}}
    task = build_role_task(
        ticket(),
        "gameplay",
        2,
        view,
        message="Choose production, ask Economy, or explicitly defer when permitted.",
        handoff_review={"role": "gameplay", "pending": pending},
    )
    assert task.reason == "required_choices" and task.passNumber == 2
    assert task.pendingChoices == pending and task.canEndTurn is False and task.canDeferChoices is True
    assert task.message and "production" in task.message
    pending[0]["details"]["city"] = "changed"
    assert task.pendingChoices[0]["details"]["city"] == "city1"


@pytest.mark.parametrize(
    "review",
    [
        {"role": "economy", "pending": [{"kind": "Research", "supported": True}]},
        {"role": "gameplay", "pending": [{"kind": "Construction", "supported": True}]},
    ],
)
def test_old_or_other_role_review_does_not_relabel_current_attention(review):
    task = build_role_task(
        ticket(),
        "gameplay",
        1,
        {"pending": [{"kind": "Research", "supported": True}]},
        message="The position changed; reconsider.",
        handoff_review=review,
    )
    assert task.reason == "role_attention"
    assert task.pendingChoices == [{"kind": "Research", "supported": True}]


def test_host_task_rejects_a_malformed_pending_shape():
    with pytest.raises(ValidationError):
        build_role_task(ticket(), "gameplay", 0, {"pending": ["Construction"]})
