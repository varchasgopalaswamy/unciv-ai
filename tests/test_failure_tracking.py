"""Operator reports connect rejected decisions to one durable native receipt."""

import hashlib
import json
import shutil

import pytest

from civ_agent.civilization_state import StateConflict
from civ_agent.structured_decisions import generation_event, journal
from civ_agent.tool_failures import decision_usage, failure_detail, failure_report
from tests.test_decision_recovery import begin, command
from tests.test_economy_optimization import managed
from tests.test_economy_roles import run_async


def test_usage_keeps_peace_phases_and_context_sizes_separate():
    common = {
        "purpose": "consultation",
        "schema": {"fingerprint": "same", "schemaCharacters": 100},
        "model": {"provider": "openai", "model": "gpt-6-luna"},
        "instructionCharacters": 400,
        "initialObservationCharacters": 600,
    }
    rows = decision_usage(
        [
            {**common, "modelPhase": "peace_gameplay"},
            {**common, "modelPhase": "peace_military", "instructionCharacters": 500},
        ]
    )
    assert [row["modelPhase"] for row in rows] == ["peace_gameplay", "peace_military"]
    assert [row["instructionCharacterSizes"] for row in rows] == [[400], [500]]
    assert all(row["initialObservationCharacterSizes"] == [600] for row in rows)
    assert all(row["usageUnknownRequests"] == 1 for row in rows)


@run_async
async def test_output_guard_failure_is_generation_not_transport(tmp_path):
    async with managed(tmp_path) as m:
        record = {"threadId": "thread"}
        await begin(m, record=record)
        await generation_event(m.gateway, record, "service/transportFailure", {"reason": "REVIEW_OUTPUT_STALLED"})
        saved = journal(m.memory, record["activeDecision"])
        assert saved is not None
        assert saved["status"] == "generation_failed"
        assert saved["error"] == "REVIEW_OUTPUT_STALLED"
        assert saved["category"] == "generation"


@run_async
async def test_forwarding_diagnostics_retain_safe_metadata_in_the_decision_journal(tmp_path):
    async with managed(tmp_path) as m:
        record = {"threadId": "thread"}
        loop = await begin(m, record=record)
        await loop.bind("turn")
        details = {
            "reason": "PROVIDER_FORWARD_FAILED",
            "rpcMethod": "provider/request",
            "requestId": "provider:request",
            "stage": "upstream_read",
            "exceptionType": "ClientPayloadError",
            "forwardedBytes": 200,
            "upstreamStatus": 200,
        }
        await generation_event(
            m.gateway,
            record,
            "service/requestFailed",
            {**details, "threadId": "thread", "turnId": "turn", "message": "private"},
        )
        saved = journal(m.memory, record["activeDecision"])
        assert saved is not None
        assert saved["requestFailure"] == details and saved["category"] == "transport"
        assert not m.host.executed


@run_async
async def test_prompt_rejection_retains_safe_cause_without_enabling_generation_retry(tmp_path):
    async with managed(tmp_path) as m:
        record = {"threadId": "thread"}
        loop = await begin(m, record=record)
        await loop.bind("turn")
        await generation_event(
            m.gateway,
            record,
            "turn/completed",
            {
                "threadId": "thread",
                "turn": {
                    "id": "turn",
                    "status": "failed",
                    "error": {
                        "codexErrorInfo": "other",
                        "message": "Invalid prompt: your prompt was flagged as potentially violating our usage policy. PRIVATE",
                        "additionalDetails": "PRIVATE",
                    },
                },
            },
        )
        with pytest.raises(ValueError, match="INCOMPLETE_GENERATION"):
            await loop.complete("", "failed")
        saved = journal(m.memory, record["activeDecision"])
        assert saved is not None and saved["modelFailure"] == {"reason": "promptPolicyRejected"}
        assert "PRIVATE" not in json.dumps(saved)
        assert "generationFailure" not in record and not m.host.executed
        source = m.memory.root
    game = tmp_path / "game"
    shutil.copytree(source, game / "agent-services" / "Rome" / "civilization")
    report = failure_report([game])
    failures = [f for f in report["failures"] if f["code"] == "PROMPT_POLICY_REJECTED"]
    assert len(failures) == 1
    assert failures[0]["category"] == "provider_policy"
    assert failures[0]["diagnostic"]["modelFailure"] == {"reason": "promptPolicyRejected"}


@pytest.mark.parametrize("rejected_again", [False, True])
@run_async
async def test_provider_policy_attempts_remain_visible_without_double_counting(tmp_path, rejected_again):
    async with managed(tmp_path) as m:
        loop = await begin(m, record={"threadId": "thread"})
        await loop.bind("turn")
        common = {"threadId": "thread", "turnId": "turn"}
        for request in ("first", "retry"):
            await generation_event(
                m.gateway,
                loop.record,
                "service/providerRequest",
                {**common, "requestId": request, "kind": "generation"},
            )
            if request == "first" or rejected_again:
                await generation_event(
                    m.gateway,
                    loop.record,
                    "service/providerRejection",
                    {
                        **common,
                        "requestId": request,
                        "reason": "promptPolicyRejected",
                        "httpStatus": 400,
                        "retryRequestId": "retry" if request == "first" else None,
                        "message": "PRIVATE",
                    },
                )
        if rejected_again:
            await generation_event(
                m.gateway,
                loop.record,
                "turn/completed",
                {
                    **common,
                    "turn": {
                        "id": "turn",
                        "status": "failed",
                        "error": {
                            "message": "Invalid prompt: your prompt was flagged as potentially violating our usage policy. PRIVATE",
                        },
                    },
                },
            )
            with pytest.raises(ValueError, match="INCOMPLETE_GENERATION"):
                await loop.complete("", "failed")
        else:
            await loop.complete(
                loop.contract.encode(
                    {"kind": "act", "operations": [command("SET_CITY_FOCUS", target="city2", selection="FoodFocus")]}
                )
            )
        assert len(m.host.executed) == (0 if rejected_again else 1)
        assert len(loop.record["providerRejections"]) == (2 if rejected_again else 1)
        source = m.memory.root
    game = tmp_path / "game"
    shutil.copytree(source, game / "agent-services" / "Rome" / "civilization")
    report = failure_report([game], code="PROMPT_POLICY_REJECTED")
    failures = report["failures"]
    assert len(failures) == (2 if rejected_again else 1)
    assert all(f["kind"] == "provider_rejection" for f in failures)
    assert failures[0]["diagnostic"]["retryRequestId"] == "retry"
    detail = failure_detail(report, failures[0]["id"])
    assert "PRIVATE" not in json.dumps(detail)
    usage = report["structuredUsage"][0]
    assert usage["providerAttempts"] == 2 and usage["policyRetryAttempts"] == 1
    assert usage["unreportedRejectedAttempts"] == len(failures)


@pytest.mark.parametrize("partially_applied", [False, True])
@run_async
async def test_duplicate_plan_receipt_is_rejected_and_recovery_is_executable(tmp_path, partially_applied):
    async with managed(tmp_path) as m:
        if partially_applied:
            await m.gateway.call(m.economy, "memory_read", {"ids": [m.plans[1]]}, "read-other-plan")
        loop = await begin(m)
        changes = ([{"op": "amend", "id": m.plans[1], "fields": {"priority": 2}}] if partially_applied else []) + [
            {"op": "create", "content": {"type": "city_plan", "city": "city1", "reasons": {"growth": "Grow"}}}
        ]
        result = await loop.complete(
            loop.contract.encode(
                {
                    "kind": "act",
                    "operations": [
                        {
                            "tool": "memory_update",
                            "arguments": {"changes": changes},
                        },
                        command("SET_CITY_FOCUS", target="city2", selection="FoodFocus"),
                    ],
                }
            )
        )
        rejected = result["receipts"][0]
        assert rejected["status"] == ("confirmed" if partially_applied else "rejected")
        assert result["receipts"][1]["status"] == "unexecuted"
        assert not m.host.executed
        assert await loop.execute() == result
        loop.delivered(result)
        await loop.begin(result)
        recovered = await loop.complete(
            loop.contract.encode(
                {"kind": "inspect", "operations": rejected["result"]["changes"][-1]["recovery"]["reads"]}
            )
        )
        assert recovered["receipts"][0]["status"] == "confirmed"
        assert recovered["receipts"][0]["result"]["records"][0]["id"] == m.plans[0]
        source = m.memory.root
    game = tmp_path / "game"
    shutil.copytree(source, game / "agent-services" / "Rome" / "civilization")
    report = failure_report([game])
    rejected = [f for f in report["failures"] if f["code"] == "CITY_PLAN_ALREADY_EXISTS"]
    assert len(rejected) == 1 and rejected[0]["executionStatus"] == "rejected"


@run_async
async def test_memory_conflict_recovery_fits_the_strict_inspection_contract(tmp_path):
    async with managed(tmp_path) as m:
        loop = await begin(m)
        result = await loop.complete(
            loop.contract.encode(
                {
                    "kind": "act",
                    "operations": [
                        {
                            "tool": "memory_update",
                            "arguments": {"changes": [{"op": "amend", "id": m.plans[0], "fields": {"priority": 2}}]},
                        }
                    ],
                }
            )
        )
        conflict = result["receipts"][0]["result"]["changes"][0]
        assert conflict["reason"] == "MEMORY_CONFLICT"
        loop.delivered(result)
        await loop.begin(result)
        recovered = await loop.complete(
            loop.contract.encode({"kind": "inspect", "operations": conflict["recovery"]["reads"]})
        )
        row = recovered["receipts"][0]
        assert row["status"] == "confirmed" and row["result"]["memoryDelivery"]["items"][0]["kind"] == "full"
        loop.delivered(recovered)
        await loop.begin(recovered)
        changed = await loop.complete(
            loop.contract.encode(
                {
                    "kind": "act",
                    "operations": [
                        {
                            "tool": "memory_update",
                            "arguments": {"changes": [{"op": "amend", "id": m.plans[0], "fields": {"priority": 2}}]},
                        }
                    ],
                }
            )
        )
        assert changed["receipts"][0]["result"]["changes"][0]["accepted"]


@run_async
async def test_provider_attempts_are_deduplicated_and_budget_blocks_are_correlated(tmp_path):
    async with managed(tmp_path / "working") as m:
        record = {"threadId": "thread"}
        loop = await begin(m, record=record)
        await loop.bind("turn")
        for identity, kind in (("first", "compaction"), ("second", "generation"), ("second", "generation")):
            await generation_event(
                m.gateway,
                record,
                "service/providerRequest",
                {
                    "requestId": identity,
                    "kind": kind,
                    "threadId": "thread",
                    "turnId": "turn",
                },
            )
        await generation_event(
            m.gateway,
            record,
            "service/requestFailed",
            {
                "threadId": "thread",
                "turnId": "turn",
                "reason": "PROVIDER_DECISION_REQUEST_LIMIT",
            },
        )
        saved = journal(m.memory, record["activeDecision"])
        assert saved is not None
        assert saved["category"] == "request_budget"
        assert saved["status"] == "generation_failed"
        usage = decision_usage([saved])[0]
        assert usage["providerAttempts"] == 2 and usage["compactionAttempts"] == 1
        assert usage["providerAttemptsUnknownDecisions"] == 0
        assert usage["usageUnknownRequests"] == 1
        source = m.memory.root
    game = tmp_path / "game"
    shutil.copytree(source, game / "agent-services" / "Rome" / "civilization")
    failures = failure_report([game])["failures"]
    assert len(failures) == 1 and failures[0]["category"] == "request_budget"


@run_async
async def test_confirmed_action_with_failed_refresh_remains_confirmed(tmp_path):
    async with managed(tmp_path / "working") as m:
        loop = await begin(m)
        native = m.host.tool

        async def failed_refresh(name, arguments):
            if name == "player_turn" and m.host.executed:
                raise StateConflict("HOST_PROTOCOL_ERROR")
            return await native(name, arguments)

        m.host.tool = failed_refresh
        result = await loop.complete(
            loop.contract.encode(
                {"kind": "act", "operations": [command("SET_CITY_FOCUS", target="city2", selection="FoodFocus")] * 2}
            )
        )
        receipt = result["receipts"][0]
        assert receipt["status"] == "confirmed"
        assert receipt["result"]["applied"] is True
        assert receipt["result"]["error"] == "STATE_UPDATE_FAILED"
        assert result["receipts"][1]["status"] == "unexecuted"
        assert len(m.host.executed) == 1
        source = m.memory.root
    game = tmp_path / "game"
    shutil.copytree(source, game / "agent-services" / "Rome" / "civilization")
    report = failure_report([game])
    failure = next(row for row in report["failures"] if row["code"] == "STATE_UPDATE_FAILED")
    assert failure["executionStatus"] == "confirmed"
    assert failure["category"] == "refresh"
    assert failure_detail(report, failure["id"])["nativeReceipt"]["result"].get("error") is None


@run_async
async def test_failure_tracking_is_read_only_correlated_and_does_not_count_replays(tmp_path):
    async with managed(tmp_path / "working") as m:
        loop = await begin(m)
        native = m.host.tool

        async def reject(name, arguments):
            if name == "player_command":
                return {"error": "INSUFFICIENT_GOLD", "applied": False, "requestId": arguments["requestId"]}
            return await native(name, arguments)

        m.host.tool = reject
        result = await loop.complete(
            loop.contract.encode(
                {"kind": "act", "operations": [command("SET_CITY_FOCUS", target="city2", selection="FoodFocus")] * 2}
            )
        )
        assert await loop.execute() == result
        await loop.begin(result)
        await loop.complete("invalid JSON")
        source = m.memory.root
    game = tmp_path / "game"
    destination = game / "agent-services" / "Rome" / "civilization"
    shutil.copytree(source, destination)
    database = destination / "memory.sqlite3"
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    report = failure_report([game], role="economy")
    assert not report["unavailable"]
    errors = [row for row in report["failures"] if row["tool"] == "player_command"]
    assert len(errors) == 1 and errors[0]["category"] == "legality"
    assert errors[0]["decisionId"] == result["decisionId"] and errors[0]["schemaFingerprint"]
    assert report["games"][0]["errorCalls"] == 1
    assert report["games"][0]["pendingCalls"] == 0
    assert {row["kind"] for row in report["failures"]} == {"error", "partial", "validation_failed"}
    evidence = failure_detail(report, errors[0]["id"])
    assert evidence["nativeReceipt"]["result"]["error"] == "INSUFFICIENT_GOLD"
    assert evidence["nativeReceipt"]["payload"]["kind"] == "SET_CITY_FOCUS"
    invalid = next(row for row in report["failures"] if row["kind"] == "validation_failed")
    details = failure_detail(report, invalid["id"])["decision"]
    assert "outputSha256" in details and "output" not in details
    assert report["structuredUsage"][0]["decisionRequests"] == 2
    assert report["structuredUsage"][0]["usageUnknownRequests"] == 2
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
