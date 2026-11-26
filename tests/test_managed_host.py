"""Shared host authority stays with one Temporal game owner across player turns."""

import asyncio
import copy

import pytest

from civ_agent.game_config import Limits
from civ_agent.operator_connection import OperatorError
from civ_agent.orchestration.managed_host import (
    GameAuthority,
    HumanActivation,
    ManagedHost,
    ManagedLimits,
    ManagedLimitsRequest,
    validate_limit_increase,
)
from civ_agent.orchestration.store import JournalConflict
from civ_agent.protocol import JsonObject


def authority():
    return GameAuthority(game="game", epoch="e1", lease_id="shared-lease", limits=Limits(max_invocations=200))


class Host:
    def __init__(self):
        self.calls = []
        self.status: JsonObject = {
            "epoch": "e1",
            "serverInstance": "server1",
            "phase": "PAUSED",
            "turn": 0,
            "handoff": 0,
            "activeCivilization": "Rome",
            "activeController": "AGENT",
            "scheduled": True,
            "leaseId": None,
            "leaseExpiresInMs": 0,
            "modelInvocations": 0,
            "modelUsage": {},
        }
        self.lose_acquire_reply = False
        self.forbidden = False

    def call(self, operation, **fields):
        assert not self.forbidden, "Replay cannot call the host"
        self.calls.append((operation, copy.deepcopy(fields)))
        if operation == "acquire":
            assert self.status["leaseId"] is None
            self.status.update(leaseId=fields["leaseId"], leaseExpiresInMs=60000)
            if self.lose_acquire_reply:
                self.lose_acquire_reply = False
                raise OperatorError("Lost acquire acknowledgement", "TRANSPORT_TIMEOUT")
        elif operation not in {"status"}:
            assert self.status["leaseId"] == fields["leaseId"]
            if operation == "release":
                self.status.update(leaseId=None, leaseExpiresInMs=0)
                if self.status["phase"] not in {"FINISHED", "HALTED"}:
                    self.status["phase"] = "PAUSED"
            elif operation == "pause":
                self.status["phase"] = "PAUSED"
            elif operation == "activate":
                self.status["phase"] = "ACTIVE"
            elif operation == "job_limits":
                assert 1 <= fields["maxConcurrent"] <= 64
                assert 1 <= fields["maxPerCivilization"] <= 4
        return {"status": copy.deepcopy(self.status), "playerToken": "must-not-enter-workflow-history"}


def test_one_game_owner_reuses_lease_and_preserves_host_usage_across_player_turns():
    async def run():
        host = Host()
        owner = ManagedHost(authority(), host)
        try:
            assert (await owner.start(authority())).leaseId == "shared-lease"
            await owner.configure_limits(ManagedLimitsRequest.for_limits(authority(), authority().limits))
            host.status.update(activeCivilization="France", handoff=1, modelInvocations=7)
            status = await owner.poll(authority())
            assert (status.activeCivilization, status.handoff, status.modelInvocations) == ("France", 1, 7)
            assert [operation for operation, _ in host.calls].count("acquire") == 1
            assert next(fields for operation, fields in host.calls if operation == "job_limits") == {
                "leaseId": "shared-lease",
                "maxInvocations": 0,
                "maxConcurrent": 4,
                "maxPerCivilization": 4,
            }
            assert await owner.release(authority()) == await owner.release(authority())
            assert [operation for operation, _ in host.calls].count("release") == 1
            assert host.status["modelInvocations"] == 7
        finally:
            await owner.close()

    asyncio.run(run())


@pytest.mark.parametrize("field", ["max_turns", "max_invocations", "max_seconds", "turn_timeout", "startup_timeout"])
def test_only_increases_to_existing_operator_limits_are_allowed(field):
    original = Limits(max_turns=5)
    values = original.model_dump()
    increased = ManagedLimits.model_validate({**values, field: values[field] + 1})
    validate_limit_increase(original, increased)
    with pytest.raises(ValueError, match="decrease"):
        validate_limit_increase(increased, original)


@pytest.mark.parametrize("global_limit,per_civilization", [(1, 1), (3, 3), (4, 4), (6, 4), (64, 4)])
def test_global_job_capacity_preserves_independent_native_per_civilization_limit(global_limit, per_civilization):
    async def run():
        host = Host()
        selected = authority().model_copy(
            update={"limits": Limits(max_invocations=200, max_concurrent_jobs=global_limit)}
        )
        owner = ManagedHost(selected, host)
        try:
            await owner.start(selected)
            original = selected.model_dump_json()
            await owner.configure_limits(ManagedLimitsRequest.for_limits(selected, selected.limits))
            assert next(fields for operation, fields in host.calls if operation == "job_limits") == {
                "leaseId": "shared-lease",
                "maxInvocations": 0,
                "maxConcurrent": global_limit,
                "maxPerCivilization": per_civilization,
            }
            assert selected.model_dump_json() == original
            assert [operation for operation, _ in host.calls].count("acquire") == 1
        finally:
            await owner.close()

    asyncio.run(run())


def test_public_game_capacity_validation_matches_native_global_range_before_start():
    from pydantic import ValidationError

    from civ_agent.game_config import GameConfig

    for limit in (1, 4, 6, 64):
        assert GameConfig.model_validate({"limits": {"max_concurrent_jobs": limit}}).limits.max_concurrent_jobs == limit
    for limit in (0, 65):
        with pytest.raises(ValidationError) as rejected:
            GameConfig.model_validate({"limits": {"max_concurrent_jobs": limit}})
        assert rejected.value.errors(include_input=False)[0]["loc"] == ("limits", "max_concurrent_jobs")


def test_unlimited_turns_capacity_settings_and_strict_native_limit_types():
    from pydantic import ValidationError

    validate_limit_increase(Limits(max_turns=5), Limits(max_turns=None))
    with pytest.raises(ValueError, match="decrease"):
        validate_limit_increase(Limits(max_turns=None), Limits(max_turns=5))
    for field in ("max_concurrent_jobs", "model_capacity_retries", "model_capacity_retry_delay"):
        original = Limits()
        with pytest.raises(ValueError, match="capacity"):
            validate_limit_increase(original, original.model_copy(update={field: getattr(original, field) + 1}))
    for value in (True, 30.5, "30"):
        with pytest.raises(ValidationError):
            ManagedLimits.model_validate({"max_seconds": value})


def test_configuring_increased_limits_is_idempotent_and_start_never_lowers_them():
    async def run():
        host = Host()
        owner = ManagedHost(authority(), host)
        try:
            await owner.start(authority())
            host.status.update(modelInvocations=7, modelUsage={"inputTokens": 123})
            increased = authority().limits.model_copy(update={"max_invocations": 300})
            command = ManagedLimitsRequest.for_limits(authority(), increased)
            first = await owner.configure_limits(command)
            assert await owner.configure_limits(command) == first
            await owner.start(authority())
            assert first.modelInvocations == 7 and first.modelUsage == {"inputTokens": 123}
            assert [fields["maxInvocations"] for operation, fields in host.calls if operation == "job_limits"] == [
                0,
                0,
            ]
            host.status["phase"] = "ACTIVE"
            with pytest.raises(JournalConflict, match="REQUIRE_PAUSED"):
                await owner.configure_limits(command)
        finally:
            await owner.close()

    asyncio.run(run())


@pytest.mark.parametrize("damage", ["authority", "epoch", "lease"])
def test_changed_authority_and_other_host_epoch_or_lease_are_fenced_before_mutation(damage):
    async def run():
        host = Host()
        owner = ManagedHost(authority(), host)
        request = authority()
        if damage == "authority":
            request = request.model_copy(update={"game": "other"})
        elif damage == "epoch":
            host.status["epoch"] = "other"
        else:
            host.status["leaseId"] = "another-owner"
        try:
            with pytest.raises(JournalConflict, match="CONFLICT"):
                await owner.start(request)
            assert all(operation == "status" for operation, _ in host.calls)
        finally:
            await owner.close()

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["FINISHED", "HALTED"])
def test_terminal_host_is_observed_without_acquiring_or_configuring_it(phase):
    async def run():
        host = Host()
        host.status["phase"] = phase
        owner = ManagedHost(authority(), host)
        try:
            assert (await owner.start(authority())).phase == (await owner.poll(authority())).phase == phase
            assert (await owner.pause(authority())).phase == phase
            assert all(operation == "status" for operation, _ in host.calls)
        finally:
            await owner.close()

    asyncio.run(run())


def test_finished_host_retains_owned_lease_while_final_usage_is_settled():
    async def run():
        host = Host()
        owner = ManagedHost(authority(), host)
        try:
            await owner.start(authority())
            host.status.update(phase="FINISHED", modelInvocations=7)
            host.calls.clear()
            status = await owner.poll(authority())
            assert status.phase == "FINISHED" and status.modelInvocations == 7
            assert host.calls == [("status", {}), ("heartbeat", {"leaseId": "shared-lease"})]
            await owner.release(authority())
            host.calls.clear()
            assert (await owner.poll(authority())).leaseId is None
            assert host.calls == [("status", {})]
        finally:
            await owner.close()

    asyncio.run(run())


def test_human_activation_checks_turn_and_keeps_its_player_token_inside_activity():
    async def run():
        host = Host()
        host.status.update(activeCivilization="Human", activeController="HUMAN")
        owner = ManagedHost(authority(), host)
        request = HumanActivation(authority=authority(), civilization="Human", handoff=0, activation_id="human0")
        try:
            await owner.start(authority())
            with pytest.raises(JournalConflict, match="HUMAN_TURN"):
                await owner.activate_human(request.model_copy(update={"handoff": 1}))
            status = await owner.activate_human(request)
            assert status.phase == "ACTIVE" and "playerToken" not in status.model_dump_json()
            assert next(fields for operation, fields in host.calls if operation == "activate") == {
                "leaseId": "shared-lease",
                "epoch": "e1",
                "serverInstance": "server1",
                "handoff": 0,
                "civilization": "Human",
                "activationId": "human0",
            }
        finally:
            await owner.close()

    asyncio.run(run())
