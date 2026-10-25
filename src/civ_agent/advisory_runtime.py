"""Optional memory/review triage and separate batched city recommendations."""

import asyncio

import aiohttp

from civ_agent import city_decisions
from civ_agent.advisory_answers import failed_answers
from civ_agent.advisory_delivery import delivery_basis, finish_delivery
from civ_agent.advisory_review import current_review_binding
from civ_agent.advisory_store import (
    AdvisoryStore,
    filter_pending_memory,
    pending_memory_requests,
    source_attempt_key,
)
from civ_agent.decisions_backends import BACKENDS
from civ_agent.decisions_policy import EARLY_REVIEW_MIN_TURN
from civ_agent.decisions_tasks import (
    TASK_VERSION,
    acceptance_threshold,
    request_body,
    request_id,
    request_metadata,
    validate_result,
)
from civ_agent.protocol import JsonObject


class AdvisoryRuntime:
    def __init__(self, gateway, client, settings):
        self.gateway, self.client, self.settings = gateway, client, settings
        self.store = AdvisoryStore(gateway.memory)

    async def state(self):
        return await self.gateway.io.call(self.store.state)

    async def save(self, **values):
        await self.gateway.io.call(self.store.save, **values)

    async def evaluate(self, evidence):
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
                        raw = await self.client.batch(evidence, timeout=self.settings.decisions_timeout_seconds)
                    else:
                        raw = await self.client.lookup(stable_id, timeout=self.settings.decisions_timeout_seconds)
                        if (
                            isinstance(raw, dict)
                            and raw.get("requestId") == stable_id
                            and raw.get("fallback") == "not_submitted"
                        ):
                            # A second delivery of the same immutable request is
                            # serialized and deduplicated by the broker ledger.
                            raw = await self.client.batch(evidence, timeout=self.settings.decisions_timeout_seconds)
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

    async def record_source_attempts(self, evidence, result):
        state = await self.state()
        attempts = state.get("sourceAttempts", {})
        outcomes = {row["name"]: row for row in result.get("questionResults", [])}
        context = {k: evidence[k] for k in ("player", "objectives", "topics")}
        unresolved = {"broker_unavailable", "malformed_broker_output", "completion_unknown"}
        for field, prefixes in (
            ("retrieval", ("retrieval_include", "retrieval_priority")),
            ("preservation", ("preservation",)),
            ("beliefs", ("beliefs",)),
        ):
            for index, source in enumerate(evidence.get(field, [])):
                for prefix in prefixes:
                    row = outcomes.get(f"{prefix}:{index}", {})
                    status = (
                        result.get("fallback") if result.get("fallback") in unresolved else row.get("status", "missing")
                    )
                    key = source_attempt_key(prefix, source, **context, new_evidence=evidence.get("newEvidence", []))
                    attempts[key] = {
                        "status": status,
                        "requestId": result.get("requestId"),
                        "source": {"key": source["key"], "version": source["version"]},
                    }
        await self.save(sourceAttempts=attempts)

    async def startup(self, job, reads, topics):
        gateway = self.gateway
        if "advisories" in job.memory:
            if job.memory["advisories"].get("cityRecommendations"):
                await self.validate_city_delivery(job)
            return
        evidence = await gateway.io.call(self.store.freeze, job, topics, self.settings)
        evidence["version"] = TASK_VERSION
        state = await self.state()
        turn = job.memory.get("gameTurn") or 0
        gap = self.settings.decisions_startup_min_gap_turns
        memory_eligible = turn - state.get("memoryAttemptTurn", -gap) >= gap
        recovered_notices = []
        recovered_bindings = []
        if memory_eligible:
            identifiers = [
                identifier
                for identifier in pending_memory_requests(evidence, state)
                if identifier in state.get("memoryEvidence", {})
                and state.get("reconciliations", {}).get(identifier, 0) < 1
            ]
            for identifier in identifiers[:1]:
                original = state["memoryEvidence"][identifier]
                await self.save(memoryAttemptTurn=turn)
                recovered = await self.evaluate(original)
                await self.record_source_attempts(original, recovered)
                notice, _ = await gateway.io.call(self.store.apply, job, original, recovered)
                recovered_notices.append(notice)
                recovered_bindings.extend(
                    delivery_basis(
                        {k: v for k, v in original.items() if k not in {"cities", "cityContext"}},
                        recovered,
                    )
                )
                if recovered.get("fallback") not in {
                    "broker_unavailable",
                    "malformed_broker_output",
                    "completion_unknown",
                }:
                    latest = await self.state()
                    retained = latest.get("memoryEvidence", {})
                    retained.pop(identifier, None)
                    await self.save(memoryEvidence=retained)
            if identifiers:
                evidence = await gateway.io.call(self.store.freeze, job, topics, self.settings)
                evidence["version"] = TASK_VERSION
            state = await self.state()
            evidence = filter_pending_memory(evidence, state)
        if not memory_eligible:
            for field in ("retrieval", "preservation", "beliefs", "newEvidence"):
                evidence[field] = []
        active_gameplay = job.purpose in {"gameplay", "economy"} and (job.observation or {}).get("phase") == "ACTIVE"
        city_trigger = city_decisions.change_key(job, evidence["objectives"]) if active_gameplay else None
        city_changed = city_trigger != state.get("cityTrigger")
        unresolved_city = state.get("cityResult", {}).get("fallback") in {
            "broker_unavailable",
            "malformed_broker_output",
            "completion_unknown",
        }
        city_retry = (state.get("cityCaptureUnavailable", False) or unresolved_city) and turn - state.get(
            "cityAttemptTurn", -gap
        ) >= gap
        city_eligible = active_gameplay and (city_changed or city_retry)
        cities, city_context = [], {}
        reason = (
            "not_active_gameplay"
            if not active_gameplay
            else "disabled_budget"
            if self.settings.decisions_backend == "luna" and self.settings.additional_decisions_budget_usd == 0
            else "unchanged_city_decision"
        )
        capture_status: JsonObject = {"capturedCities": 0, "reason": reason}
        city_result: JsonObject = {"answers": []}
        if self.gateway.role_coordinator is not None and job.purpose != "economy":
            city_eligible = False
        if city_eligible and (
            self.settings.decisions_backend == "jev" or self.settings.additional_decisions_budget_usd > 0
        ):
            await self.save(cityAttemptTurn=turn, cityTrigger=city_trigger)
            try:
                async with asyncio.timeout(self.settings.decisions_timeout_seconds):
                    cities, city_context, capture_status = await city_decisions.capture(
                        gateway, job, evidence["objectives"]
                    )
            except (TimeoutError, OSError, ValueError, aiohttp.ClientError):
                capture_status = {"capturedCities": 0, "reason": "native_capture_unavailable"}
            await self.save(
                cityCaptureUnavailable=capture_status.get("reason")
                in {"native_capture_unavailable", "stale_native_input"}
                or capture_status.get("unavailableCities", 0) > 0
            )
        city_key = city_decisions.semantic_key(cities, city_context, evidence["objectives"]) if cities else None
        if cities:
            evidence.update(cities=cities.copy(), cityContext=city_context)
            while evidence["cities"]:
                try:
                    request_body(evidence, self.settings)
                    break
                except ValueError:
                    evidence["cities"].pop()
                    names = {o["native"]["name"] for c in evidence["cities"] for o in c["constructions"]}
                    city_context["constructionRules"] = [
                        r for r in city_context.get("constructionRules", []) if r["name"] in names
                    ]
                    city_context["omittedRuleNames"] = [
                        name for name in city_context.get("omittedRuleNames", []) if name in names
                    ]
            omitted = len(cities) - len(evidence["cities"])
            if omitted:
                cities = evidence["cities"].copy()
                capture_status["omittedCities"] = capture_status.get("omittedCities", 0) + omitted
                city_key = city_decisions.semantic_key(cities, city_context, evidence["objectives"]) if cities else None
            if city_key is not None and city_key == state.get("cityKey"):
                city_result = state.get("cityResult", {"answers": []})
                if city_result.get("fallback") in {
                    "broker_unavailable",
                    "malformed_broker_output",
                    "completion_unknown",
                }:
                    city_result = await self.evaluate(state["cityEvidence"])
                    await self.save(cityResult=city_result)
                evidence["cities"] = []
                capture_status["reused"] = True
            if not evidence["cities"]:
                evidence.pop("cityContext", None)
        result: JsonObject = {"answers": [], "fallback": "cooldown_or_unchanged"}
        has_memory = any(evidence.get(k) for k in ("retrieval", "preservation", "beliefs"))
        if has_memory or evidence.get("cities"):
            pending = {"answers": [], "fallback": "completion_unknown", "requestId": request_id(evidence)}
            if has_memory:
                latest = await self.state()
                await self.save(
                    memoryAttemptTurn=turn,
                    memoryEvidence={**latest.get("memoryEvidence", {}), pending["requestId"]: evidence},
                )
                await self.record_source_attempts(evidence, pending)
            if evidence.get("cities"):
                await self.save(cityKey=city_key, cityResult=pending, cityEvidence=evidence)
            result = await self.evaluate(evidence)
            await self.record_source_attempts(evidence, result)
            if has_memory and result.get("fallback") not in {
                "broker_unavailable",
                "malformed_broker_output",
                "completion_unknown",
            }:
                latest = await self.state()
                retained = latest.get("memoryEvidence", {})
                retained.pop(result.get("requestId"), None)
                await self.save(memoryEvidence=retained)
            if evidence.get("cities"):
                city_result = result
                await self.save(cityKey=city_key, cityResult=result, cityEvidence=evidence)
        notices, ranks = await gateway.io.call(self.store.apply, job, evidence, result)
        for field in ("preservationCandidates", "beliefNotices"):
            combined = [row for notice in recovered_notices for row in notice[field]] + notices[field]
            key = (
                (lambda row: (row["key"], row["version"]))
                if field == "preservationCandidates"
                else (lambda row: (row["belief"]["key"], row["belief"]["version"]))
            )
            notices[field] = list({key(row): row for row in combined}.values())
        if notices.get("requestId") is None and recovered_notices:
            notices["requestId"] = recovered_notices[0].get("requestId")
        classified_city_result = city_result
        if cities and not await gateway.io.call(self.store.objectives_current, evidence["objectives"], turn):
            city_result = {"answers": [], "fallback": "stale_objectives"}
        notices["cityRecommendations"] = city_decisions.recommendations(gateway, job, cities, city_result, city_context)
        notices["cityAdviceStatus"] = {**capture_status, "fallback": city_result.get("fallback")}
        job.memory["cityObjectiveSources"] = [
            {"key": r["key"], "version": r["version"]} for r in evidence["objectives"]
        ]
        memory_evidence = {k: v for k, v in evidence.items() if k not in {"cities", "cityContext"}}
        bindings = recovered_bindings + (delivery_basis(memory_evidence, result) if result.get("requestId") else [])
        bindings.extend(await gateway.io.call(self.store.cached_retrieval_bindings, job, evidence))
        if classified_city_result.get("requestId"):
            bindings.extend(delivery_basis({"objectives": evidence["objectives"]}, classified_city_result, cities))
        job.memory["advisoryDeliveryBasis"] = list({(row["requestId"], row["name"]): row for row in bindings}.values())
        await gateway.io.call(self.store.city_delivery, job, notices)
        job.memory["advisories"] = notices
        job.memory["advisoryRanking"] = ranks
        if notices["cityRecommendations"]:
            await self.validate_city_delivery(job)

    async def finish_delivery(self, job):
        """Record exposure privately after the selected memory has actually been read."""
        bindings = job.memory.get("advisoryDeliveryBasis", [])
        objective_status = {}
        for row in bindings:
            identifier = row["requestId"]
            if identifier not in objective_status:
                objective_status[identifier] = await self.gateway.io.call(
                    self.store.objectives_current, row.get("objectives", []), job.memory.get("gameTurn")
                )
        job.memory["advisoryObjectiveStatus"] = objective_status
        rows = finish_delivery(bindings, job.memory)
        if rows == job.memory.get("advisoryDelivery"):
            return
        await self.gateway.io.call(self.store.record_delivery, job, rows)
        job.memory["advisoryDelivery"] = rows
        if self.gateway.accounting is not None:
            for identifier in dict.fromkeys(row["requestId"] for row in rows):
                await self.gateway.io.call(
                    self.gateway.accounting.classifier,
                    "advisory_delivery",
                    {
                        "requestId": identifier,
                        "policyVersion": TASK_VERSION,
                        "deliveryResults": [row for row in rows if row["requestId"] == identifier],
                    },
                )

    async def validate_city_delivery(self, job):
        notices = job.memory["advisories"]
        try:
            fresh = await city_decisions.internal_read(self.gateway, job, "player_turn", {})
        except (TimeoutError, OSError, ValueError, aiohttp.ClientError):
            fresh = {}
        if not isinstance(fresh, dict):
            fresh = {}
        recommendations = notices.get("cityRecommendations", [])
        valid = (
            "error" not in fresh
            and await self.gateway.io.call(
                self.store.objectives_current, job.memory.get("cityObjectiveSources", []), job.memory.get("gameTurn")
            )
            and all(
                city_decisions.current(self.gateway, job, r["basis"])
                and all(fresh.get(k) == r["basis"][k] for k in ("epoch", "revision", "turn"))
                for r in recommendations
            )
        )
        if not valid:
            notices["cityRecommendations"] = []
            notices["cityAdviceStatus"]["fallback"] = "stale_native_basis"
        await self.gateway.io.call(self.store.city_delivery, job, notices)

    async def triage(self, turn, *, scheduled_soon, next_review_turn):
        if turn < EARLY_REVIEW_MIN_TURN:
            return {"earlyReview": False, "reason": "opening_turns"}
        if scheduled_soon:
            return {"earlyReview": False, "reason": "scheduled_review_soon"}
        evidence = await self.gateway.io.call(self.store.review_input, turn, self.settings, next_review_turn)
        if evidence is None:
            return {"earlyReview": False, "reason": "ineligible"}
        signature = evidence.pop("signature")
        state = await self.state()
        original = state.get("reviewEvidence")
        same_context = signature == state.get("reviewAttemptSignature") and original is not None
        if same_context:
            # Keep provider input and its identity pinned, even when the current
            # overview acquired a newer version with the same material facts.
            evidence = original
            scheduled = state.get("lastEarlyTurn")
            if type(scheduled) is int and scheduled >= evidence["earlyReview"]["completedTurn"]:
                return {"earlyReview": False, "reason": "cached"}
        await self.save(
            reviewAttemptSignature=signature,
            earlyRequestTurn=None,
            reviewEvidence=evidence,
            reviewDeliveryEvidence=None,
        )
        result = await self.evaluate(evidence)
        accepted = any(a["name"] == "early_review" and a["choice"] == "review" for a in result.get("answers", []))
        binding = await self.gateway.io.call(
            current_review_binding, self.gateway.memory, turn, self.settings, next_review_turn, signature
        )
        if binding is None:
            return {"earlyReview": False, "reason": "stale_evidence"}
        await self.save(
            earlyRequestTurn=turn if accepted else None, reviewDeliveryEvidence=binding if accepted else None
        )
        return {
            "earlyReview": accepted,
            "reason": "meaningful_change" if accepted else result.get("fallback") or "wait",
        }

    async def review_scheduled(self, turn):
        """A prepared review acknowledges scheduling, independently of classifier acceptance."""
        state = await self.state()
        evidence = state.get("reviewEvidence")
        binding = state.get("reviewDeliveryEvidence")
        if (
            evidence is None
            or binding is None
            or state.get("earlyRequestTurn") != turn
            or binding["earlyReview"]["completedTurn"] != turn
        ):
            await self.save(lastEarlyTurn=turn)
            return
        identifier = request_id(evidence)
        if state.get("reviewDeliveryTurn") == turn and any(
            row.get("requestId") == identifier and row.get("status") == "delivered"
            for row in state.get("reviewDelivery", [])
        ):
            await self.save(lastEarlyTurn=turn)
            return
        result = state.get("attempts", {}).get(identifier, {})
        if not any(a["name"] == "early_review" and a["choice"] == "review" for a in result.get("answers", [])):
            await self.save(lastEarlyTurn=turn)
            return
        current = await self.gateway.io.call(
            current_review_binding,
            self.gateway.memory,
            turn,
            self.settings,
            binding["earlyReview"]["nextReviewTurn"],
            state.get("reviewAttemptSignature"),
        )
        await self.save(lastEarlyTurn=turn)
        if current is None:
            return
        rows = finish_delivery(
            delivery_basis(current, result),
            {"earlyReviewDelivery": {"requestId": identifier, "scheduled": True}},
        )
        if rows == state.get("reviewDelivery"):
            return
        await self.save(reviewDelivery=rows, reviewDeliveryTurn=turn, reviewDeliveryEvidence=current)
        if self.gateway.accounting is not None:
            await self.gateway.io.call(
                self.gateway.accounting.classifier,
                "advisory_delivery",
                {"requestId": identifier, "policyVersion": TASK_VERSION, "deliveryResults": rows},
            )
