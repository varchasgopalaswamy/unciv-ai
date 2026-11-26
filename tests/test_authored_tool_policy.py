"""Frozen tool-policy inputs preserve old native requests and paid allowances."""

import asyncio
import hashlib

import pytest

from civ_agent.orchestration.contracts import PrepareRole, RoleRequest, digest
from civ_agent.orchestration.recovery_tasks import bind_role_budget, merge_task_budgets
from civ_agent.orchestration.store import JournalConflict
from tests.test_native_player_stores import production_stores
from tests.test_recovery_tasks import OWNER, role, role_source, ticket


def test_missing_and_explicit_legacy_policy_preserve_exact_request_bytes():
    original = role(ticket(), purpose="economy")
    old_wire = original.model_dump(mode="json")
    assert "typed_authored_plans" not in old_wire
    missing = RoleRequest.model_validate(old_wire)
    explicit = RoleRequest.model_validate({**old_wire, "typed_authored_plans": False})
    assert missing == explicit == original
    assert missing.model_dump_json() == explicit.model_dump_json()
    # The complete legacy request includes its original default fields/order.
    assert hashlib.sha256(missing.model_dump_json().encode()).hexdigest() == (
        "e2ada07c05f870bece163f297fd73030fe47a42234b305864604dcf4a81d2e4d"
    )
    assert missing.model_dump_json() == explicit.model_dump_json()
    restored = RoleRequest.model_validate_json(missing.model_dump_json())
    assert restored == missing and not restored.typed_authored_plans


def test_new_policy_is_part_of_the_exact_native_workflow_input():
    legacy = role(ticket(), purpose="economy")
    selected = legacy.model_copy(update={"typed_authored_plans": True})
    payload = selected.model_dump_json()
    restored = RoleRequest.model_validate_json(payload)
    assert restored == selected and restored.typed_authored_plans
    assert restored.model_dump(mode="json")["typed_authored_plans"] is True
    assert digest(restored.model_dump(mode="json")) != digest(legacy.model_dump(mode="json"))
    assert restored.model_dump_json() == payload


@pytest.mark.parametrize(
    ("used", "generations", "failure"),
    [(1, 2, None), (2, 2, "CORRECTION_ATTEMPTS_EXHAUSTED"), (1, 5, "MODEL_REQUEST_LIMIT")],
    ids=["remaining-allowance", "corrections-exhausted", "generations-exhausted"],
)
def test_new_policy_cannot_refund_a_legacy_tasks_paid_allowances(used, generations, failure):
    source_ticket, target_ticket = ticket(), ticket("e2", handoff=9, index=6)
    legacy = role(source_ticket, purpose="economy")
    paid = sorted(digest(["paid-generation", index]) for index in range(generations))
    source = role_source(source_ticket, purpose="economy", request=legacy, ids=paid, used=used)
    inventory = merge_task_budgets(OWNER, "e1", [source])
    saved_inventory = inventory.model_dump_json()
    replacement = role(target_ticket, purpose="economy").model_copy(update={"typed_authored_plans": True})
    if failure is not None:
        with pytest.raises(JournalConflict, match=failure):
            bind_role_budget(inventory, replacement, ticket=target_ticket, pass_number=0)
    else:
        bound = bind_role_budget(inventory, replacement, ticket=target_ticket, pass_number=0)
        assert isinstance(bound.request, RoleRequest) and bound.request.typed_authored_plans
        assert bound.request.attempt_offset == used
        assert bound.request.request_limit == legacy.request_limit == 5
        assert bound.request.attempt_limit == legacy.attempt_limit == 2
        assert bound.generation.generation_ids == paid
        assert bound.generation.source == legacy.scope and bound.generation.target == replacement.scope
        assert bound.source_budget_sha256 == source.sha256
        assert source.request_sha256 == digest(legacy.model_dump(mode="json"))
    assert inventory.model_dump_json() == saved_inventory
    assert not legacy.typed_authored_plans


@pytest.mark.parametrize("retained", ["request", "bootstrap"])
def test_coordinator_keeps_a_retained_legacy_request_policy(tmp_path, retained):
    async def run():
        async with production_stores(tmp_path) as prepared:
            command = PrepareRole(ticket=prepared.ticket, purpose="gameplay", pass_number=0)
            created = await prepared.root.prepare_role(command)
            assert created is not None and created.typed_authored_plans
            # Stand in for an already saved pre-policy preparation. No model or
            # tool has run, and its captured gateway state/catalogue stay exact.
            legacy = created.model_copy(update={"typed_authored_plans": False})
            wire = legacy.model_dump(mode="json")
            record = prepared.root.record
            assert record is not None
            record["bootstraps"]["gameplay:0"]["request"] = wire
            if retained == "request":
                record["requests"]["gameplay:0"] = wire
            else:
                record["requests"].pop("gameplay:0")
            restored = await prepared.root.prepare_role(command)
            assert restored == legacy and not restored.typed_authored_plans
            assert restored.model_dump_json() == legacy.model_dump_json()
            assert record["requests"]["gameplay:0"] == wire
            assert "typed_authored_plans" not in record["requests"]["gameplay:0"]

    asyncio.run(run())
