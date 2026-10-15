"""Optional memory/review triage and separate batched city recommendations."""

import asyncio

import aiohttp

from civ_agent import city_decisions
from civ_agent.advisory_store import AdvisoryStore, material_facts
from civ_agent.decisions_ledger import fingerprint
from civ_agent.decisions_tasks import TASK_VERSION, parse_answers, request_body, request_id
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
        """Preflight and remember attempts before I/O; broker deduplicates durably."""
        stable_id = request_id(evidence)
        state = await self.state()
        attempted = state.get("attempts", {})
        if stable_id in attempted:
            return attempted[stable_id]
        if self.settings.additional_decisions_budget_usd == 0:
            result = {"answers": [], "fallback": "budget_exhausted", "requestId": stable_id}
            await self.save(attempts={**attempted, stable_id: result})
            return result
        try:
            body = request_body(evidence, self.settings)
        except (ValueError, TypeError, KeyError):
            result = {"answers": [], "fallback": "input_unavailable", "requestId": stable_id}
        else:
            pending = {"answers": [], "fallback": "completion_unknown", "requestId": stable_id}
            await self.save(attempts={**attempted, stable_id: pending})
            raw: JsonObject
            try:
                raw = await self.client.batch(evidence, timeout=self.settings.decisions_timeout_seconds)
            except (TimeoutError, OSError, ValueError):
                raw = {"answers": [], "fallback": "broker_unavailable", "requestId": stable_id}
            if (
                not isinstance(raw, dict)
                or raw.get("requestId") != stable_id
                or not isinstance(raw.get("answers"), list)
            ):
                raw = {"answers": [], "fallback": "malformed_broker_output", "requestId": stable_id}
            # Validate at the delivery boundary too; a transport reply never
            # grants new memory sources or unexpected task choices.
            if raw.get("answers"):
                supplied = raw["answers"]
                known = {a.get("name") for a in supplied if isinstance(a, dict) and isinstance(a.get("name"), str)}
                complete = supplied + [
                    {"name": q["name"], "type": "refusal"} for q in body["questions"] if q["name"] not in known
                ]
                parsed = parse_answers(body, {"answers": complete}, self.settings.decisions_min_confidence)
                result = {**raw, **parsed}
            else:
                result = {**raw, "answers": []}
        state = await self.state()
        if self.gateway.accounting is not None:
            await self.gateway.io.call(self.gateway.accounting.classifier, "advisory_batch", result)
        await self.save(
            attempts={**state.get("attempts", {}), stable_id: result},
            accounting=result.get("accounting", state.get("accounting")),
        )
        return result

    async def startup(self, job, reads, topics):
        gateway = self.gateway
        # Preparing the same activation again reuses its frozen memory outputs.
        if "advisories" in job.memory:
            if job.memory["advisories"].get("cityRecommendations"):
                await self.validate_city_delivery(job)
            return
        evidence = await gateway.io.call(self.store.freeze, job, topics, self.settings)
        state = await self.state()
        retrieval_key = fingerprint({k: evidence[k] for k in ("objectives", "topics", "retrieval")})
        if retrieval_key in {state.get("retrievalKey"), state.get("retrievalAttemptKey")}:
            evidence["retrieval"] = []
        evidence["version"] = TASK_VERSION
        turn = job.memory.get("gameTurn") or 0
        eligible = (
            turn - state.get("startupTurn", -self.settings.decisions_startup_min_gap_turns)
            >= self.settings.decisions_startup_min_gap_turns
        )
        cities, city_context = [], {}
        capture_status: JsonObject = {"capturedCities": 0, "reason": "cooldown_or_disabled_budget"}
        city_result: JsonObject = {"answers": []}
        if eligible and self.settings.additional_decisions_budget_usd > 0:
            try:
                async with asyncio.timeout(self.settings.decisions_timeout_seconds):
                    cities, city_context, capture_status = await city_decisions.capture(
                        gateway, job, evidence["objectives"]
                    )
            except TimeoutError:
                capture_status = {"capturedCities": 0, "reason": "native_capture_timeout"}
        city_key = city_decisions.semantic_key(cities, city_context, evidence["objectives"]) if cities else None
        if cities:
            evidence.update(cities=cities.copy(), cityContext=city_context)
            # Preserve the pair of questions for each city when fitting the
            # shared byte/question limits. Memory triage keeps its fallback.
            while evidence["cities"]:
                try:
                    request_body(evidence, self.settings)
                    break
                except ValueError:
                    evidence["cities"].pop()
                    # Rules for omitted candidates can otherwise keep the
                    # remaining city pair above the byte limit on their own.
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
                evidence["cities"] = []
            if not evidence["cities"]:
                evidence.pop("cityContext", None)
        result: JsonObject = {"answers": [], "fallback": "cooldown_or_unchanged"}
        if eligible and any(evidence.get(k) for k in ("retrieval", "preservation", "beliefs", "cities")):
            values: JsonObject = {"startupTurn": turn}
            if evidence["retrieval"]:
                values["retrievalAttemptKey"] = retrieval_key
            await self.save(**values)
            result = await self.evaluate(evidence)
            if evidence.get("cities"):
                city_result = result
                await self.save(cityKey=city_key, cityResult=result)
            seen = {s["key"]: s["version"] for s in evidence["newEvidence"]}
            state = await self.state()
            view = reads.get("player_turn", {})
            await self.save(
                seenSources={**state.get("seenSources", {}), **seen},
                seenFacts=fingerprint(material_facts(view)) if view else state.get("seenFacts"),
            )
        notices, ranks = await gateway.io.call(self.store.apply, job, evidence, result)
        if cities and not await gateway.io.call(self.store.sources_current, evidence["objectives"]):
            city_result = {"answers": [], "fallback": "stale_objectives"}
        notices["cityRecommendations"] = city_decisions.recommendations(gateway, job, cities, city_result, city_context)
        notices["cityAdviceStatus"] = {**capture_status, "fallback": city_result.get("fallback")}
        job.memory["cityObjectiveSources"] = [
            {"key": r["key"], "version": r["version"]} for r in evidence["objectives"]
        ]
        await gateway.io.call(self.store.city_delivery, job, notices)
        if evidence["retrieval"] and any(a["name"].startswith("retrieval:") for a in result.get("answers", [])):
            await self.save(retrievalKey=retrieval_key, retrievalRanking=ranks)
        elif retrieval_key == state.get("retrievalKey"):
            ranks = state.get("retrievalRanking", {})
        job.memory["advisories"] = notices
        job.memory["advisoryRanking"] = ranks
        if notices["cityRecommendations"]:
            await self.validate_city_delivery(job)

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
            and await self.gateway.io.call(self.store.sources_current, job.memory.get("cityObjectiveSources", []))
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

    async def triage(self, turn, *, scheduled_soon):
        if scheduled_soon:
            return {"earlyReview": False, "reason": "scheduled_review_soon"}
        evidence = await self.gateway.io.call(self.store.review_input, turn, self.settings)
        if evidence is None:
            return {"earlyReview": False, "reason": "ineligible"}
        signature = evidence.pop("signature")
        state = await self.state()
        if signature == state.get("reviewAttemptSignature"):
            accepted = state.get("earlyRequestTurn") == turn
            return {"earlyReview": accepted, "reason": "cached"}
        await self.save(reviewAttemptSignature=signature, earlyRequestTurn=None)
        result = await self.evaluate(evidence)
        accepted = any(a["name"] == "early_review" and a["choice"] == "review" for a in result.get("answers", []))
        if not await self.gateway.io.call(self.store.review_current, evidence):
            return {"earlyReview": False, "reason": "stale_evidence"}
        await self.save(earlyRequestTurn=turn if accepted else None)
        return {
            "earlyReview": accepted,
            "reason": "meaningful_change" if accepted else result.get("fallback") or "wait",
        }
