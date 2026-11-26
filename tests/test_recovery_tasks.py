"""Logical task identity binds paid facts, without copying old SDK inputs."""

import copy
from typing import Literal

import pytest
from pydantic import ValidationError

from civ_agent.orchestration.contracts import (
    CommunicationBatch,
    CommunicationRun,
    EventNotice,
    ModelSelection,
    PeacePhase,
    PeaceRequest,
    PlayerRole,
    Reference,
    ReviewRequest,
    RoleRequest,
    Scope,
    TurnTicket,
    digest,
)
from civ_agent.orchestration.correction_attempts import CorrectionAllowance
from civ_agent.orchestration.managed_checkpoints import NativeRunBinding
from civ_agent.orchestration.recovery_tasks import (
    BoundTaskBudget,
    PeacePhaseTask,
    RecoveryTaskInventory,
    ReviewTask,
    TaskBudget,
    TaskOwner,
    TurnRoleTask,
    bind_review_budget,
    bind_role_budget,
    merge_task_budgets,
    peace_task_budget,
    review_task_budget,
    role_task_budget,
)
from civ_agent.orchestration.store import JournalConflict, WorkflowStore

OWNER = TaskOwner(game="game", player="p0", civilization="Rome")
MODEL = ModelSelection(model="fixture")
REF = Reference(sha256=digest("reference without private content"))


def ticket(epoch="e1", *, turn=10, handoff=4, index=2):
    scope = Scope(game="game", player="p0", epoch=epoch, activation=digest(["game", "p0", epoch, handoff]))
    return TurnTicket(scope=scope, turn=turn, handoff=handoff, index=index)


def role(turn, purpose: PlayerRole = "gameplay", number=0, *, offset=0, request_limit=5):
    identifier = digest([turn.scope.activation, f"{purpose}:{number}"])
    return RoleRequest(
        scope=turn.scope.model_copy(update={"activation": identifier}),
        job=identifier,
        purpose=purpose,
        startup=REF,
        catalogue=REF,
        selection=MODEL,
        request_limit=request_limit,
        attempt_offset=offset,
    )


def review(turn, *, offset=0):
    source_turn = 0 if turn.turn == 0 else turn.turn - 1
    scope = turn.scope.model_copy(update={"activation": digest([turn.scope.model_dump(), "review", source_turn])})
    return ReviewRequest(
        scope=scope, inputs=REF, selection=MODEL, attempt_limit=3, request_limit=5, attempt_offset=offset
    )


def native_facts(request, used=1):
    kind = "role" if isinstance(request, RoleRequest) else "review"
    run = NativeRunBinding(scope=request.scope, kind=kind, run_id="native/opaque:first-run")
    allowance = CorrectionAllowance(
        scope=request.scope,
        kind=kind,
        purpose=request.purpose if kind == "role" else "review",
        workflow_run_id=run.run_id,
        attempt_limit=request.attempt_limit,
        attempt_offset=request.attempt_offset,
        used=used,
        evidence_sha256=digest("verified native lifecycle facts"),
    )
    return run, allowance


def role_source(turn, purpose: PlayerRole = "gameplay", number=0, *, ids=None, used=1, previous=None, request=None):
    request = request or role(turn, purpose, number)
    run, correction = native_facts(request, used)
    return role_task_budget(
        OWNER,
        request,
        sorted(ids or [digest("paid")]),
        ticket=turn,
        pass_number=number,
        run=run,
        correction=correction,
        previous=previous,
    )


def test_all_role_passes_and_nested_economy_calls_have_separate_budgets_and_actual_target_tickets():
    old = ticket()
    # roleJobs cannot represent this inventory; it retains one job per purpose.
    cases: list[tuple[PlayerRole, int]] = [
        ("gameplay", 0),
        ("gameplay", 1),
        ("economy", 0),
        ("economy", 1),
        ("economy", 2),
        ("military", 0),
        ("military", 1),
    ]
    sources = [role_source(old, purpose, number) for purpose, number in cases]
    inventory = merge_task_budgets(OWNER, "e1", sources)
    actual = ticket("e2", handoff=9, index=6)
    for source in sources:
        assert isinstance(source.task, TurnRoleTask)
        target = role(actual, source.task.purpose, source.task.pass_number)
        bound = bind_role_budget(inventory, target, ticket=actual, pass_number=source.task.pass_number)
        assert bound.generation.source == source.source and bound.generation.target == target.scope
        assert bound.request.attempt_offset == 1 and bound.request.request_limit == 5
        assert bound.inventory_sha256 == inventory.sha256 and bound.source_budget_sha256 == source.sha256
        assert BoundTaskBudget.model_validate_json(bound.model_dump_json()) == bound
        assert "native/opaque:first-run" not in bound.request.model_dump_json()
    assert len({source.source.activation for source in sources}) == 7


@pytest.mark.parametrize("kind", ["role", "review"])
def test_dormant_allowance_survives_repeated_rewind_without_an_intermediate_sdk_history(kind):
    old, actual = ticket(), ticket("e3", handoff=12)
    if kind == "role":
        source = role_source(old)
        target = role(actual)
    else:
        request = review(old)
        run, correction = native_facts(request, 2)
        source = review_task_budget(OWNER, old, request, [digest("paid")], run=run, correction=correction)
        target = review(actual)
    first = merge_task_budgets(OWNER, "e1", [source])
    dormant = merge_task_budgets(OWNER, "e2", [], previous=first)
    third = merge_task_budgets(OWNER, "e3", [], previous=dormant)
    assert dormant.current == third.current == []
    assert third.dormant[0].budget == source and third.dormant[0].budget_sha256 == source.sha256
    assert third.dormant[0].inventory_sha256 == dormant.sha256
    # Binding is to a different current target epoch, including dormant sources.
    actual = ticket("e4", handoff=15)
    target = role(actual) if kind == "role" else review(actual)
    if isinstance(target, RoleRequest):
        bound = bind_role_budget(third, target, ticket=actual, pass_number=0)
    else:
        bound = bind_review_budget(third, actual, target)
    assert bound.generation.source.epoch == "e1" and bound.request.attempt_offset == source.used_attempts
    damaged = third.model_dump(mode="json")
    damaged["dormant"][0]["budget"]["request_limit"] = 6
    with pytest.raises(ValidationError, match="DORMANT_TASK_CHANGED"):
        RecoveryTaskInventory.model_validate(damaged)


def test_unstarted_inherited_preparation_keeps_exact_generation_and_correction_floor():
    old, actual = ticket(), ticket("e2")
    source = role_source(old)
    first = merge_task_budgets(OWNER, "e1", [source])
    bound = bind_role_budget(first, role(actual), ticket=actual, pass_number=0)
    assert isinstance(bound.request, RoleRequest)
    prepared = role_task_budget(
        OWNER, bound.request, sorted(bound.generation.inherited_ids), ticket=actual, pass_number=0, previous=first
    )
    assert prepared.run is prepared.correction is None and prepared.used_attempts == 1
    second = merge_task_budgets(OWNER, "e2", [prepared], previous=first)
    later = ticket("e3")
    assert bind_role_budget(second, role(later), ticket=later, pass_number=0).request.attempt_offset == 1


def test_post_checkpoint_confirmed_and_uncertain_reservations_transfer_without_refund_or_double_usage(tmp_path):
    old, actual, later = ticket(), ticket("e2"), ticket("e3")
    request = role(old, request_limit=3)
    target = role(actual, request_limit=3)
    store = WorkflowStore(tmp_path / "store", player="p0")
    try:
        store.bind_epoch("game", "e1")
        store.reserve(request.scope, "at-checkpoint", {}, generation_limit=3)
        at_checkpoint = store.prepare_generation_transfer(request.scope, target.scope)
        store.complete(request.scope, "at-checkpoint", {}, {"usage": "old usage remains source-owned"})
        store.reserve(request.scope, "uncertain-after-checkpoint", {}, generation_limit=3)
        retirement = store.prepare_generation_transfer(request.scope, target.scope)
        assert len(at_checkpoint.generation_ids) == 1 and len(retirement.generation_ids) == 2
        source = role_source(old, request=request, ids=retirement.generation_ids)
        first = merge_task_budgets(OWNER, "e1", [source])
        bound = bind_role_budget(first, target, ticket=actual, pass_number=0)
        assert isinstance(bound.request, RoleRequest)
        assert bound.generation == retirement
        assert store.install_generation_transfer(bound.generation) == 2
        store.bind_epoch("game", "e2", previous="e1")
        assert store.reserve(target.scope, "last-new-generation", {}, generation_limit=3)[0]
        with pytest.raises(JournalConflict, match="MODEL_REQUEST_LIMIT"):
            store.reserve(target.scope, "no-refund", {}, generation_limit=3)
        fresh = store.prepare_generation_transfer(target.scope, role(later, request_limit=3).scope)
        run, correction = native_facts(bound.request, 2)
        second_source = role_task_budget(
            OWNER,
            bound.request,
            fresh.generation_ids,
            ticket=actual,
            pass_number=0,
            run=run,
            correction=correction,
            previous=first,
        )
        second = merge_task_budgets(OWNER, "e2", [second_source], previous=first)
        with pytest.raises(JournalConflict, match="CORRECTION_ATTEMPTS_EXHAUSTED"):
            bind_role_budget(second, role(later, request_limit=3), ticket=later, pass_number=0)
        assert store.receipt(target.scope, "at-checkpoint") is None
    finally:
        store.close()


def test_review_source_turn_preserves_actual_corrections_independent_of_legacy_review_rows():
    old, actual = ticket(), ticket("e2", handoff=100)
    request = review(old)
    run, correction = native_facts(request, 2)
    source = review_task_budget(OWNER, old, request, [digest("paid")], run=run, correction=correction)
    inventory = merge_task_budgets(OWNER, "e1", [source])
    bound = bind_review_budget(inventory, actual, review(actual))
    assert isinstance(bound.task, ReviewTask)
    assert bound.task.source_turn == 9 and bound.request.attempt_offset == 2 and bound.request.attempt_limit == 3
    different = ticket("e2", turn=11)
    with pytest.raises(JournalConflict, match="TASK_NOT_FOUND"):
        bind_review_budget(inventory, different, review(different))


def communication(epoch, *, body="same-native-event", baseline="same-permitted-context", event_id=1):
    scope = Scope(game="game", player="p0", epoch=epoch, activation="communication")
    run = CommunicationRun(scope=scope, selection=MODEL, request_limit=5, managed=True)
    notice = EventNotice(
        scope=scope,
        id=scope.identity("native-event", str(event_id)),
        native_id=event_id,
        content=Reference(sha256=digest(body)),
    )
    batch = CommunicationBatch(run=run, notices=[notice])
    identifier = digest([scope.model_dump(), [notice.model_dump(mode="json")]])
    request = RoleRequest(
        scope=scope.model_copy(update={"activation": identifier}),
        job=identifier,
        purpose="communication",
        startup=Reference(sha256=digest(baseline)),
        catalogue=REF,
        selection=MODEL,
        request_limit=5,
    )
    return batch, request


def test_communication_requires_exact_event_batch_while_fresh_context_keeps_allowance():
    batch, request = communication("e1")
    run, correction = native_facts(request)
    source = role_task_budget(OWNER, request, [digest("paid")], batch=batch, run=run, correction=correction)
    inventory = merge_task_budgets(OWNER, "e1", [source])
    same_batch, target = communication("e2")
    bound = bind_role_budget(inventory, target, batch=same_batch)
    assert isinstance(bound.request, RoleRequest)
    assert bound.request.attempt_offset == 1 and bound.request.job != request.job
    assert source.source == request.scope and source.run == run
    fresh_batch, fresh = communication("e2", baseline="fresh permitted context")
    refreshed = bind_role_budget(inventory, fresh, batch=fresh_batch)
    assert isinstance(refreshed.request, RoleRequest)
    assert refreshed.request.attempt_offset == 1 and refreshed.request.startup == fresh.startup
    for changed in ({"body": "changed-body-same-integer"}, {"event_id": 2}):
        fresh_batch, fresh = communication("e2", **changed)
        with pytest.raises(JournalConflict, match="TASK_NOT_FOUND"):
            bind_role_budget(inventory, fresh, batch=fresh_batch)


def peace(
    epoch="e1", *, phase: Literal["military", "gameplay"] = "military", inputs="permitted-exact-inputs", military=None
):
    basis = digest("same tactical facts and terms")
    scope = Scope(game="game", player="p0", epoch=epoch, activation=digest(["game", "p0", epoch, "peace", basis]))
    return PeacePhase(
        request=PeaceRequest(scope=scope, basis=basis, inputs=Reference(sha256=digest(inputs)), selection=MODEL),
        phase=phase,
        military=military,
    )


def test_peace_phase_provenance_remains_exact_while_native_task_allowance_does_not_reset():
    military, main = peace(), peace(phase="gameplay", military={"privateReport": "first"})
    sources = [
        peace_task_budget(
            OWNER,
            p,
            [digest(p.phase)],
            run=NativeRunBinding(scope=p.scope, kind="peace_phase", run_id="opaque"),
        )
        for p in [military, main]
    ]
    inventory = merge_task_budgets(OWNER, "e1", sources)
    phase = peace("e2")
    same = peace_task_budget(OWNER, phase, [])
    assert inventory.find(same.task) == sources[0]
    phase = peace("e2", inputs="different future input")
    changed_inputs = peace_task_budget(OWNER, phase, [])
    phase = peace("e2", phase="gameplay", military={"privateReport": "second"})
    changed_military = peace_task_budget(OWNER, phase, [])
    assert inventory.find(changed_inputs.task) == sources[0]
    assert inventory.find(changed_military.task) == sources[1]
    assert isinstance(changed_inputs.task, PeacePhaseTask) and isinstance(sources[0].task, PeacePhaseTask)
    assert isinstance(changed_military.task, PeacePhaseTask) and isinstance(sources[1].task, PeacePhaseTask)
    assert changed_inputs.task.inputs_sha256 != sources[0].task.inputs_sha256
    assert changed_military.task.military_sha256 != sources[1].task.military_sha256
    assert "privateReport" not in inventory.model_dump_json()
    assert "first" not in inventory.model_dump_json()
    assert sources[0].source != same.source  # Budget correspondence grants no old approval.


@pytest.mark.parametrize(
    "damage", ["owner", "purpose", "pass", "ticket", "scope", "run", "correction", "generation", "prepared", "limit"]
)
def test_source_binding_rejects_wrong_owner_task_scope_native_proof_and_budget_facts(damage):
    old = ticket()
    source = role_source(old)
    body = source.model_dump(mode="json")
    if damage == "owner":
        body["task"]["owner"]["player"] = "foreign"
    elif damage == "purpose":
        body["task"]["purpose"] = "economy"
    elif damage == "pass":
        body["task"]["pass_number"] = 1
    elif damage == "ticket":
        body["proof"]["ticket"]["scope"]["activation"] = "invented"
    elif damage == "scope":
        body["source"]["epoch"] = "e2"
    elif damage == "run":
        body["run"]["run_id"] = "other-run"
    elif damage == "correction":
        body["correction"]["purpose"] = "military"
    elif damage == "generation":
        body["generation_ids"] *= 2
    elif damage == "prepared":
        body["run"] = body["correction"] = None
    elif damage == "limit":
        body["attempt_limit"] = 3
    with pytest.raises((ValidationError, JournalConflict)):
        TaskBudget.model_validate(body)


def test_duplicate_or_ambiguous_turn_sources_and_changed_inherited_prefixes_reject():
    source = role_source(ticket())
    with pytest.raises(ValidationError, match="TASK_AMBIGUOUS"):
        merge_task_budgets(OWNER, "e1", [source, source])
    another_activation = role_source(ticket(handoff=5))
    with pytest.raises(ValidationError, match="TASK_AMBIGUOUS"):
        merge_task_budgets(OWNER, "e1", [source, another_activation])
    first = merge_task_budgets(OWNER, "e1", [source])
    actual = ticket("e2")
    bound = bind_role_budget(first, role(actual), ticket=actual, pass_number=0)
    assert isinstance(bound.request, RoleRequest)
    with pytest.raises(ValidationError, match="INHERITANCE_CONFLICT"):
        role_task_budget(OWNER, bound.request, [], ticket=actual, pass_number=0, previous=first)
    inherited = role_task_budget(
        OWNER, bound.request, sorted(bound.generation.inherited_ids), ticket=actual, pass_number=0, previous=first
    )
    damaged = inherited.model_copy(update={"inheritance": None, "attempt_offset": 0})
    with pytest.raises((ValidationError, JournalConflict)):
        merge_task_budgets(OWNER, "e2", [damaged], previous=first)


@pytest.mark.parametrize("allowance_policy", ["carry", "refresh"])
@pytest.mark.parametrize("damage", ["same_epoch", "purpose", "job", "limit", "offset", "turn", "owner", "ticket"])
def test_actual_target_binding_rejects_identity_limit_and_allowance_changes(damage, allowance_policy):
    first = merge_task_budgets(OWNER, "e1", [role_source(ticket())])
    actual = ticket("e2")
    target = role(actual)
    if damage == "same_epoch":
        actual, target = ticket(), role(ticket())
    elif damage == "purpose":
        target = target.model_copy(update={"purpose": "military"})
    elif damage == "job":
        target = target.model_copy(update={"job": "other"})
    elif damage == "limit":
        target = target.model_copy(update={"request_limit": 6})
    elif damage == "offset":
        target = target.model_copy(update={"attempt_offset": 2})
    elif damage == "turn":
        actual, target = ticket("e2", turn=11), role(ticket("e2", turn=11))
    elif damage == "owner":
        target = target.model_copy(update={"scope": target.scope.model_copy(update={"player": "foreign"})})
    elif damage == "ticket":
        actual = actual.model_copy(update={"scope": actual.scope.model_copy(update={"activation": "invented"})})
    with pytest.raises((ValidationError, JournalConflict)):
        bind_role_budget(first, target, ticket=actual, pass_number=0, allowance_policy=allowance_policy)


def test_exhaustion_rejects_before_admission_and_bound_wire_tampering_cannot_change_owner():
    old, actual = ticket(), ticket("e2")
    source = role_source(old, used=2)
    with pytest.raises(JournalConflict, match="CORRECTION_ATTEMPTS_EXHAUSTED"):
        bind_role_budget(merge_task_budgets(OWNER, "e1", [source]), role(actual), ticket=actual, pass_number=0)
    full = role_source(old, ids=sorted(digest(i) for i in range(5)))
    with pytest.raises(JournalConflict, match="MODEL_REQUEST_LIMIT"):
        bind_role_budget(merge_task_budgets(OWNER, "e1", [full]), role(actual), ticket=actual, pass_number=0)
    first = merge_task_budgets(OWNER, "e1", [role_source(old)])
    bound = bind_role_budget(first, role(actual), ticket=actual, pass_number=0)
    bad = copy.deepcopy(bound.model_dump(mode="json"))
    bad["generation"]["target"]["activation"] = "changed"
    with pytest.raises(ValidationError, match="TARGET_CONFLICT"):
        BoundTaskBudget.model_validate(bad)


@pytest.mark.parametrize("kind", ["role", "review"])
def test_explicit_restart_refreshes_exhausted_task_without_changing_source_proof_or_limits(kind):
    old, actual = ticket(), ticket("e2")
    original = role(old) if kind == "role" else review(old)
    paid = sorted(digest(["paid-before-restart", index]) for index in range(original.request_limit))
    run, correction = native_facts(original, original.attempt_limit)
    if isinstance(original, RoleRequest):
        source = role_task_budget(OWNER, original, paid, ticket=old, pass_number=0, run=run, correction=correction)
        target = role(actual).model_copy(update={"typed_authored_plans": True})
    else:
        source = review_task_budget(OWNER, old, original, paid, run=run, correction=correction)
        target = review(actual)

    def bind(policy: Literal["carry", "refresh"]):
        if isinstance(target, RoleRequest):
            return bind_role_budget(inventory, target, ticket=actual, pass_number=0, allowance_policy=policy)
        return bind_review_budget(inventory, actual, target, allowance_policy=policy)

    inventory = merge_task_budgets(OWNER, "e1", [source])
    frozen_source = inventory.model_dump_json()
    with pytest.raises(JournalConflict, match="CORRECTION_ATTEMPTS_EXHAUSTED"):
        bind("carry")
    refreshed = bind("refresh")
    assert refreshed == bind("refresh")
    assert refreshed.request.attempt_offset == 0
    assert refreshed.request.attempt_limit == original.attempt_limit
    assert refreshed.request.request_limit == original.request_limit
    assert refreshed.request.scope == target.scope and refreshed.proof != source.proof
    assert refreshed.generation.generation_ids == paid
    assert refreshed.generation.allowance_policy == "refresh" and not refreshed.generation.inherited_ids
    assert refreshed.inventory_sha256 == inventory.sha256 and refreshed.source_budget_sha256 == source.sha256
    assert inventory.model_dump_json() == frozen_source
    assert BoundTaskBudget.model_validate_json(refreshed.model_dump_json()) == refreshed
    if isinstance(refreshed.request, RoleRequest):
        assert refreshed.request.typed_authored_plans


def test_next_recovery_carries_only_current_allowance_unless_operator_refreshes_again():
    old, actual, later = ticket(), ticket("e2"), ticket("e3")
    original = role_source(old, used=2, ids=sorted(digest(["old", i]) for i in range(5)))
    first = merge_task_budgets(OWNER, "e1", [original])
    refreshed = bind_role_budget(first, role(actual), ticket=actual, pass_number=0, allowance_policy="refresh")
    assert isinstance(refreshed.request, RoleRequest)
    paid_since_restart = sorted(digest(["new", i]) for i in range(2))
    run, correction = native_facts(refreshed.request, 1)
    source = role_task_budget(
        OWNER,
        refreshed.request,
        paid_since_restart,
        ticket=actual,
        pass_number=0,
        run=run,
        correction=correction,
        previous=first,
        allowance_policy="refresh",
    )
    second = merge_task_budgets(OWNER, "e2", [source], previous=first, allowance_policy="refresh")
    assert source.inheritance is not None
    assert source.inheritance.generation.generation_ids == original.generation_ids
    assert source.inheritance.generation.allowance_policy == "refresh"
    carried = bind_role_budget(second, role(later), ticket=later, pass_number=0)
    assert carried.request.attempt_offset == 1 and carried.generation.generation_ids == paid_since_restart
    assert carried.generation.allowance_policy == "carry" and len(carried.generation.inherited_ids) == 2
    renewed = bind_role_budget(second, role(later), ticket=later, pass_number=0, allowance_policy="refresh")
    assert renewed.request.attempt_offset == 0 and renewed.generation.generation_ids == paid_since_restart
    assert not renewed.generation.inherited_ids
    assert first.find(original.task) == original and source.correction == correction


def test_unspent_restart_allowance_survives_dormant_task_and_later_carry_recovery():
    old = ticket()
    source = role_source(old, used=2, ids=sorted(digest(["old", i]) for i in range(5)))
    first = merge_task_budgets(OWNER, "e1", [source])
    refreshed = merge_task_budgets(OWNER, "e2", [], previous=first, allowance_policy="refresh")
    carried = merge_task_budgets(OWNER, "e3", [], previous=refreshed)
    assert len(carried.dormant) == 1 and carried.dormant[0].budget == source
    assert carried.dormant[0].allowance_policy == "refresh"
    actual = ticket("e4")
    bound = bind_role_budget(carried, role(actual), ticket=actual, pass_number=0)
    assert bound.request.attempt_offset == 0
    assert bound.generation.allowance_policy == "refresh" and not bound.generation.inherited_ids
    assert bound.generation.generation_ids == source.generation_ids
