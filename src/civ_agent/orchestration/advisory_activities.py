"""Bounded broker exchanges: persist intent and reconcile the original request ID."""

import aiohttp

from civ_agent.advisory_answers import failed_answers
from civ_agent.decisions_backends import BACKENDS
from civ_agent.decisions_policy import DecisionsSettings
from civ_agent.decisions_tasks import (
    Evidence,
    acceptance_threshold,
    request_body,
    request_id,
    request_metadata,
    validate_result,
)
from civ_agent.orchestration.contracts import AdvisoryExchange, AdvisoryReply, AdvisoryResult, FinishAdvisory
from civ_agent.orchestration.store import JournalConflict


class AdvisoryServices:
    def __init__(self, store, broker, settings: DecisionsSettings, *, civilization, reply_checkpoint=None):
        self.store, self.broker, self.settings = store, broker, settings
        self.civilization, self.reply_checkpoint = civilization, reply_checkpoint

    def inputs(self, request):
        evidence = Evidence.model_validate(self.store.get(request.scope, request.evidence)).model_dump(
            exclude_none=True
        )
        policy = DecisionsSettings.model_validate(self.store.get(request.scope, request.policy))
        if (
            policy != self.settings
            or not policy.decisions_enabled
            or evidence["player"] != self.civilization
            or self.broker.backend != policy.decisions_backend
            or request.request_id != request_id(evidence, model=BACKENDS[policy.decisions_backend].model)
            or request.timeout_seconds != policy.decisions_timeout_seconds
        ):
            raise JournalConflict("ADVISORY_BINDING_CONFLICT")
        body = request_body(evidence, policy)
        return evidence, policy, body, acceptance_threshold(evidence, policy)

    async def exchange(self, operation: AdvisoryExchange) -> AdvisoryReply:
        worker, request = self, operation.request
        evidence, policy, body, threshold = worker.inputs(request)
        key = "broker:reconcile" if operation.reconcile else "broker:submit"
        encoded = operation.model_dump(mode="json")
        reserved, receipt = worker.store.reserve(request.scope, key, encoded)
        if receipt is not None:
            return AdvisoryReply.model_validate(worker.store.get(request.scope, receipt))
        metadata = request_metadata(body, evidence)
        if policy.decisions_backend == "luna" and policy.additional_decisions_budget_usd == 0:
            result = {**metadata, **failed_answers(body, threshold, "budget_exhausted")}
        else:
            try:
                if operation.reconcile or not reserved:
                    raw = await worker.broker.lookup(request.request_id, timeout=min(5, request.timeout_seconds))
                    if (
                        isinstance(raw, dict)
                        and raw.get("requestId") == request.request_id
                        and raw.get("fallback") == "not_submitted"
                    ):
                        # The broker conclusively has no reservation. Its immutable
                        # identity still deduplicates a concurrent original submit.
                        raw = await worker.broker.batch(
                            evidence, timeout=request.timeout_seconds, scope=request.scope, task=request.task
                        )
                else:
                    raw = await worker.broker.batch(
                        evidence, timeout=request.timeout_seconds, scope=request.scope, task=request.task
                    )
            except (TimeoutError, OSError, ValueError, aiohttp.ClientError):
                raw = {"requestId": request.request_id, "fallback": "broker_unavailable"}
            if (
                isinstance(raw, dict)
                and raw.get("requestId") == request.request_id
                and raw.get("fallback") == "broker_unavailable"
            ):
                result = {**metadata, **failed_answers(body, threshold, "broker_unavailable")}
            else:
                result = validate_result(body, evidence, raw, threshold)
        reply = AdvisoryReply(
            outcome=worker.store.put(request.scope, result),
            unresolved=result.get("fallback")
            in {"broker_unavailable", "malformed_broker_output", "completion_unknown"},
        )
        worker.store.complete(request.scope, key, encoded, reply.model_dump(mode="json"))
        if worker.reply_checkpoint is not None:
            await worker.reply_checkpoint(key, request)
        return reply

    async def finish(self, finished: FinishAdvisory) -> AdvisoryResult:
        worker, request = self, finished.request
        worker.inputs(request)
        key = "broker:reconcile" if finished.reconciled else "broker:submit"
        if worker.store.receipt(request.scope, key) != finished.reply.model_dump(mode="json"):
            raise JournalConflict("ADVISORY_RECEIPT_REQUIRED")
        outcome = worker.store.get(request.scope, finished.reply.outcome)
        if finished.reconciled:
            outcome["brokerReconciliation"] = True
        result = AdvisoryResult(
            scope=request.scope, request_id=request.request_id, outcome=worker.store.put(request.scope, outcome)
        )
        body = finished.model_dump(mode="json")
        worker.store.reserve(request.scope, "advisory-result", body)
        worker.store.complete(request.scope, "advisory-result", body, result.model_dump(mode="json"))
        return result
