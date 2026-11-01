"""Durable, host-dispatched decisions. No generated code is ever evaluated."""

import asyncio
import copy
import hashlib
import json
import secrets

from jsonschema import ValidationError as SchemaValidationError
from pydantic import ValidationError
from sqlalchemy import insert, select
from sqlalchemy.exc import SQLAlchemyError

from civ_agent import memory_schema as tables
from civ_agent.civilization_state import StateConflict, receipt_is_uncertain
from civ_agent.decision_contracts import DecisionContract
from civ_agent.model_status import error_fields
from civ_agent.observations import required_detail
from civ_agent.protocol import JsonObject
from civ_agent.response_compaction import expand_response
from civ_agent.strict_schema import encoded, fingerprint
from civ_agent.tool_failures import STATE_ERRORS, category

EVENT_FIELDS = (
    "usage",
    "cumulativeUsage",
    "requestFailure",
    "modelFailure",
    "providerRequests",
    "providerRejections",
    "generationStopped",
)
GENERATION_RECOVERY_LIMIT = "STRUCTURED_GENERATION_RECOVERY_LIMIT"


class GenerationRecoveryError(ValueError):
    """A bounded generation recovery cannot safely continue."""


def journal(memory, identifier, body=None, *, event=False):
    with memory.transaction() as db:
        value = db.execute(
            select(tables.decision_journal.c.body).where(tables.decision_journal.c.id == identifier)
        ).scalar_one_or_none()
        prior = json.loads(value) if value is not None else None
        if body is not None:
            if prior is not None:
                if event:
                    body = {
                        **prior,
                        **{k: v for k, v in body.items() if k in EVENT_FIELDS or prior["status"] == "generating"},
                    }
                else:
                    body = {**body, **{k: prior[k] for k in EVENT_FIELDS if k in prior}}
                    # A failure can arrive before turn/start's caller binds the
                    # turn. Binding must not revive that rejected response.
                    if prior["status"] == "generation_failed" and body["status"] == "generating":
                        body.update({k: prior[k] for k in ("status", "category", "error") if k in prior})
            db.execute(
                insert(tables.decision_journal)
                .values(
                    id=identifier,
                    job=body["job"],
                    epoch=body["epoch"],
                    purpose=body["purpose"],
                    body=encoded(body),
                )
                .prefix_with("OR REPLACE")
            )
            return body
        return prior


def reserve_generation_retry(memory, failed_id, replacement):
    """Persist one retry per job/epoch before admitting another model request."""
    with memory.transaction() as db:
        current_epoch = memory._meta(db, "epoch")
        # Opening reviews may run before gameplay first binds the notebook.
        # Their controller fences report publication; a bound epoch must match.
        if current_epoch != replacement["epoch"] and not (current_epoch is None and replacement["purpose"] == "review"):
            raise StateConflict("DECISION_CONTEXT_EXPIRED")
        rows = {
            row["id"]: row
            for body in db.scalars(
                select(tables.decision_journal.c.body).where(
                    tables.decision_journal.c.job == replacement["job"],
                    tables.decision_journal.c.epoch == replacement["epoch"],
                )
            )
            for row in [json.loads(body)]
        }
        failed = rows.get(failed_id, {})
        if (
            failed.get("status") != "generation_failed"
            or failed.get("error") != "REVIEW_OUTPUT_STALLED"
            or failed.get("requestFailure", {}).get("reason") != "REVIEW_OUTPUT_STALLED"
            or not failed.get("generationStopped")
            or failed.get("thread") != replacement["thread"]
            or "decision" in failed
            or failed.get("receipts")
        ):
            raise GenerationRecoveryError("GENERATION_RECOVERY_UNSAFE")
        if any(row.get("retryOf") for row in rows.values()):
            raise GenerationRecoveryError(GENERATION_RECOVERY_LIMIT)
        # A crash after this commit consumes the allowance even if no provider
        # response is received. Never refund an uncertain request.
        replacement = {**replacement, "retryOf": failed_id, "repair": True}
        db.execute(
            insert(tables.decision_journal).values(
                id=replacement["id"],
                job=replacement["job"],
                epoch=replacement["epoch"],
                purpose=replacement["purpose"],
                body=encoded(replacement),
            )
        )
        return replacement


def continuation(result):
    return {"input": [], "toolOutput": {"name": "decision_receipts", "output": encoded(result), "retain": True}}


def capture_answer(answers, params):
    """A late unphased message must not replace an explicit final answer."""
    item, turn = params.get("item", {}), params.get("turnId")
    if item.get("type") != "agentMessage":
        return
    if item.get("phase") == "final_answer":
        answers[turn] = item.get("text", "")
        answers[(turn, "final")] = True
    elif item.get("phase") is None and not answers.get((turn, "final")):
        answers[turn] = item.get("text", "")


def completed_call(memory, job, identity):
    """Look up a prior receipt without registering an unexecuted operation."""
    with memory.transaction() as db:
        return (
            db.scalar(select(tables.calls.c.result).where(tables.calls.c.job == job, tables.calls.c.id == identity))
            is not None
        )


def check_epoch(memory, epoch):
    with memory.transaction() as db:
        if memory._meta(db, "epoch") != epoch:
            raise StateConflict("DECISION_CONTEXT_EXPIRED")


async def generation_event(gateway, record, method, params):
    """Correlate sanitized provider notices and actual usage with their request."""
    turn = params.get("turn", {}).get("id") if method == "turn/completed" else params.get("turnId")
    if params.get("threadId") not in {None, record.get("threadId")}:
        return
    identity = record.get("decisionTurns", {}).get(turn, record.get("activeDecision"))
    if not identity:
        return
    saved = await gateway.io.call(journal, gateway.memory, identity)
    if saved is None:
        return
    if turn and saved.get("turn") not in {None, turn}:
        return
    changes: JsonObject = {}
    if method == "service/providerRequest":
        requests = dict(saved.get("providerRequests", {}))
        requests[params["requestId"]] = params["kind"]
        changes["providerRequests"] = requests
        record.setdefault("providerRequests", {})[params["requestId"]] = params["kind"]
    elif method == "service/providerRejection":
        rejection = {key: params[key] for key in ("reason", "httpStatus", "retryRequestId") if key in params}
        changes["providerRejections"] = {**saved.get("providerRejections", {}), params["requestId"]: rejection}
        record.setdefault("providerRejections", {})[params["requestId"]] = rejection
    elif method == "thread/tokenUsage/updated":
        if saved.get("turn") not in {None, params.get("turnId")}:
            return
        usage = params.get("tokenUsage", {})
        changes["cumulativeUsage"] = {
            name: value
            for name, value in usage.get("total", {}).items()
            if name in {"inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens", "totalTokens"}
            and type(value) is int
            and value >= 0
        }
        # `last` covers only the last provider response, not automatic
        # continuations inside one app-server turn. Difference thread totals
        # against the durable baseline for this host decision.
        changes["usage"] = {
            name: max(0, value - saved.get("usageBaseline", {}).get(name, 0))
            for name, value in changes["cumulativeUsage"].items()
        }
        if not changes["usage"]:
            changes["usage"] = {
                k: v
                for k, v in usage.get("last", {}).items()
                if k in {"inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens", "totalTokens"}
                and type(v) is int
                and v >= 0
            }
        thread_usage = record.setdefault("structuredUsageByThread", {}).setdefault(params.get("threadId"), {})
        for key, value in changes["cumulativeUsage"].items():
            thread_usage[key] = max(thread_usage.get(key, 0), value)
    elif method in {
        "service/requestFailed",
        "service/transportFailure",
        "service/approvalRequired",
        "service/generationFailed",
    }:
        # A disconnect while draining a completed response does not undo its
        # receipts. Preserve both execution status and transport evidence.
        if saved["status"] == "generating":
            reason = params.get("reason", "PROVIDER_REQUEST_FAILED")
            changes.update(
                status="generation_failed",
                category="request_budget"
                if reason in {"PROVIDER_DECISION_REQUEST_LIMIT", "MODEL_REQUEST_BUDGET_EXHAUSTED"}
                else "generation"
                if reason in {"REVIEW_OUTPUT_STALLED", "REVIEW_OUTPUT_LIMIT"}
                else "transport",
                error=reason,
            )
            if method == "service/generationFailed" and reason == "REVIEW_OUTPUT_STALLED":
                failure = {"decisionId": identity, "turnId": turn, "reason": reason}
                record["generationFailure"] = failure
                record.setdefault("generationFailures", {})[identity] = failure
        changes["requestFailure"] = {
            k: params[k]
            for k in (
                "rpcMethod",
                "rpcCode",
                "reason",
                "requestId",
                "stage",
                "exceptionType",
                "forwardedBytes",
                "upstreamStatus",
            )
            if k in params
        }
    elif method == "turn/completed":
        if isinstance(params.get("turn", {}).get("error"), dict):
            changes["modelFailure"] = error_fields(params["turn"]["error"])
            if (
                saved["status"] == "generating"
                and params["turn"].get("status") == "failed"
                and changes["modelFailure"]["reason"] == "promptPolicyRejected"
            ):
                # The transport's one retry is exhausted or unavailable. Outer
                # specialist/review restarts must not multiply that allowance.
                record.setdefault("error", "PROMPT_POLICY_REJECTED")
        if (
            saved["status"] == "generation_failed"
            and saved.get("error") == "REVIEW_OUTPUT_STALLED"
            and params.get("turn", {}).get("status") in {"completed", "interrupted"}
        ):
            changes["generationStopped"] = True
        elif params.get("turn", {}).get("status") != "completed":
            changes.update(status="generation_failed", category="generation", error="INCOMPLETE_GENERATION")
    await gateway.io.call(journal, gateway.memory, identity, changes, event=True)


class DecisionLoop:
    def __init__(self, gateway, job, contract: DecisionContract, record):
        self.gateway, self.job, self.contract, self.record = gateway, job, contract, record
        self.current = None
        self.review = None
        self.review_targets = set()
        self.delivered_revision = (job.observation or {}).get("revision")
        self.delivered_access = self._access()
        self.repairs = 0

    @property
    def generation_failed(self):
        return self.record.get("generationFailure", {}).get("decisionId") == (self.current or {}).get("id") and bool(
            self.current
        )

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

    async def begin(self, message):
        failure = self.record.get("generationFailure")
        # A restarted controller may have lost the last response after commit.
        # Reconcile its identities before requesting any replacement decision.
        if self.current is None and self.record.get("activeDecision"):
            saved = await self.gateway.io.call(journal, self.gateway.memory, self.record["activeDecision"])
            if (
                saved
                and saved["job"] == self.job.id
                and saved["epoch"] == self.job.epoch
                and saved.get("thread") == self.record.get("threadId")
            ):
                # The journal commits usage before the controller's record.
                # Recover that baseline if a crash happened between those writes.
                baseline = self.record.setdefault("structuredUsageByThread", {}).setdefault(
                    self.record.get("threadId"), {}
                )
                for key, value in saved.get("cumulativeUsage", {}).items():
                    baseline[key] = max(baseline.get(key, 0), value)
                if saved.get("status") == "generation_failed" and saved.get("error") == "REVIEW_OUTPUT_STALLED":
                    failure = {"decisionId": saved["id"], "turnId": saved.get("turn"), "reason": saved["error"]}
                    self.record["generationFailure"] = failure
                    self.record.setdefault("generationFailures", {})[saved["id"]] = failure
                    self.delivered_access = copy.deepcopy(saved["deliveredAccess"])
                    self.delivered_revision = saved.get("observationRevision")
            if (
                saved
                and saved["job"] == self.job.id
                and saved["epoch"] == self.job.epoch
                and saved.get("status")
                in {"validated", "dispatching", "uncertain", "completed", "partial", "reported", "rejected"}
            ):
                self.current = saved
                recovered = await self.execute(recovery=True)
                message = (
                    (message + "\nRecovered decision receipts:\n" + encoded(recovered))
                    if isinstance(message, str)
                    else {"context": message, "recoveredDecision": recovered}
                )
        if failure is not None:
            # Keep the last delivered receipts/context in the conversation. This
            # feedback replaces only the failed response, never a confirmed action.
            message = {
                "error": failure["reason"],
                "failedDecisionId": failure["decisionId"],
                "applied": False,
                "instruction": (
                    "The previous response was discarded after excessive whitespace; no operation from it was "
                    "dispatched. Return a complete compact JSON decision. Include every required key, using null "
                    "for omitted optional arguments. Use the delivered context and confirmed receipts; never "
                    "repeat a confirmed action."
                ),
                **({"context": message} if not isinstance(message, str) else {}),
            }
        identifier = "decision:" + secrets.token_hex(16)
        self.current = {
            "id": identifier,
            "job": self.job.id,
            "epoch": self.job.epoch,
            "purpose": self.contract.purpose,
            "modelPhase": self.record.get("modelPhase", self.contract.purpose),
            "thread": self.record.get("threadId"),
            "usageBaseline": copy.deepcopy(
                self.record.get("structuredUsageByThread", {}).get(self.record.get("threadId"), {})
            ),
            "turn": None,
            "status": "generating",
            "schema": self.contract.costs(),
            "observationRevision": self.delivered_revision,
            "deliveredAccess": copy.deepcopy(self.delivered_access),
            "gameTurn": self.record.get("gameTurn"),
            "model": self.record.get("effectiveSettings", self.record.get("settings")),
            "inputCharacters": len(message) if isinstance(message, str) else len(encoded(message)),
            "instructionCharacters": self.record.get("startup", {}).get("instructionCharacters"),
            "initialObservationCharacters": self.record.get("startup", {}).get("userCharacters"),
            "repair": self.repairs > 0,
        }
        if failure is not None:
            self.current = await self.gateway.io.call(
                reserve_generation_retry, self.gateway.memory, failure["decisionId"], self.current
            )
            self.record.pop("generationFailure", None)
            self.record["generationRetries"] = self.record.get("generationRetries", 0) + 1
        else:
            await self.save()
        self.record["decisionSchema"] = self.contract.costs()
        self.record["activeDecision"] = identifier
        self.record["structuredRequests"] = self.record.get("structuredRequests", 0) + 1
        return {
            **({"input": [{"type": "text", "text": message}]} if isinstance(message, str) else continuation(message)),
            "outputSchema": self.contract.schema,
        }

    async def bind(self, turn):
        assert self.current is not None
        self.current["turn"] = turn
        self.record.setdefault("decisionTurns", {})[turn] = self.current["id"]
        await self.save()

    async def reject_report(self, code):
        assert self.current is not None
        self.current.update(status="report_rejected", category="semantic", error=code)
        self.repairs += 1
        self.record["decisionRepairs"] = self.record.get("decisionRepairs", 0) + 1
        await self.save()

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

    async def complete(self, text, status="completed"):
        assert self.current is not None
        if self.generation_failed:
            raise GenerationRecoveryError("GENERATION_RESPONSE_DISCARDED")
        self.current.update(outputCharacters=len(text), outputSha256=hashlib.sha256(text.encode()).hexdigest())
        if status != "completed":
            self.current.update(status="generation_failed", category="generation", error="INCOMPLETE_GENERATION")
            await self.save()
            raise ValueError("INCOMPLETE_GENERATION")
        try:
            decision = self.contract.parse(text)
        except (ValueError, ValidationError, SchemaValidationError) as error:
            self.repairs += 1
            self.record["decisionRepairs"] = self.record.get("decisionRepairs", 0) + 1
            feedback: JsonObject = {
                "error": "INVALID_STRUCTURED_DECISION",
                "applied": False,
                "instruction": "Return a complete decision matching the current schema. No operation was dispatched.",
            }
            if isinstance(error, ValidationError):
                feedback["issues"] = [
                    {"path": list(e["loc"]), "type": e["type"]}
                    for e in error.errors(include_input=False, include_context=False)
                ][:12]
            elif isinstance(error, SchemaValidationError):
                feedback["constraint"] = error.validator
            self.current.update(status="validation_failed", category="arguments", result=feedback)
            await self.save()
            if self.repairs > 2:
                raise ValueError("STRUCTURED_DECISION_REPAIR_LIMIT") from None
            return feedback
        self.repairs = 0
        self.current.update(status="validated", decision=decision)
        await self.save()
        return await self.execute()

    async def execute(self, *, recovery=False):
        """Replay durable children; pending native requests are reconciled before retry."""
        assert self.current is not None
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
                    result = {"error": "MILITARY_REVIEW_NOT_DELIVERED", "applied": False}
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
                if stop:
                    results.append({"operationId": identity, "status": "unexecuted", "reason": stop})
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
                            else self._prerequisite(name, arguments, access, index)
                            if kind == "act"
                            else None
                        )
                        if stop:
                            results.append({"operationId": identity, "status": "unexecuted", "reason": stop})
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
