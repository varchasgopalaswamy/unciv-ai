"""Transport-independent domain batches with delivered-evidence guards and receipts."""

import asyncio
import copy
import hashlib
import json

from sqlalchemy.exc import SQLAlchemyError

from civ_agent import memory_schema as tables
from civ_agent.civilization_state import StateConflict, receipt_is_uncertain
from civ_agent.decision_contracts import DecisionContract
from civ_agent.decision_journal import check_epoch, completed_call, journal
from civ_agent.execution_windows import describe_stale, focused_stop_reason
from civ_agent.observations import required_detail
from civ_agent.protocol import JsonObject
from civ_agent.response_compaction import expand_response
from civ_agent.strict_schema import fingerprint
from civ_agent.tool_failures import STATE_ERRORS, category


class BatchExecutor:
    def __init__(self, gateway, job, contract: DecisionContract):
        self.gateway, self.job, self.contract = gateway, job, contract
        self.current = None
        self.review = None
        self.review_targets = set()
        self.delivered_revision = (job.observation or {}).get("revision")
        self.delivered_access = self._access()

    def _access(self) -> JsonObject:
        view = self.job.observation or {}
        return {
            "revision": view.get("revision"),
            "details": [detail.model_dump() for detail in self.job.details_read],
            "warVersions": copy.deepcopy(self.job.war_versions),
            "memoryVersions": copy.deepcopy(self.job.memory_versions),
            "messages": sorted(self.job.seen_messages),
            "trades": {key: fingerprint(value) for key, value in self.job.trades.items()},
            "entities": {
                row["handle"]: fingerprint(row)
                for row in [*view.get("cities", []), *(view.get("map") or {}).get("units", [])]
                if "handle" in row
            },
        }

    async def save(self):
        assert self.current is not None
        self.current = await self.gateway.io.call(journal, self.gateway.memory, self.current["id"], self.current)
        from civ_agent.intent_handoffs import record_action_attempts

        record_action_attempts(
            self.job,
            self.current.get("dispatch", []),
            self.current.get("receipts", []),
            decision_id=self.current["id"],
        )

    def delivered(self, result, *, access=None):
        """Called only when a response is about to become model context."""
        result = expand_response(result)
        review = result.get("militaryCompletion")
        if review is not None:
            if self.review is None or self.review.get("basis") != review.get("basis"):
                self.review_targets.clear()
            self.review = copy.deepcopy(review)
            self.review_targets.update(item["target"] for item in review.get("items", []))
        for receipt in result.get("receipts", []):
            if isinstance(receipt.get("result"), dict):
                snapshot = (self.current or {}).get("receiptAccess", {}).get(receipt.get("operationId"))
                self.delivered(receipt["result"], access=snapshot)
        if "receipts" in result:
            return
        for row in result.get("results", []):
            if isinstance(row.get("result"), dict):
                self.delivered(row["result"], access=access)
        snapshot = access if access is not None else self._access()
        # Hidden host refreshes and unrelated receipts cannot grant credit.
        # Invalidate old credits; add only the inspection in this response.
        details = [detail for detail in self.delivered_access["details"] if detail in snapshot["details"]]
        query = result.get("query")
        if query in snapshot["details"] and query not in details:
            details.append(copy.deepcopy(query))
        self.delivered_access["details"] = details
        if "stateId" in result or "stateUpdate" in result:
            self.delivered_revision = snapshot["revision"]
            self.delivered_access.update(revision=snapshot["revision"], entities=copy.deepcopy(snapshot["entities"]))
        if (
            "economyCoordination" in result
            or "coordinationUpdate" in result
            or "warState" in result
            or "episodes" in result
            or result.get("section") == "episodes"
        ):
            self.delivered_access["warVersions"] = copy.deepcopy(snapshot["warVersions"])
        if result.get("trade"):
            # A trade read or receipt carries the exact inspected proposal.
            reference = result["trade"].get("proposalId")
            if reference in snapshot["trades"]:
                self.delivered_access["trades"][reference] = snapshot["trades"][reference]
        if any(key in result for key in ("memoryUpdates", "memoryDelivery", "changes", "records")):
            self.delivered_access["memoryVersions"] = copy.deepcopy(snapshot["memoryVersions"])
        messages = {
            event["body"]["message"]
            for event in [*result.get("messages", []), *result.get("events", [])]
            if isinstance(event, dict) and isinstance(event.get("body"), dict) and event["body"].get("message")
        }
        messages.update(
            message["message"]
            for thread in result.get("conversationContext", {}).get("threads", [])
            for message in thread.get("messages", [])
        )
        self.delivered_access["messages"] = sorted(set(self.delivered_access["messages"]) | messages)

    async def execute(self, *, recovery=False):
        """Replay durable children; pending native requests are reconciled before retry."""
        assert self.current is not None
        if self.gateway.retiring_epoch(self.job.epoch):
            return await self.receipts_only()
        if self.current["job"] != self.job.id or self.current["epoch"] != self.job.epoch:
            raise StateConflict("DECISION_CONTEXT_EXPIRED")
        decision = self.current["decision"]
        kind = decision["kind"]
        if kind == "submit_report":
            # Pure report validation may finish while a checkpoint holds
            # publication. Its controller fences publication against rewind.
            result = {"report": decision["report"]}
            self.current.update(status="reported", result=result)
            await self.save()
            return result
        self.gateway.check(self.job, ended_allowed=True)
        await self.gateway.io.call(check_epoch, self.gateway.memory, self.job.epoch)
        if self.current.get("result") is not None:
            return copy.deepcopy(self.current["result"])
        if kind == "finish" and self.job.purpose == "communication":
            self.job.role_finished.set()
            self.current.update(status="completed", result={"roleFinished": True})
            await self.save()
            return self.current["result"]
        operations = copy.deepcopy(
            self.current.get("dispatch")
            or decision.get(
                "operations",
                [
                    {
                        "tool": "finish_role" if kind == "finish" else "economy_wait",
                        "arguments": decision.get("arguments", {}),
                    }
                ],
            )
        )
        if kind == "finish" and self.job.purpose == "military" and "dispatch" not in self.current:
            arguments = operations[0]["arguments"]
            decisions = arguments.pop("decisions", [])
            if decisions:
                targets = {t for d in decisions for t in d["targets"]}
                if self.review is None or not targets <= self.review_targets:
                    result = {
                        "error": "MILITARY_REVIEW_NOT_DELIVERED",
                        "applied": False,
                        "recoverable": True,
                        "deliveredTargets": sorted(self.review_targets),
                        "undeliveredTargets": sorted(targets - self.review_targets),
                        "recovery": {
                            "instruction": "Submit hold decisions only for targets in the delivered military completion review. "
                            "Read military_review and any remaining pages before including an undelivered target. "
                            "If a target is absent from that review, omit it from decisions; other deliberate holds can be "
                            "explained in the summary. Do not repeat confirmed actions."
                        },
                    }
                    if self.review is not None and self.review.get("basis") is not None:
                        result["militaryCompletion"] = copy.deepcopy(self.review)
                    self.current.update(status="rejected", result=result)
                    await self.save()
                    return result
                arguments["militaryReview"] = {"basis": self.review["basis"], "decisions": decisions}
        ids = [self.current["id"] + ":" + str(i) for i in range(len(operations))]
        self.current.update(status="dispatching", operationIds=ids, dispatch=operations)
        await self.save()  # All child identities exist before the first dispatch.
        results = []
        previous = {r["operationId"]: r for r in self.current.get("receipts", []) if r["status"] != "unexecuted"}
        access = self.current["deliveredAccess"]
        self.job.delivered_messages = set(access.get("messages", []))
        stop = None
        async with self.gateway.admit(review=self.job.review_inputs is not None):
            for index, (operation, identity) in enumerate(zip(operations, ids, strict=True)):
                if self.gateway.retiring_epoch(self.job.epoch):
                    self.current["receipts"] = results
                    return await self.receipts_only()
                if stop:
                    results.append(
                        {
                            "operationId": identity,
                            "status": "unexecuted",
                            "reason": stop,
                            **(
                                {"feedback": describe_stale(self.job, access.get("revision"))}
                                if stop == "FRESH_OBSERVATION_REQUIRED"
                                else {}
                            ),
                        }
                    )
                    continue
                name, arguments = operation["tool"], operation["arguments"]
                try:
                    confirmed = identity in previous
                    if self.job.review_inputs is None and not confirmed:
                        await self._reconcile(identity)
                        confirmed = (
                            await self.gateway.io.call(completed_call, self.gateway.memory, self.job.id, identity)
                            or await self.gateway.io.call(self.gateway.memory.call_receipt, self.job.id, identity)
                            is not None
                        )
                    if not confirmed:
                        stop = (
                            "RECOVERY_REQUIRES_DECISION"
                            if recovery
                            else focused_stop_reason() or self._prerequisite(name, arguments, access, index)
                            if kind == "act"
                            else None
                        )
                        if stop:
                            results.append(
                                {
                                    "operationId": identity,
                                    "status": "unexecuted",
                                    "reason": stop,
                                    **(
                                        {"feedback": describe_stale(self.job, access.get("revision"))}
                                        if stop == "FRESH_OBSERVATION_REQUIRED"
                                        else {}
                                    ),
                                }
                            )
                            continue
                    result = (
                        previous[identity]["result"]
                        if identity in previous
                        else await self.gateway._admitted_call(self.job, name, arguments, identity, source="tool")
                    )
                    # Preserve the gateway's exact delivery representation,
                    # including message-local sharing and explicit full reads.
                    # delivered() expands each child for host bookkeeping.
                except BaseException as error:
                    # Neither cancellation nor a lost response permits new action IDs.
                    code = str(error) if isinstance(error, StateConflict) else ""
                    known_conflict = code in STATE_ERRORS | {"DECISION_EXECUTION_UNCERTAIN"}
                    self.current.update(
                        status="uncertain",
                        category=category(code)
                        if known_conflict
                        else "state_store"
                        if isinstance(error, SQLAlchemyError)
                        else "host"
                        if isinstance(error, (ValueError, KeyError, TypeError))
                        else "transport",
                        error=code if known_conflict else type(error).__name__,
                        receipts=results,
                        uncertainOperation=identity,
                    )
                    await asyncio.shield(self.save())
                    raise
                rejected = "error" in result or any(
                    change.get("accepted") is False for change in result.get("changes", [])
                )
                applied = result.get("applied") is True or any(
                    change.get("accepted") is True for change in result.get("changes", [])
                )
                results.append(
                    {
                        "operationId": identity,
                        "tool": name,
                        "status": "confirmed" if applied or not rejected else "rejected",
                        "result": result,
                    }
                )
                if identity not in previous:
                    self.current.setdefault("receiptAccess", {})[identity] = self._access()
                self.current["receipts"] = results
                await self.save()
                if rejected:
                    stop = "OPERATION_REJECTED"
                elif result.get("stateUpdate", {}).get("kind") in {"unavailable", "turn_ended"}:
                    stop = "FRESH_OBSERVATION_REQUIRED"
                elif kind == "act" and name in {
                    "city_override",
                    "escort_assignment",
                }:
                    # A transfer changes authority, even when native state is
                    # unchanged. Notes and requests do not transfer authority.
                    stop = "PREREQUISITES_CHANGED"
                elif self.job.role_refresh_required or self.job.negotiation_pending.is_set():
                    stop = "POSITION_CHANGED"
        result = {
            "decisionId": self.current["id"],
            "receipts": results,
            "unexecuted": [r["operationId"] for r in results if r["status"] == "unexecuted"],
        }
        self.current.update(status="partial" if result["unexecuted"] else "completed", result=result)
        await self.save()
        return result

    async def receipts_only(self):
        """Finish an existing batch using only durable, already-admitted outcomes."""
        assert self.current is not None
        if not self.gateway.retiring_epoch(self.job.epoch):
            raise StateConflict("SOURCE_RETIREMENT_REQUIRED")
        await self.gateway.io.call(check_epoch, self.gateway.memory, self.job.epoch)
        if self.current.get("result") is not None:
            return copy.deepcopy(self.current["result"])
        operations = self.current.get("dispatch") or self.current["decision"].get("operations", [])
        previous = {
            row["operationId"]: row for row in self.current.get("receipts", []) if row["status"] != "unexecuted"
        }

        def project():
            results = []
            with self.gateway.memory.transaction() as db:
                for index, operation in enumerate(operations):
                    identity = self.current["id"] + ":" + str(index)
                    if identity in previous:
                        results.append(copy.deepcopy(previous[identity]))
                        continue
                    call = (
                        db.execute(
                            tables.calls.select().where(
                                tables.calls.c.job == self.job.id, tables.calls.c.id == identity
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if call is not None and (
                        call["name"] != operation["tool"] or json.loads(call["arguments"]) != operation["arguments"]
                    ):
                        raise StateConflict("CALL_CONFLICT")
                    request_id = "r" + hashlib.sha256((self.job.id + ":" + identity).encode()).hexdigest()[:24]
                    request = (
                        db.execute(
                            tables.outbox.select().where(
                                tables.outbox.c.id == request_id, tables.outbox.c.job == self.job.id
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if call is not None and call["result"] is not None:
                        value = json.loads(call["result"])
                    elif request is not None and request["result"] is not None:
                        value = json.loads(request["result"])
                        if call is not None:
                            db.execute(
                                tables.calls.update()
                                .where(
                                    tables.calls.c.job == self.job.id,
                                    tables.calls.c.id == identity,
                                    tables.calls.c.result.is_(None),
                                )
                                .values(result=json.dumps(value, ensure_ascii=False, separators=(",", ":")))
                            )
                    elif request is not None:
                        raise StateConflict("DECISION_EXECUTION_UNCERTAIN")
                    elif call is not None:
                        raise StateConflict("RECOVERY_LOCAL_CALL_UNSETTLED")
                    else:
                        results.append(
                            {"operationId": identity, "status": "unexecuted", "reason": "SOURCE_EPOCH_RETIRED"}
                        )
                        continue
                    results.append(
                        {
                            "operationId": identity,
                            "tool": operation["tool"],
                            "status": "rejected" if "error" in value else "confirmed",
                            "result": value,
                        }
                    )
            return results

        receipts = await self.gateway.io.call(project)
        result = {
            "decisionId": self.current["id"],
            "receipts": receipts,
            "unexecuted": [row["operationId"] for row in receipts if row["status"] == "unexecuted"],
        }
        self.current.update(status="partial" if result["unexecuted"] else "completed", receipts=receipts, result=result)
        await self.save()
        return result

    def _prerequisite(self, name, arguments, access, index):
        if (
            name in {"player_command", "war_goals", "military_operation"}
            and self.job.war_versions != access["warVersions"]
        ):
            return "PREREQUISITES_CHANGED"
        memory_ids = {
            change["id"] for change in arguments.get("changes", []) if name == "memory_update" and "id" in change
        }
        intent = arguments.get("intent") or {}
        memory_ids.update(intent[key] for key in ("cityPlan", "strategy") if intent.get(key))
        if any(self.job.memory_versions.get(key) != access["memoryVersions"].get(key) for key in memory_ids):
            return "FRESH_OBSERVATION_REQUIRED"
        if name == "military_operation" and self.current is not None:
            # Updating another note is independent; revising a note changed
            # earlier in this batch requires seeing its receipt first.
            targets = set(arguments.get("ids", [])) | set(arguments.get("supersedes", []))
            if arguments.get("id"):
                targets.add(arguments["id"])
            for operation in self.current["dispatch"][:index]:
                if operation["tool"] != name:
                    continue
                prior = operation["arguments"]
                changed = set(prior.get("ids", [])) | set(prior.get("supersedes", []))
                if prior.get("id"):
                    changed.add(prior["id"])
                if targets & changed:
                    return "PREREQUISITES_CHANGED"
        reference = (
            arguments.get("proposalId")
            if name == "trade_decide"
            else arguments.get("counterofferTo")
            if name == "trade_offer"
            else None
        )
        if reference and (
            reference not in access["trades"] or access["trades"][reference] != self._access()["trades"].get(reference)
        ):
            return "FRESH_INSPECTION_REQUIRED"
        if name != "player_command":
            return None
        required = required_detail(arguments)
        if required is not None and (
            required.model_dump() not in access["details"] or required not in self.job.details_read
        ):
            return "FRESH_INSPECTION_REQUIRED"
        if not index and (self.job.observation or {}).get("revision") != access["revision"]:
            return "FRESH_OBSERVATION_REQUIRED"
        if index:
            if self.job.observation is None or self.job.role_refresh_required:
                return "FRESH_OBSERVATION_REQUIRED"
            # A changed target requires another decision. A sibling action may
            # update the native revision, but cannot confer new inspection credit.
            target = arguments.get("target")

            if target and access["entities"].get(target) != self._access()["entities"].get(target):
                return "TARGET_CHANGED"
        return None

    async def _reconcile(self, identity):
        routed = getattr(getattr(self.gateway, "routing", None), "reconcile_call", None)
        if routed is not None and await routed(self.job, identity):
            return
        request_id = "r" + hashlib.sha256((self.job.id + ":" + identity).encode()).hexdigest()[:24]
        pending = await self.gateway.io.call(self.gateway.memory.pending)
        request = next((p for p in pending if p["id"] == request_id), None)
        if request is None:
            return
        payload = json.loads(request["payload"])
        if request["transport"] == "player_command":
            result = await self.job.connection.tool("player_receipt", payload)
        else:
            result = await self.job.connection.tool(
                "player_communication", {"op": "receipt", "epoch": self.job.epoch, "requestId": request_id}
            )
        if receipt_is_uncertain(result):
            raise StateConflict("DECISION_EXECUTION_UNCERTAIN")
        await self.gateway.io.call(self.gateway.memory.receipt, request_id, result)
