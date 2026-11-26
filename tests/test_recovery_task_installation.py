"""Target allowance installation uses real stores and explicit synthetic permits."""

from civ_agent.orchestration.contracts import RoleRequest, Scope, TurnTicket, digest
from civ_agent.orchestration.recovery_tasks import (
    CommunicationTask,
    PeacePhaseTask,
    TaskOwner,
    task_identity_sha256,
)


def target(p, source: RoleRequest, *, turn=None, pass_number=0):
    epoch = p.command.target_epoch
    original = p.source.ticket
    handoff = original.handoff + (30 if turn is None else 31)
    activation = digest(["game", "p0", epoch, handoff])
    ticket = TurnTicket(
        scope=Scope(game="game", player="p0", epoch=epoch, activation=activation),
        turn=original.turn if turn is None else turn,
        handoff=handoff,
        index=original.index + 50,
    )
    job = digest([activation, f"{source.purpose}:{pass_number}"])
    scope = ticket.scope.model_copy(update={"activation": job})
    request = source.model_copy(
        update={
            "scope": scope,
            "job": job,
            "startup": p.source.store.put(scope, {"fresh": "new target reads"}),
            "catalogue": p.source.store.put(scope, [{"name": "new target catalogue"}]),
            "attempt_offset": 0,
        }
    )
    return ticket, request


def test_native_communication_identity_is_exact_ordered_full_batch_and_never_context():
    owner = TaskOwner(game="game", player="p0", civilization="Rome")
    original = CommunicationTask(
        owner=owner, batch_sha256=digest([[1, "content1"], [2, "content2"]]), inputs_sha256=digest("old-context")
    )
    changed_context = original.model_copy(update={"inputs_sha256": digest("new-context")})
    assert task_identity_sha256(original) == task_identity_sha256(changed_context)
    for batch in ([[1, "content1"]], [[2, "content2"], [1, "content1"]], [[1, "different"], [2, "content2"]]):
        changed = original.model_copy(update={"batch_sha256": digest(batch)})
        assert task_identity_sha256(original) != task_identity_sha256(changed)


def test_native_peace_identity_retains_provenance_without_using_it_to_refund_allowances():
    owner = TaskOwner(game="game", player="p0", civilization="Rome")
    original = PeacePhaseTask(
        owner=owner,
        basis=digest("facts-and-exact-terms"),
        phase="gameplay",
        inputs_sha256=digest("original-private-inputs"),
        military_sha256=digest("original-report"),
    )
    refreshed = original.model_copy(
        update={"inputs_sha256": digest("new-private-inputs"), "military_sha256": digest("new-valid-report")}
    )
    assert original != refreshed and task_identity_sha256(original) == task_identity_sha256(refreshed)
    assert task_identity_sha256(original) != task_identity_sha256(
        original.model_copy(update={"basis": digest("different-terms")})
    )
