"""Optional memory/review triage and separate batched city recommendations."""

import asyncio

import aiohttp

from civ_agent.advisory_answers import failed_answers
from civ_agent.advisory_policy import AdvisoryPolicy
from civ_agent.decisions_backends import BACKENDS
from civ_agent.decisions_tasks import (
    acceptance_threshold,
    request_body,
    request_id,
    request_metadata,
    validate_result,
)
from civ_agent.protocol import JsonObject


class CheckpointInterrupted(Exception):
    """The broker owns any submitted request; its durable ID can be reconciled."""


class AdvisoryRuntime(AdvisoryPolicy):
    def __init__(self, gateway, client, settings):
        super().__init__(gateway, settings)
        self.client = client
        self.requests: set[asyncio.Task] = set()

    def suspend(self):
        for task in self.requests:
            task.cancel()

    async def request(self, operation, *args, **kwargs):
        if getattr(self.gateway, "checkpoint_requested", False):
            raise CheckpointInterrupted()
        task = asyncio.create_task(operation(*args, **kwargs))
        self.requests.add(task)
        try:
            return await task
        except asyncio.CancelledError:
            owner = asyncio.current_task()
            if getattr(self.gateway, "checkpoint_requested", False) and owner is not None and not owner.cancelling():
                raise CheckpointInterrupted() from None
            raise
        finally:
            self.requests.discard(task)

    async def evaluate(self, evidence, *, task=None):
        """Persist attempts before I/O; only receipt lookup can reconcile lost replies."""
        try:
            stable_id = request_id(evidence, model=BACKENDS[self.settings.decisions_backend].model)
        except (ValueError, TypeError, KeyError):
            return {"answers": [], "rawAnswers": [], "questionResults": [], "fallback": "input_unavailable"}
        state = await self.state()
        attempted = state.get("attempts", {})
        previous = attempted.get(stable_id)
        unresolved = {"broker_unavailable", "malformed_broker_output", "completion_unknown"}
        if previous is not None and previous.get("fallback") not in unresolved:
            return previous
        reconciliations = state.get("reconciliations", {})
        if previous is not None and reconciliations.get(stable_id, 0) >= 1:
            return previous
        try:
            body = request_body(evidence, self.settings)
        except (ValueError, TypeError, KeyError):
            result = {
                "answers": [],
                "rawAnswers": [],
                "questionResults": [],
                "fallback": "input_unavailable",
                "requestId": stable_id,
            }
        else:
            metadata = request_metadata(body, evidence)
            threshold = acceptance_threshold(evidence, self.settings)
            if self.settings.decisions_backend == "luna" and self.settings.additional_decisions_budget_usd == 0:
                result = {**metadata, **failed_answers(body, threshold, "budget_exhausted")}
            else:
                pending = {**metadata, **failed_answers(body, threshold, "completion_unknown")}
                values = {"attempts": {**attempted, stable_id: pending}}
                if previous is not None:
                    values["reconciliations"] = {**reconciliations, stable_id: 1}
                await self.save(**values)
                raw: JsonObject
                try:
                    if previous is None:
                        raw = await self.request(
                            self.client.batch, evidence, timeout=self.settings.decisions_timeout_seconds
                        )
                    else:
                        raw = await self.request(
                            self.client.lookup, stable_id, timeout=self.settings.decisions_timeout_seconds
                        )
                        if (
                            isinstance(raw, dict)
                            and raw.get("requestId") == stable_id
                            and raw.get("fallback") == "not_submitted"
                        ):
                            # A second delivery of the same immutable request is
                            # serialized and deduplicated by the broker ledger.
                            raw = await self.request(
                                self.client.batch, evidence, timeout=self.settings.decisions_timeout_seconds
                            )
                except CheckpointInterrupted:
                    raw = {**metadata, **failed_answers(body, threshold, "completion_unknown")}
                except (TimeoutError, OSError, ValueError, aiohttp.ClientError):
                    raw = {"answers": [], "fallback": "broker_unavailable", "requestId": stable_id}
                if (
                    isinstance(raw, dict)
                    and raw.get("requestId") == stable_id
                    and raw.get("fallback") == "broker_unavailable"
                ):
                    result = {**metadata, **failed_answers(body, threshold, "broker_unavailable")}
                else:
                    result = validate_result(body, evidence, raw, threshold)
                if previous is not None:
                    result["brokerReconciliation"] = True
        state = await self.state()
        if self.gateway.accounting is not None:
            await self.gateway.io.call(self.gateway.accounting.classifier, "advisory_batch", result)
        await self.save(
            attempts={**state.get("attempts", {}), stable_id: result},
            accounting=result.get("accounting", state.get("accounting")),
        )
        return result
