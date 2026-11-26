import copy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from civ_agent.economy_roles import TurnRoles
from civ_agent.execution_policy import ExecutionPolicy
from civ_agent.orchestration.role_state import capture_roles, restore_roles
from civ_agent.orchestration.store import JournalConflict
from tests.domain_fixtures import managed, run_async


@run_async
async def test_role_context_preserves_ownership_readiness_and_schedules(tmp_path):
    async with managed(tmp_path) as m:
        roles = m.roles
        roles.overrides["city1"] = "Emergency"
        roles.known_units = {"unit1", "unit2"}
        roles.ready["gameplay"] = True
        roles.deferral = {"deferChoices": True, "deferReason": "Explicit native deferral"}
        roles.military_revision = ("g1", 0, "revision", "wars")
        roles.military_observation_state = "a" * 64
        roles.messages["military"] = "Review changed ownership"
        roles.intent_handoffs["military"] = {
            "role": "military",
            "sourceTurn": 0,
            "attempts": [
                {
                    "operationId": "attempt:0",
                    "role": "military",
                    "sourceTurn": 0,
                    "status": "unexecuted",
                    "reason": "FRESH_OBSERVATION_REQUIRED",
                    "attemptedCommand": "MOVE_UNIT",
                }
            ],
        }
        roles.resumes["military"].set()
        saved = capture_roles(roles)
        restored = TurnRoles(roles.service, roles.record)
        restore_roles(restored, saved)
        assert capture_roles(restored) == saved
        assert restored.owner("city1") == "gameplay" and restored.owner("city2") == "economy"
        assert restored.economy_schedule is not None
        assert restored.ready["gameplay"] and not restored.ready["economy"]
        assert restored.military_revision == ("g1", 0, "revision", "wars")
        assert restored.jobs == {}  # Connections are rebound from registered jobs.
        assert restored.context()["intentHandoffs"][0]["operationId"] == "attempt:0"


@pytest.mark.parametrize(
    "missing",
    [("intent_handoffs",), ("military_observation_state",), ("intent_handoffs", "military_observation_state")],
)
@run_async
async def test_old_role_checkpoint_defaults_only_additive_fields(tmp_path, missing):
    async with managed(tmp_path) as m:
        saved = capture_roles(m.roles)
        for name in missing:
            saved["data"].pop(name)
        restored = TurnRoles(m.roles.service, m.roles.record)
        restore_roles(restored, saved)
        assert restored.intent_handoffs == {}
        assert restored.military_observation_state is None
        assert restored.ready == m.roles.ready
        saved["data"].pop("war_state")
        with pytest.raises(JournalConflict, match="INCOMPLETE"):
            restore_roles(TurnRoles(m.roles.service, m.roles.record), saved)


@run_async
async def test_unexecuted_specialist_intents_get_one_main_reassessment_without_forcing_an_action(tmp_path):
    async with managed(tmp_path) as m:
        roles = m.roles
        roles.ready.update(gameplay=True, economy=True, military=True)
        roles.intent_handoffs["military"] = {
            "attempts": [{"operationId": "retreat:0", "status": "unexecuted", "role": "military", "sourceTurn": 0}]
        }
        assert not await roles.notify_unexecuted_intents()  # Retained policies do not add a pass.
        roles.service.managed_session = SimpleNamespace(
            settings=SimpleNamespace(execution_policy=ExecutionPolicy.current())
        )
        assert await roles.notify_unexecuted_intents()
        assert not roles.ready["gameplay"] and roles.ready["military"]
        assert "accept risk" in roles.messages["gameplay"]
        roles.ready["gameplay"] = True  # Main chooses to finish after reconsidering.
        assert not await roles.notify_unexecuted_intents()
        assert roles.ready["gameplay"]
        roles.intent_handoffs["military"]["attempts"].append(
            {"operationId": "second:0", "status": "rejected", "role": "military", "sourceTurn": 0}
        )
        assert await roles.notify_unexecuted_intents()
        roles.ready["gameplay"] = True
        roles.intent_handoffs["military"]["attempts"].pop()
        assert not await roles.notify_unexecuted_intents()  # A -> A+B -> A never reopens A.
        restored = TurnRoles(roles.service, roles.record)
        restore_roles(restored, capture_roles(roles))
        assert not await restored.notify_unexecuted_intents()
        restored.intent_handoffs["military"]["attempts"].append(
            {"operationId": "late:0", "status": "unexecuted", "role": "military", "sourceTurn": 0}
        )
        restored.service.stopping = True
        assert not await restored.notify_unexecuted_intents()


@pytest.mark.parametrize("remaining", [0, 1])
@run_async
async def test_intent_attention_saturates_without_evicting_ids_or_reopening_main(tmp_path, remaining):
    async with managed(tmp_path) as m:
        roles = m.roles
        roles.service.managed_session = SimpleNamespace(
            settings=SimpleNamespace(execution_policy=ExecutionPolicy.current())
        )
        roles.ready.update(gameplay=True, economy=True, military=True)
        original = [f"offered:{n}" for n in range(256 - remaining)]
        roles.record["intentHandoffAttentionIds"] = original.copy()
        roles.intent_handoffs["military"] = {
            "attempts": [
                {"operationId": name, "status": "unexecuted", "role": "military", "sourceTurn": 0}
                for name in ("new:0", "new:1")
            ]
        }
        assert await roles.notify_unexecuted_intents() is bool(remaining)
        assert roles.record["intentHandoffAttentionSaturated"] is True
        assert len(roles.record["intentHandoffAttentionIds"]) == 256
        assert roles.record["intentHandoffAttentionIds"][: len(original)] == original
        # Main may accept risk and finish; later new IDs and returning old IDs
        # cannot create further automatic attention after this activation caps.
        roles.ready["gameplay"] = True
        roles.intent_handoffs["military"]["attempts"].append(
            {"operationId": "later:0", "status": "rejected", "role": "military", "sourceTurn": 0}
        )
        assert not await roles.notify_unexecuted_intents()
        assert roles.ready["gameplay"]
        restored = TurnRoles(roles.service, roles.record)
        restore_roles(restored, capture_roles(roles))
        assert not await restored.notify_unexecuted_intents()
        assert restored.ready["gameplay"]


@run_async
async def test_role_context_rejects_new_fields_and_other_activation(tmp_path):
    async with managed(tmp_path) as m:
        saved = capture_roles(m.roles)
        changed = copy.deepcopy(saved)
        changed["key"][2] = "another-activation"
        with pytest.raises(JournalConflict, match="IDENTITY_CONFLICT"):
            restore_roles(TurnRoles(m.roles.service, m.roles.record), changed)
        changed = copy.deepcopy(saved)
        changed["data"].pop("ready")
        with pytest.raises(JournalConflict, match="INCOMPLETE"):
            restore_roles(TurnRoles(m.roles.service, m.roles.record), changed)
        changed = copy.deepcopy(saved)
        changed["data"]["ready"]["economy"] = "true"
        with pytest.raises(ValidationError):
            restore_roles(TurnRoles(m.roles.service, m.roles.record), changed)
        changed = copy.deepcopy(saved)
        changed["data"]["ready"].pop("economy")
        with pytest.raises(JournalConflict, match="INCOMPLETE"):
            restore_roles(TurnRoles(m.roles.service, m.roles.record), changed)
        vars(m.roles)["new_state"] = True
        with pytest.raises(JournalConflict, match="FIELDS_CHANGED"):
            capture_roles(m.roles)
