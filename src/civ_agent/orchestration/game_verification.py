"""Audit the bounded scripted run against native checkpoint and private receipts."""

import asyncio
import gzip
import hashlib
import json
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

from pydantic_ai.messages import ModelResponse, ToolCallPart

from civ_agent.orchestration.contracts import GameRunRequest, GameRunResult, canonical, digest
from civ_agent.orchestration.gateway_execution import operation_identity
from civ_agent.orchestration.store import JournalConflict, WorkflowStore
from civ_agent.orchestration.store_verification import verify_native_stores
from civ_agent.orchestration.transcripts import backend, read
from civ_agent.tls import write_private_json


def database(path):
    return closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True))


def valid_archive(root, head):
    try:
        path = root / "coordinator/notebook/native-archives" / (head["archive"] + ".json.gz")
        body = json.loads(gzip.decompress(path.read_bytes()))
        return body.get("retired") is True and digest(body) == head["archive"]
    except (OSError, ValueError, KeyError, EOFError):
        return False


def verify_routing_artifacts(root, result, checkpoint):
    """Match both routing attempts to exact native text and crash boundaries."""
    fixture = json.loads((root / "communication-fixture.json").read_text())
    crashes = json.loads((root / "routing-crashes.json").read_text())
    count = json.loads((root / "advisory-broker/provider-count.json").read_text())["calls"]
    with database(root / "advisory-broker/decisions.sqlite3") as db:
        rows = db.execute("SELECT id,result,input_tokens FROM routing_requests").fetchall()
        optional_requests = db.execute("SELECT count(*) FROM requests").fetchone()[0]
    receipts = [json.loads(row[1]) for row in rows]
    by_direction = {receipt["direction"]: receipt for receipt in receipts}
    message = fixture["receipt"]["message"]
    original = fixture["request"]["text"]
    expected = "Acknowledged " + message["message"] + ": " + original[:2000]
    native_messages = [
        native
        for native in checkpoint.get("communication", {}).get("requests", {}).values()
        if native["response"].get("message", {}).get("author") == "Rome"
    ]
    responses = [native for native in native_messages if native["response"]["message"].get("text") == expected]
    with database(root / "coordinator/notebook/memory.sqlite3") as db:
        states = json.loads(db.execute("SELECT value FROM meta WHERE key='routing:events'").fetchone()[0])
        links = [
            json.loads(row[0]) for row in db.execute("SELECT value FROM meta WHERE key LIKE 'nativeRoutingLink:%'")
        ]
        drafts = [
            json.loads(row[0])
            for row in db.execute(
                "SELECT r.body FROM records r JOIN heads h ON r.key=h.key AND r.version=h.version WHERE r.category='delivery'"
            )
        ]
    routing_links = [
        link for link in result.workflow_links if link.kind in {"routing", "outgoing-draft", "outgoing-delivery"}
    ]
    crash_checks = (
        [crash["point"] for crash in crashes] == ["routing-reply", "routing-delivery"]
        and all(crash["exitCode"] == 137 for crash in crashes)
        and crashes[0]["brokerReplyPersisted"]
        and crashes[0]["workerReplyPersisted"]
        and crashes[1]["nativeReceiptPresent"]
        and crashes[1]["outcomeUnrecorded"]
        and crashes[1]["outgoingText"] == expected
        and states[str(crashes[0]["incomingRow"])]["deadline"] == crashes[0]["incomingDeadline"]
        and states[str(crashes[0]["incomingRow"])].get("completed")
    )
    receipt_checks = (
        len(rows) == count == 2
        and optional_requests == 0
        and set(by_direction) == {"incoming", "outgoing"}
        and all(type(row[2]) is int and row[2] > 0 for row in rows)
        and all(
            not receipt.get("pending") and receipt.get("fallback") is None and receipt["choice"] == "batch"
            for receipt in receipts
        )
        and {row[0] for row in rows} == {receipt["requestId"] for receipt in receipts}
        and crashes[0]["requestId"] == by_direction["incoming"]["requestId"]
        and any(item["text"] == original for item in by_direction["incoming"]["replayEvidence"]["pending"])
        and by_direction["outgoing"]["replayEvidence"]["proposedMessage"]["text"] == expected
    )
    native_checks = (
        message["text"] == original
        and len(responses) == len(native_messages) == 1
        and responses[0]["audit"]["handoff"] == 1
        and responses[0]["response"]["message"]["participants"] == sorted(message["participants"])
        and not responses[0]["response"]["message"]["public"]
        and len(drafts) == 1
        and drafts[0]["status"] == "sent"
        and drafts[0]["payload"]["text"] == expected
        and drafts[0]["requestId"] == crashes[1]["nativeRequestId"]
    )
    history_checks = Counter(link.kind for link in routing_links) == {
        "routing": 2,
        "outgoing-draft": 1,
        "outgoing-delivery": 1,
    } and {link.id for link in routing_links} == {link["id"] for link in links}
    return {
        "verified": bool(crash_checks and receipt_checks and native_checks and history_checks),
        "routingRequests": len(rows),
        "routingProviderCalls": count,
        "routingInputTokens": sum(row[2] or 0 for row in rows),
        "routingWorkflows": len(routing_links),
    }


async def verify_transcripts(root, result, native_ids):
    store = WorkflowStore(root / "player-store", player=result.player)
    scopes = [
        (role.scope, role.history, "player_role_v1", role.scope.identity("conversation", "role"))
        for role in result.roles
    ]
    scopes.extend(
        (review.scope, review.history, "strategic_review_v1", review.scope.identity("conversation", "review"))
        for review in result.reviews
    )
    scopes.extend(
        (
            peace.scope,
            phase.history,
            "peace_military_v1" if phase.phase == "military" else "peace_main_v1",
            peace.scope.model_copy(update={"activation": digest([peace.scope.model_dump(), phase.phase])}).identity(
                "conversation", "peace"
            ),
        )
        for peace in result.peace
        for phase in peace.phases
    )
    runs, linked = 0, 0
    verified_ids = set()
    selected_commands = {}
    try:
        steps = backend(store)
        for scope, reference, agent, conversation in scopes:
            messages = await read(store, scope, reference, agent=agent)
            records = await steps.list_runs(conversation_id=conversation)
            if not records:
                raise JournalConflict("TRANSCRIPT_RUN_REQUIRED")
            for record in records:
                if (
                    record.agent_name != agent
                    or record.metadata.get("scope") != canonical(scope.model_dump(mode="json"))
                    or await steps.list_unresolved_tool_effects(run_id=record.run_id)
                ):
                    raise JournalConflict("TRANSCRIPT_UNRESOLVED_RUN")
                events = await steps.list_events(run_id=record.run_id)
                for call_id in {event.tool_call_id for event in events if event.kind == "tool_call_started"}:
                    if call_id is None:
                        raise JournalConflict("TRANSCRIPT_TOOL_ID_REQUIRED")
                    effect = await steps.get_tool_effect(run_id=record.run_id, tool_call_id=call_id)
                    if effect is None or effect.status != "completed":
                        raise JournalConflict("TRANSCRIPT_TOOL_UNRESOLVED")
                    if agent == "player_role_v1":
                        offset = record.metadata.get("modelStepOffset")
                        if offset is None:
                            if effect.idempotency_key not in native_ids:
                                raise JournalConflict("TRANSCRIPT_NATIVE_RECEIPT_REQUIRED")
                        else:
                            started = [
                                event
                                for event in events
                                if event.kind == "tool_call_started" and event.tool_call_id == call_id
                            ]
                            role = next(role for role in result.roles if role.scope == scope)
                            expected = {
                                operation_identity(role.job, scope.epoch, int(offset) + event.step_index, call_id)
                                for event in started
                            }
                            if expected != {effect.idempotency_key}:
                                raise JournalConflict("TRANSCRIPT_NATIVE_LINK_CONFLICT")
                        linked += 1
                        verified_ids.add(effect.idempotency_key)
                        if effect.tool_name == "execute_commands":
                            choices = [
                                part.args_as_dict().get("commands")
                                for message in messages
                                if isinstance(message, ModelResponse)
                                for part in message.parts
                                if isinstance(part, ToolCallPart)
                                and part.tool_call_id == call_id
                                and part.tool_name == effect.tool_name
                            ]
                            if len(choices) != 1 or not isinstance(choices[0], list):
                                raise JournalConflict("TRANSCRIPT_NATIVE_CHOICES_REQUIRED")
                            selected_commands[effect.idempotency_key] = choices[0]
                runs += 1
    finally:
        store.close()
    return (
        {"snapshotPointers": len(scopes), "upstreamAgentRuns": runs, "linkedNativeToolEffects": linked},
        verified_ids,
        selected_commands,
    )


def verify_game_artifacts(root: Path):
    manifest = json.loads((root / "manifest.json").read_text())
    config = json.loads((root / "control/run.json").read_text())
    request = GameRunRequest.model_validate(config["request"])
    result = GameRunResult.model_validate_json((root / "results/game-result.json").read_text())
    checkpoint = json.loads((root / "host/checkpoint.json").read_text())
    game = json.loads(checkpoint["gameJson"])
    native_rows = checkpoint["players"]["Rome"]["receipts"]
    native = {row["request"]["requestId"]: row for row in native_rows}
    with database(root / "coordinator/notebook/memory.sqlite3") as db:
        commands = db.execute("SELECT id, payload, result FROM outbox WHERE transport = 'player_command'").fetchall()
        native_decisions = [
            {**json.loads(row[0]), "_job": row[1]}
            for row in db.execute(
                "SELECT r.body,r.job FROM records r JOIN heads h ON r.key=h.key AND r.version=h.version "
                "WHERE r.category='decision' AND json_extract(r.body,'$.native.transport')='player_command'"
            )
        ]
        unfinished_jobs = db.execute("SELECT count(*) FROM jobs WHERE status != 'COMPLETED'").fetchone()[0]
        unfinished_domain_calls = db.execute("SELECT count(*) FROM calls WHERE result IS NULL").fetchone()[0]
        heads = [json.loads(row[0]) for row in db.execute("SELECT value FROM meta WHERE key LIKE 'nativeGatewayJob:%'")]
        turns = [json.loads(row[0]) for row in db.execute("SELECT value FROM meta WHERE key LIKE 'nativeTurn:%'")]
        journal_contexts = sum(
            "gatewayState" in json.loads(row[0]) for row in db.execute("SELECT body FROM decision_journal")
        )
        completed_waits = db.execute("SELECT count(*) FROM meta WHERE key LIKE 'nativeWait:%'").fetchone()[0]
        native_ids = {row[0] for row in db.execute("SELECT id FROM decision_journal")}
        if result.workflow_links:
            review_rows = db.execute("SELECT status FROM reviews").fetchall()
            permits = [
                json.loads(row[0]) for row in db.execute("SELECT value FROM meta WHERE key LIKE 'nativeReviewGate:%'")
            ]
            pending_notifications = db.execute(
                "SELECT count(*) FROM native_event_notifications WHERE notify=1 AND (handled=0 OR signaled=0)"
            ).fetchone()[0]
            communication_requests = db.execute(
                "SELECT id, payload, result FROM outbox WHERE transport='player_communication'"
            ).fetchall()
        else:
            review_rows, permits, pending_notifications, communication_requests = [], [], 0, []
    with database(root / "player-store/workflow.sqlite3") as db:
        attempts = db.execute("SELECT count(*) FROM generation_attempts").fetchone()[0]
        unfinished = db.execute("SELECT count(*) FROM calls WHERE result IS NULL").fetchone()[0]
        advisory_outcomes = [
            json.loads(
                db.execute("SELECT body FROM artifacts WHERE sha256=?", (advisory.outcome.sha256,)).fetchone()[0]
            )
            for advisory in result.advisories
        ]
    native_domain_valid = True
    trusted_ids = {
        decision["native"]["operationId"]
        for decision in native_decisions
        if decision["command"].get("kind") == "END_TURN" and "toolOperationId" not in decision["native"]
    }
    if trusted_ids:
        # Historical workflow-only handoff claims require their retired replay
        # verifier. Reading an archive never manufactures that proof from an
        # SDK tool call or treats an unverifiable claim as a native receipt.
        native_domain_valid = False
    roles_by_job = {role.job: role for role in result.roles}
    for decision in native_decisions:
        marker, payload = decision["native"], decision["command"]
        identifier = marker["operationId"]
        recorded = native.get(identifier)
        role = roles_by_job.get(decision["_job"])
        tool_id = marker.get("toolOperationId", "")
        expected_id = "r" + hashlib.sha256((decision["_job"] + ":" + tool_id).encode()).hexdigest()[:24]
        if (
            recorded is None
            or role is None
            or (role.scope.game, role.scope.player) != (result.game, result.player)
            or payload.get("epoch") != role.scope.epoch
            or payload.get("epoch") != checkpoint["epoch"]
            or identifier != expected_id
            or payload.get("requestId") != identifier
            or marker["requestSha256"] != digest(["player_command", payload])
            or marker["receiptSha256"] != digest(recorded["receipt"])
            or decision["outcome"] != "confirmed"
            or not tool_id.startswith("native:")
        ):
            native_domain_valid = False
        commands.append(
            (identifier, canonical(payload), canonical(recorded["receipt"]) if recorded is not None else None)
        )
    kinds = Counter(json.loads(payload)["kind"] for _, payload, _ in commands)
    confirmed = all(
        receipt is not None
        and "error" not in json.loads(receipt)
        and identifier in native
        and all(native[identifier]["request"].get(key) == value for key, value in json.loads(payload).items())
        and native[identifier]["receipt"] == json.loads(receipt)
        for identifier, payload, receipt in commands
    )
    communication_native = checkpoint.get("communication", {}).get("requests", {})
    communication_confirmed = all(
        receipt is not None
        and "error" not in json.loads(receipt)
        and ("Rome:" + identifier) in communication_native
        and communication_native["Rome:" + identifier]["request"] == json.loads(payload)
        and communication_native["Rome:" + identifier]["response"] == json.loads(receipt)
        for identifier, payload, receipt in communication_requests
    )
    crash_required = manifest.get("crashTest", False)
    restart_required = crash_required or (request.restart_after is not None and request.restart_after < request.turns)
    project = manifest["project"]
    restarted = (
        all(
            (root / (project + "-" + suffix + ".log")).read_text().count(marker) >= 2
            for suffix, marker in (("coordinator", "COORDINATOR-READY"), ("player", "PLAYER-WORKER-READY"))
        )
        if restart_required
        else False
    )
    archives_valid = all(valid_archive(root, head) for head in heads)
    delivered_advisories = sum(
        row.get("status") == "delivered"
        for head in heads
        if valid_archive(root, head)
        for row in json.loads(
            gzip.decompress(
                (root / "coordinator/notebook/native-archives" / (head["archive"] + ".json.gz")).read_bytes()
            )
        )
        .get("state", {})
        .get("data", {})
        .get("memory", {})
        .get("advisoryDelivery", [])
    )
    crashes = json.loads((root / "crashes.json").read_text()) if crash_required else []
    crash_ids = {row["id"] for row in crashes}
    crash_checks = (
        (
            len(crashes) == 3
            and crash_ids == {"command-reply", "handoff-reply", "handoff-record"}
            and all(row["epoch"] == checkpoint["epoch"] and row["killed"].get("coordinator") == 137 for row in crashes)
            and all(
                row["nativeReceiptPresent"]
                and row["outcomeUnrecorded"]
                and row["requestId"] in native
                and native[row["requestId"]]["request"]["kind"] == row["kind"]
                for row in crashes
                if row["point"] == "native-reply"
            )
            and next(row for row in crashes if row["id"] == "command-reply")["killed"]
            == {"player": 137, "coordinator": 137, "host": 137}
            and [row["generationsBeforeKill"] for row in crashes]
            == sorted(row["generationsBeforeKill"] for row in crashes)
            and attempts >= max(row["generationsBeforeKill"] for row in crashes)
        )
        if crash_required
        else True
    )
    host_required = manifest.get("hostRestart", False)
    host_event = json.loads((root / "host-restart.json").read_text()) if host_required else None
    host_checks = (
        (
            host_event["hostExitCode"] == 137
            and host_event["pythonContainersRetained"]
            and host_event["receiptsRetained"]
            and host_event["epoch"] == host_event["afterEpoch"] == checkpoint["epoch"]
            and host_event["handoff"] == host_event["afterHandoff"]
            and all(
                marker in (root / (project + "-coordinator.log")).read_text()
                for marker in ("HOST-RECONNECTING", "HOST-RECONNECTED")
            )
        )
        if host_event is not None
        else True
    )
    communication_test = manifest.get("communicationTest", False)
    communication_checks = True
    if communication_test:
        fixture = json.loads((root / "communication-fixture.json").read_text())
        crash = json.loads((root / "communication-crash.json").read_text())
        message = fixture["receipt"]["message"]
        responses = [
            row
            for row in communication_native.values()
            if row["response"].get("message", {}).get("author") == "Rome"
            and row["response"]["message"]["text"].startswith("Acknowledged " + message["message"] + ":")
        ]
        communication_checks = (
            crash["exitCode"] == 137
            and crash["signalUnacknowledged"]
            and result.communication_duplicates >= 1
            and len(responses) == 1
            and responses[0]["audit"]["handoff"] == 1
            and responses[0]["response"]["message"]["participants"] == sorted(message["participants"])
            and not responses[0]["response"]["message"]["public"]
        )
    review_test = manifest.get("reviewTest", False)
    review_checks = True
    if review_test:
        crash = json.loads((root / "review-crash.json").read_text())
        expected_reviews = 1 + sum(
            source_turn >= request.review_interval and source_turn % request.review_interval == 0
            for source_turn in range(request.turns - 1)
        )
        review_checks = (
            crash["exitCode"] == 137
            and crash["reportsPublished"] == 1
            and crash["permitsReady"]
            and crash["generationsBeforeKill"] == 1
            and len(result.reviews) == expected_reviews
            and all(review.status == "COMPLETED" for review in result.reviews)
            and len({review.scope.activation for review in result.reviews}) == len(result.reviews)
        )
    wait_test = manifest.get("waitTest", False)
    wait_checks = True
    if wait_test:
        events = json.loads((root / "wait-crashes.json").read_text())
        wait_checks = (
            [event["point"] for event in events] == ["role-wait-start", "role-wait-settled"]
            and all(event["killed"] == {"coordinator": 137, "player": 137} for event in events)
            and events[0]["waitPending"]
            and events[0]["completedWaits"] == 0
            and not events[1]["waitPending"]
            and events[1]["completedWaits"] == 1
            and completed_waits == 1
            and sum(role.usage.get("waits", 0) for role in result.roles) == 1
        )
    advisory_test = manifest.get("advisoryTest", False)
    advisory_checks, broker_requests = True, 0
    if advisory_test:
        crash = json.loads((root / "advisory-crash.json").read_text())
        calls = json.loads((root / "advisory-broker/provider-count.json").read_text())["calls"]
        with database(root / "advisory-broker/decisions.sqlite3") as db:
            rows = db.execute("SELECT id, result, reserved FROM requests").fetchall()
        broker_requests = len(rows)
        advisory_checks = (
            crash["exitCode"] == 137
            and crash["brokerReceiptsBeforeKill"] >= 1
            and bool(result.advisories)
            and delivered_advisories > 0
            and calls == broker_requests
            and {advisory.request_id for advisory in result.advisories} == {row[0] for row in rows}
            and all(row[1] is not None and row[2] == 0 for row in rows)
            and all(
                outcome.get("questionResults")
                and all(row["status"] == "accepted" for row in outcome["questionResults"])
                for outcome in advisory_outcomes
            )
            and all(advisory.scope.epoch == checkpoint["epoch"] for advisory in result.advisories)
        )
    routing_test = manifest.get("routingTest", False)
    routing_report = verify_routing_artifacts(root, result, checkpoint) if routing_test else {"verified": True}
    stores_report = (
        asyncio.run(verify_native_stores(root, result, request)) if manifest.get("storesTest") else {"verified": True}
    )
    try:
        persistence_report, verified_ids, selected_commands = asyncio.run(verify_transcripts(root, result, native_ids))
    except (ValueError, OSError):
        persistence_report = {"snapshotPointers": 0, "upstreamAgentRuns": 0, "linkedNativeToolEffects": 0}
        verified_ids = set()
        selected_commands = {}
    native_domain_valid = native_domain_valid and all(
        decision["native"].get("toolOperationId", "").rsplit(":", 1)[0] in verified_ids for decision in native_decisions
    )
    for decision in native_decisions:
        identity = decision["native"].get("toolOperationId", "").rsplit(":", 1)
        if len(identity) != 2:
            native_domain_valid = False
            continue
        parent, index = identity
        choices = selected_commands.get(parent, [])
        if not index.isdigit() or int(index) >= len(choices):
            native_domain_valid = False
            continue
        choice = choices[int(index)]
        if not isinstance(choice, dict):
            native_domain_valid = False
            continue
        command = decision["command"]
        selected = {
            key: value for key, value in choice.items() if key not in {"intent", "reviewAfter", "initiativeDecisions"}
        }
        bookkeeping = {"epoch", "revision", "requestId"} | (
            {"reviewId"} if selected.get("kind") == "END_TURN" else set()
        )
        if {key: value for key, value in command.items() if key not in bookkeeping} != selected:
            native_domain_valid = False
    checks = {
        "upstreamPersistence": bool(persistence_report["linkedNativeToolEffects"]),
        "scriptedModelOnly": request.selection.model == "scripted-v1",
        "scopeMatches": (result.game, result.player) == (request.game, request.player),
        "requestedHandoffs": result.handoffs == request.turns == kinds["END_TURN"],
        "nativePositionMatches": checkpoint["handoff"] == result.handoffs and game.get("turns", 0) == result.turn,
        "rolesCompleted": bool(result.roles) and all(role.status == "COMPLETED" for role in result.roles),
        "nativeReceiptsMatch": bool(commands)
        and len(commands) == len(native) == len(native_rows)
        and native_domain_valid
        and confirmed
        and communication_confirmed,
        "reviewsPublished": not result.workflow_links
        or (
            bool(result.reviews)
            and len(review_rows) == len(result.reviews)
            and all(row[0] in {"COMPLETED", "UNAVAILABLE"} for row in review_rows)
        ),
        "reviewPermitsReady": not result.workflow_links
        or (len(permits) == request.turns and all(permit["state"] in {"READY", "UNAVAILABLE"} for permit in permits)),
        "eventNotificationsSettled": pending_notifications == 0,
        "communicationAcrossHandoff": communication_checks,
        "reviewPublicationRecovery": review_checks,
        "roleWaitRecovery": wait_checks,
        "advisoryBrokerRecovery": advisory_checks,
        "routingRecovery": routing_report["verified"],
        "nativeStores": stores_report["verified"],
        "jobsFinalized": unfinished_jobs == 0,
        "jobsRetired": len(heads) == len(result.roles)
        and archives_valid
        and {digest(head["identity"]) for head in heads}
        == {
            digest({"scope": role.scope.model_dump(mode="json"), "job": role.job, "purpose": role.purpose})
            for role in result.roles
        },
        "contextsCompacted": journal_contexts == 0
        and len(turns) == request.turns
        and all(
            turn.get("lifecycleFinalized") and not turn.get("bootstraps") and "roleContext" not in turn
            for turn in turns
        ),
        "reservationsSettled": unfinished == 0 and unfinished_domain_calls == 0,
        "generationBudgetRetained": attempts
        == sum(role.usage["requests"] for role in result.roles)
        + sum(review.usage["requests"] for review in result.reviews)
        + sum(phase.usage["requests"] for peace in result.peace for phase in peace.phases),
        "restartObserved": host_checks if host_required else restarted if restart_required else True,
        "historiesArchived": len(list((root / "results").glob("history-*.json")))
        == 1 + len(result.roles) + len(result.workflow_links),
        "historiesReplayed": json.loads((root / "control-wait.log").read_text()).get("historyReplay") == "passed",
        "hardCrashRecovery": crash_checks,
        "hostRestartRecovery": host_checks,
    }
    report = {
        "version": 1,
        "verified": all(checks.values()),
        "scope": "bounded_scripted_local_game",
        "handoffs": result.handoffs,
        "turn": result.turn,
        "roleRuns": len(result.roles),
        "reviewReports": len(result.reviews),
        "communicationJobs": sum(role.purpose == "communication" for role in result.roles),
        "communicationEffects": len(communication_requests),
        "duplicateSignals": result.communication_duplicates,
        "completedRoleWaits": completed_waits,
        "advisoryWorkflows": len(result.advisories),
        "deliveredAdvisoryQuestions": delivered_advisories,
        "simulatedBrokerRequests": broker_requests,
        "simulatedGenerations": attempts,
        "realProviderCalls": 0,
        **({"nativeStores": stores_report} if manifest.get("storesTest") else {}),
        **{key: value for key, value in routing_report.items() if key != "verified"},
        **persistence_report,
        "servicesRestarted": restarted
        or (host_required and host_checks)
        or (communication_test and communication_checks)
        or (review_test and review_checks)
        or (wait_test and wait_checks)
        or (advisory_test and advisory_checks)
        or (routing_test and routing_report["verified"]),
        "routingServicesRestarted": routing_test and routing_report["verified"],
        "independentHostRestart": host_required and host_checks,
        "hardCrashPoints": len(crashes)
        + int(communication_test)
        + int(review_test)
        + 2 * int(wait_test)
        + int(advisory_test)
        + 2 * int(routing_test),
        "retiredJobs": len(heads),
        "nativeCommands": dict(kinds),
        "nativeCommandsWithoutPythonJournal": len(native_decisions),
        "trustedNativeHandoffs": 0,
        "checks": checks,
        "hostImage": manifest["hostImage"],
        "workerImage": manifest["workerImage"],
    }
    write_private_json(root / "results/verification.json", report)
    if not report["verified"]:
        raise ValueError(
            "ISOLATED_GAME_VERIFICATION_FAILED:" + ",".join(key for key, value in checks.items() if not value)
        )
    return report
