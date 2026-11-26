"""Native activity inputs retain host job identity and cumulative accounting."""

import asyncio
import copy
import json
from pathlib import Path
from typing import cast

import jsonschema
import pytest
from pydantic import ValidationError
from pydantic_ai.usage import RunUsage

from civ_agent.orchestration.contracts import Scope
from civ_agent.orchestration.job_admission import JobAdmission
from civ_agent.orchestration.job_contracts import (
    HostUsage,
    JobPurpose,
    JobTelemetry,
    MemoryEditCounts,
    NativeJob,
    NativeJobReport,
    host_job_id,
    host_usage,
)
from civ_agent.orchestration.store import JournalConflict
from civ_agent.proposal_gateway import PlayerConnection

SCHEMA_PATH = Path(__file__).parents[1] / "src/main/resources/uncivai/schemas/communication.json"


def job(purpose: JobPurpose = "gameplay", *, name="main", parent=None, **scope):
    return NativeJob(
        scope=Scope(game="game", player="p0", epoch="e1", activation=name, **scope),
        job=name,
        purpose=purpose,
        parent=parent,
    )


def report(identity, *, sequence=1, phase="RUNNING", usage=None, **telemetry):
    return NativeJobReport(
        job=identity,
        usage=usage
        or HostUsage(input_tokens=100, cached_input_tokens=20, output_tokens=30, reasoning_output_tokens=10),
        telemetry=JobTelemetry.model_validate(
            {
                "sequence": sequence,
                "phase": phase,
                "startedAtMs": 1000,
                "elapsedMs": sequence * 10,
                "usageReported": True,
                "usageComplete": False,
                "modelRequests": sequence,
                **telemetry,
            }
        ),
    )


class Host:
    """Wire-contract fake; native capacity policy is covered by the Kotlin tests."""

    def __init__(self):
        self.schema = json.loads(SCHEMA_PATH.read_text())
        self.epoch, self.civilization, self.now = "e1", "Rome", 1000
        self.jobs, self.calls = {}, []
        self.invocations = 0
        self.reply_loss = None
        self.deny = None
        self.response = None

    async def tool(self, name, payload):
        assert name == "player_communication"
        jsonschema.validate(payload, self.schema)
        self.calls.append(copy.deepcopy(payload))
        if payload["epoch"] != self.epoch:
            return {"error": "STALE_EPOCH"}
        op = payload["op"]
        if op == "state":
            return {"epoch": self.epoch, "civilization": self.civilization}
        if self.response is not None:
            return self.response
        identity, prior = payload["job"], self.jobs.get(payload["job"])
        if prior is None:
            if op != "job_claim":
                return {"error": "INVALID_ARGUMENT"}
            if self.deny:
                return {"granted": False, "reason": self.deny}
            if payload["purpose"] in {"economy", "military"}:
                parent = self.jobs.get(payload.get("parentJob"))
                if parent is None or parent["expires"] <= self.now:
                    return {"error": "INVALID_ARGUMENT"}
            self.invocations += 1
            self.jobs[identity] = prior = {
                "purpose": payload["purpose"],
                "parentJob": payload.get("parentJob"),
                "expires": self.now + 90_000,
                "usage": {},
                "telemetry": None,
            }
        else:
            if op not in {"job_release", "job_report"} and prior["expires"] <= self.now:
                return {"granted": False}
            telemetry, previous = payload.get("telemetry"), prior["telemetry"]
            if telemetry is not None and previous is not None:
                if telemetry["sequence"] < previous["sequence"]:
                    return {"granted": True}
                if telemetry["sequence"] == previous["sequence"] and (
                    telemetry != previous or payload["usage"] != prior["reportedUsage"]
                ):
                    return {"error": "INVALID_ARGUMENT"}
                if any(payload["usage"][key] < prior["reportedUsage"][key] for key in payload["usage"]):
                    return {"error": "INVALID_ARGUMENT"}
                if (
                    any(telemetry[key] < previous[key] for key in ("elapsedMs", "modelRequests", "roleWaitMs"))
                    or previous["usageComplete"]
                    and not telemetry["usageComplete"]
                ):
                    return {"error": "INVALID_ARGUMENT"}
            if telemetry is not None:
                prior["telemetry"], prior["reportedUsage"] = copy.deepcopy(telemetry), copy.deepcopy(payload["usage"])
            for key, value in payload.get("usage", {}).items():
                prior["usage"][key] = max(prior["usage"].get(key, 0), value)
            if op == "job_release":
                prior["expires"] = 0
            elif op != "job_report":
                prior["expires"] = self.now + 90_000
        if self.reply_loss == op:
            self.reply_loss = None
            raise TimeoutError("Synthetic host committed before its reply was lost")
        return {"granted": True}


def service(host):
    async def connection(scope):
        return cast(PlayerConnection, host)

    return JobAdmission(game="game", player="p0", civilization="Rome", connection=connection)


def test_lost_claim_ack_retry_and_service_replacement_count_one_job_activation():
    async def run():
        host, identity = Host(), job()
        host.reply_loss = "job_claim"
        with pytest.raises(TimeoutError):
            await service(host).claim(identity)
        assert (await service(host).claim(NativeJob.model_validate_json(identity.model_dump_json()))).granted
        assert host.invocations == 1
        child = job("economy", name="economy", parent=identity)
        assert (await service(host).claim(child)).granted
        assert host.invocations == 2
        assert host.jobs[host_job_id(child)]["parentJob"] == host_job_id(identity)
        claims = [call for call in host.calls if call["op"] == "job_claim"]
        assert claims[0] == claims[1]

    asyncio.run(run())


def test_frozen_reports_are_monotonic_duplicate_and_lost_ack_safe_without_a_python_ledger(tmp_path):
    async def run():
        host, identity = Host(), job()
        await service(host).claim(identity)
        update = report(identity)
        host.reply_loss = "job_report"
        with pytest.raises(TimeoutError):
            await service(host).report(update)
        assert (await service(host).report(update)).granted
        assert host.jobs[host_job_id(identity)]["usage"] == update.usage.model_dump()
        older = report(identity, sequence=0, usage=HostUsage())
        assert (await service(host).report(older)).granted
        assert host.jobs[host_job_id(identity)]["telemetry"] == update.telemetry.model_dump()
        changed = update.model_copy(update={"usage": HostUsage(input_tokens=101)})
        with pytest.raises(JournalConflict, match="HOST_REJECTED"):
            await service(host).report(changed)
        decreased = report(identity, sequence=2, usage=HostUsage(input_tokens=99))
        with pytest.raises(JournalConflict, match="HOST_REJECTED"):
            await service(host).report(decreased)
        assert host.invocations == 1 and not list(tmp_path.iterdir())

    asyncio.run(run())


def test_heartbeat_renews_report_does_not_and_released_identity_is_never_reclaimed():
    async def run():
        host, identity = Host(), job()
        await service(host).claim(identity)
        original = host.jobs[host_job_id(identity)]["expires"]
        host.now += 1000
        await service(host).report(report(identity))
        assert host.jobs[host_job_id(identity)]["expires"] == original
        await service(host).heartbeat(identity)
        assert host.jobs[host_job_id(identity)]["expires"] == original + 1000
        final = report(identity, sequence=2, phase="COMPLETED", endedAtMs=2000, usageComplete=True)
        host.reply_loss = "job_release"
        with pytest.raises(TimeoutError):
            await service(host).release(final)
        assert (await service(host).release(final)).granted
        assert not (await service(host).claim(identity)).granted
        assert host.invocations == 1 and host.jobs[host_job_id(identity)]["expires"] == 0

    asyncio.run(run())


def test_offturn_communication_uses_authenticated_epoch_not_active_turn_connection():
    async def run():
        host = Host()
        identity = job("communication", name="offturn-message")
        assert (await service(host).claim(identity)).granted
        await service(host).heartbeat(report(identity, phase="WAITING_NEGOTIATION"))
        await service(host).release(report(identity, sequence=2, phase="COMPLETED", endedAtMs=2000, usageComplete=True))
        assert host.invocations == 1
        assert all(call["epoch"] == "e1" for call in host.calls)

    asyncio.run(run())


@pytest.mark.parametrize("reason", ["MODEL_INVOCATION_LIMIT", "ROLE_CAPACITY_TOO_SMALL", "FUTURE_NATIVE_DENIAL"])
def test_host_denial_reason_and_absent_budget_fields_are_preserved(reason):
    async def run():
        host = Host()
        host.deny = reason
        denied = await service(host).claim(job())
        assert not denied.granted and denied.reason == reason
        assert denied.remaining is None and denied.modelInvocations is None
        assert host.invocations == 0

    asyncio.run(run())


@pytest.mark.parametrize(
    "response", [{"granted": 1}, {"granted": "true"}, {"error": "INVALID_ARGUMENT"}, {"granted": True, "remaining": -1}]
)
def test_untyped_or_rejected_host_responses_fail_closed(response):
    async def run():
        host = Host()
        host.response = response
        with pytest.raises(JournalConflict, match="HOST_REJECTED"):
            await service(host).claim(job())

    asyncio.run(run())


def test_scope_is_checked_before_connection_and_authenticated_civilization_before_claim():
    async def run():
        host = Host()
        foreign = job().model_copy(update={"scope": job().scope.model_copy(update={"player": "p1"})})
        with pytest.raises(JournalConflict, match="SCOPE_CONFLICT"):
            await service(host).claim(foreign)
        assert not host.calls
        host.civilization = "Greece"
        with pytest.raises(JournalConflict, match="CONNECTION_SCOPE_CONFLICT"):
            await service(host).claim(job())
        assert [call["op"] for call in host.calls] == ["state"]
        host.civilization, host.epoch = "Rome", "e2"
        with pytest.raises(JournalConflict, match="CONNECTION_SCOPE_CONFLICT"):
            await service(host).claim(job())
        assert host.invocations == 0

    asyncio.run(run())


def test_parent_and_scope_are_bound_into_stable_host_identity_and_unsupported_depth_is_rejected():
    parent = job()
    child = job("military", name="military", parent=parent)
    assert host_job_id(child) == host_job_id(NativeJob.model_validate_json(child.model_dump_json()))
    other = job("military", name="military", parent=job(name="other-main"))
    assert host_job_id(child) != host_job_id(other)
    assert host_job_id(child) != host_job_id(
        child.model_copy(update={"scope": child.scope.model_copy(update={"activation": "other"})})
    )
    for purpose in ("gameplay", "communication", "consultation"):
        with pytest.raises(ValidationError):
            job(purpose, parent=parent)
    with pytest.raises(ValidationError):
        job("economy", parent=child)
    with pytest.raises(ValidationError):
        job("military")
    with pytest.raises(ValidationError):
        job("economy", parent=parent.model_copy(update={"parent": parent}))
    with pytest.raises(ValidationError):
        job("economy", parent=parent.model_copy(update={"scope": parent.scope.model_copy(update={"epoch": "old"})}))
    # A review may preserve attribution to an old, expired main after restore.
    assert (
        job(
            "review", parent=parent.model_copy(update={"scope": parent.scope.model_copy(update={"epoch": "old"})})
        ).parent
        is not None
    )


def test_invalid_mutated_nested_identity_is_revalidated_before_any_host_call():
    async def run():
        host = Host()
        invalid = job("economy", parent=job()).model_copy(update={"parent": None})
        with pytest.raises(JournalConflict, match="REQUEST_INVALID"):
            await service(host).claim(invalid)
        assert not host.calls

    asyncio.run(run())


@pytest.mark.parametrize(
    "typed,details,expected",
    [(17, {}, 17), (0, {"reasoning_tokens": 17}, 17), (17, {"reasoning_tokens": 99}, 17), (0, {}, 0)],
    ids=["typed", "legacy", "typed-precedence", "zero"],
)
def test_sdk_usage_maps_all_native_host_counters_without_generations_as_invocations(typed, details, expected):
    usage = RunUsage(
        input_tokens=123,
        cache_read_tokens=40,
        cache_write_tokens=9,
        output_tokens=50,
        requests=7,
        output_reasoning_tokens=typed,
        details=details,
    )
    assert host_usage(usage).model_dump() == {
        "input_tokens": 123,
        "cached_input_tokens": 40,
        "output_tokens": 50,
        "reasoning_output_tokens": expected,
    }
    assert not hasattr(host_usage(usage), "invocations")
    with pytest.raises(ValidationError):
        HostUsage(input_tokens=True)
    with pytest.raises(ValidationError):
        HostUsage(input_tokens=9_223_372_036_854_775_808)
    with pytest.raises(ValidationError):
        report(job(), usageComplete=True)
    with pytest.raises(ValidationError):
        MemoryEditCounts(rejected=1, rejectionReasons={})
