"""A replacement epoch keeps every completed or uncertain model reservation."""

import json

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, select, update

from civ_agent.orchestration.contracts import Scope, canonical
from civ_agent.orchestration.recovery_budgets import GenerationBudgetTransfer
from civ_agent.orchestration.store import JournalConflict, WorkflowStore, artifacts, generation_attempts


def setup(tmp_path):
    store = WorkflowStore(tmp_path / "worker", player="p0")
    store.bind_epoch("game", "e1")
    old = Scope(game="game", player="p0", epoch="e1", activation="first-role")
    return store, old, old.model_copy(update={"epoch": "e2", "activation": "replacement-role"})


def test_repeated_rewind_carries_prior_inherited_debits_and_additional_spend(tmp_path):
    store, old, target = setup(tmp_path)
    try:
        store.reserve(old, "confirmed", {}, generation_limit=3)
        store.complete(old, "confirmed", {}, {"confirmed": True})
        store.reserve(old, "uncertain", {}, generation_limit=3)
        first = store.prepare_generation_transfer(old, target)
        store.install_generation_transfer(first)
        store.bind_epoch("game", "e2", previous="e1")
        store.reserve(target, "additional-uncertain", {}, generation_limit=3)
        next_target = target.model_copy(update={"epoch": "e3", "activation": "third-role"})
        second = store.prepare_generation_transfer(target, next_target)
        assert len(second.generation_ids) == 3
        assert store.install_generation_transfer(second) == 3
        store.bind_epoch("game", "e3", previous="e2")
        with pytest.raises(JournalConflict, match="MODEL_REQUEST_LIMIT"):
            store.reserve(next_target, "cannot-reset", {}, generation_limit=3)
        with pytest.raises(JournalConflict, match="EPOCH_CONFLICT"):
            store.install_generation_transfer(first)
        assert store.receipt(next_target, "confirmed") is None
    finally:
        store.close()


def test_changed_source_refuses_install_before_target_receives_any_debit(tmp_path):
    store, old, target = setup(tmp_path)
    try:
        store.reserve(old, "first", {}, generation_limit=3)
        frozen = store.prepare_generation_transfer(old, target)
        store.reserve(old, "late-reservation", {}, generation_limit=3)
        with pytest.raises(JournalConflict, match="SOURCE_CHANGED"):
            store.install_generation_transfer(frozen)
        with store.engine.begin() as db:
            assert not list(
                db.scalars(
                    select(generation_attempts.c.id).where(
                        generation_attempts.c.scope == canonical(target.model_dump())
                    )
                )
            )
    finally:
        store.close()


@pytest.mark.parametrize("damage", ["game", "player", "same_epoch", "duplicate", "omitted", "unknown", "wrong_store"])
def test_frozen_transfer_rejects_foreign_or_changed_budget_identity(tmp_path, damage):
    store, old, target = setup(tmp_path)
    other = None
    try:
        store.reserve(old, "first", {}, generation_limit=3)
        frozen = store.prepare_generation_transfer(old, target)
        body = frozen.model_dump(mode="json")
        if damage in {"game", "player"}:
            body["target"][damage] = "foreign"
        elif damage == "same_epoch":
            body["target"]["epoch"] = old.epoch
        elif damage == "duplicate":
            body["generation_ids"] *= 2
        elif damage == "omitted":
            body["generation_ids"] = []
        elif damage == "unknown":
            body["generation_ids"] = ["0" * 64]
        else:
            other = WorkflowStore(tmp_path / "foreign-worker", player="p1")
            other.bind_epoch("game", "e1")
        with pytest.raises((ValidationError, JournalConflict)):
            selected = GenerationBudgetTransfer.model_validate_json(json.dumps(body))
            (other or store).install_generation_transfer(selected)
    finally:
        if other is not None:
            other.close()
        store.close()


def test_empty_source_remains_empty_without_copying_any_artifact_or_effect(tmp_path):
    store, old, target = setup(tmp_path)
    try:
        frozen = store.prepare_generation_transfer(old, target)
        assert frozen.generation_ids == [] and store.install_generation_transfer(frozen) == 0
        store.bind_epoch("game", "e2", previous="e1")
        assert store.reserve(target, "fresh", {}, generation_limit=1) == (True, None)
        assert store.install_generation_transfer(frozen) == 0
        with pytest.raises(JournalConflict, match="MODEL_REQUEST_LIMIT"):
            store.reserve(target, "over-budget", {}, generation_limit=1)
    finally:
        store.close()


def test_preparatory_effort_receipt_is_not_model_admission(tmp_path):
    store, old, target = setup(tmp_path)
    try:
        store.reserve(old, "source-model", {}, generation_limit=2)
        frozen = store.prepare_generation_transfer(old, target)
        store.bind_epoch("game", "e2", previous="e1")
        request = {"fallback": "insufficient_supported_levels"}
        store.reserve(target, "effort-selection", request)
        receipt = store.complete(target, "effort-selection", request, {"reason": request["fallback"]})
        assert store.install_generation_transfer(frozen) == 1
        assert store.get(target, receipt) == {"reason": request["fallback"]}
        store.reserve(target, "native-model", {}, generation_limit=2)
        assert store.install_generation_transfer(frozen) == 1
        with pytest.raises(JournalConflict, match="MODEL_REQUEST_LIMIT"):
            store.reserve(target, "cannot-reset", {}, generation_limit=2)
    finally:
        store.close()


def test_first_transfer_after_actual_target_generation_is_refused(tmp_path):
    store, old, target = setup(tmp_path)
    try:
        store.reserve(old, "source-model", {}, generation_limit=3)
        frozen = store.prepare_generation_transfer(old, target)
        store.bind_epoch("game", "e2", previous="e1")
        store.reserve(target, "already-admitted", {}, generation_limit=3)
        with pytest.raises(JournalConflict, match="TARGET_ADMITTED"):
            store.install_generation_transfer(frozen)
        with store.transaction(target) as db:
            assert len(store._generations(db, target)) == 1
    finally:
        store.close()


@pytest.mark.parametrize("after_admission", [False, True])
def test_empty_transfer_cannot_be_replaced_by_another_source(tmp_path, after_admission):
    store, old, target = setup(tmp_path)
    try:
        frozen = store.prepare_generation_transfer(old, target)
        assert store.install_generation_transfer(frozen) == 0
        changed = store.prepare_generation_transfer(old.model_copy(update={"activation": "another-source"}), target)
        store.bind_epoch("game", "e2", previous="e1")
        if after_admission:
            store.reserve(target, "new-target", {}, generation_limit=2)
        with pytest.raises(JournalConflict, match="TARGET_CONFLICT"):
            store.install_generation_transfer(changed)
        assert store.install_generation_transfer(frozen) == 0
    finally:
        store.close()


@pytest.mark.parametrize("damage", ["partial", "proof"])
def test_committed_transfer_never_repairs_missing_debits_or_tampered_proof(tmp_path, damage):
    store, old, target = setup(tmp_path)
    try:
        store.reserve(old, "source-model", {}, generation_limit=3)
        frozen = store.prepare_generation_transfer(old, target)
        store.install_generation_transfer(frozen)
        store.bind_epoch("game", "e2", previous="e1")
        store.reserve(target, "new-target", {}, generation_limit=3)
        with store.engine.begin() as db:
            if damage == "partial":
                db.execute(delete(generation_attempts).where(generation_attempts.c.id.in_(frozen.inherited_ids)))
            else:
                row = db.execute(select(artifacts.c.sha256, artifacts.c.body)).mappings().one()
                body = json.loads(row["body"])
                body["version"] = 2
                db.execute(update(artifacts).where(artifacts.c.sha256 == row["sha256"]).values(body=json.dumps(body)))
            before = store._generations(db, target)
        with pytest.raises(JournalConflict, match="TARGET_CONFLICT"):
            store.install_generation_transfer(frozen)
        with store.transaction(target) as db:
            assert store._generations(db, target) == before
    finally:
        store.close()
