"""Bounded, read-only military consultation followed by main's exact-terms decision."""

import asyncio
import contextlib
import copy
import json
import secrets
import time
from contextlib import nullcontext

from pydantic import Field

from civ_agent.agent_history import HISTORY_METHODS
from civ_agent.agent_settings import codex_thread_settings
from civ_agent.player_prompts import CAMPAIGN_GUIDANCE
from civ_agent.proposal_gateway import JobAccess
from civ_agent.provider_usage import reconcile_provider_usage
from civ_agent.review_grounding import ReviewEvidence
from civ_agent.review_runtime import OfflineConnection
from civ_agent.schemas import BoundaryModel
from civ_agent.telemetry import merge_model_usage
from civ_agent.tls import private_directory
from civ_agent.war_state import tactical_basis


class TacticalOverview(BoundaryModel):
    goalAssessment: str = Field(min_length=1, max_length=4000)
    battlefield: str = Field(min_length=1, max_length=4000)
    continuingWar: str = Field(min_length=1, max_length=4000)
    peaceConsequences: str = Field(min_length=1, max_length=4000)
    uncertainties: str = Field(min_length=1, max_length=2000)
    recommendation: str = Field(min_length=1, max_length=2000)


class PeaceApproval(BoundaryModel):
    approve: bool
    reason: str = Field(min_length=1, max_length=3000)


def peace_terms(name, payload, selected=None):
    if (
        name == "player_command"
        and payload.get("kind") == "CITY_STATE_ACTION"
        and payload.get("selection") == "makePeace"
    ):
        return {k: payload[k] for k in ("kind", "target", "selection")}
    if name not in {"trade_offer", "trade_decide"} or name == "trade_decide" and payload.get("decision") != "accept":
        return None
    terms = selected if name == "trade_decide" else payload
    items = [*(terms or {}).get("give", []), *(terms or {}).get("receive", [])]
    items.extend(clause["item"] for clause in (terms or {}).get("clauses", []))
    if not any(item.get("type") == "PeaceProposal" or item.get("name") == "Peace Treaty" for item in items):
        return None
    return {
        k: copy.deepcopy((terms or {}).get(k))
        for k in ("author", "partner", "give", "receive", "clauses", "proposal", "version", "nativeRequestId")
    }


class PeaceReviews:
    def __init__(self, service):
        self.service = service
        self.lock = asyncio.Lock()
        self.unconfirmed_process = None

    def stored(self, key, value=None):
        memory = self.service.memory
        with memory.transaction() as db:
            if value is not None:
                memory._set(db, "peace:" + key, value)
            return memory._meta(db, "peace:" + key)

    async def authorize(self, job, name, payload, selected=None):
        terms = peace_terms(name, payload, selected)
        if terms is None:
            return None
        service, gateway = self.service, self.service.gateway
        async with self.lock, gateway.role_dispatch if name != "player_command" else nullcontext():
            tracks_wait = hasattr(job, "wait_started") and job.wait_started is None
            if tracks_wait:
                job.wait_started = time.monotonic()
            gateway.consultation_hold.clear()
            for participant in list(gateway.jobs.values()):
                if participant.purpose in {"gameplay", "economy", "military"}:
                    gateway.suspend_negotiation(participant)
            caller_record = service.records.get(job.id)
            old_role_state = caller_record.get("roleState") if caller_record else None
            try:
                if caller_record:
                    caller_record["roleState"] = "waiting_negotiation"
                    await service.io.call(service.accounting.sample, caller_record, job.waiting_seconds())
                    await service.flush_telemetry()
                view = await job.connection.tool("player_turn", {})
                if "error" in view:
                    return {"error": "PEACE_OBSERVATION_UNAVAILABLE", "applied": False}
                state = await service.io.call(gateway.wars.synchronize, job.epoch, view)
                basis = tactical_basis(view, state, terms)
                record = await service.io.call(self.stored, basis) or {"terms": terms, "attempts": 0}

                async def persist():
                    await service.io.call(self.stored, basis, record)
                    if caller_record is not None:
                        caller_record["peaceConsultation"] = {"basis": basis, **copy.deepcopy(record)}
                        await service._save(caller_record)

                if record.get("status") != "complete":
                    if record["attempts"] >= 3:
                        return {"error": "PEACE_CONSULTATION_UNAVAILABLE", "applied": False, "consultation": record}
                    # Persist before spending; failures remain visible across process restarts.
                    record.update(status="pending", attempts=record["attempts"] + 1)
                    await persist()
                    inputs = {"position": view, "warState": state, "exactPeaceTerms": terms}
                    try:
                        if "overview" not in record:
                            record["overview"] = await self.compute(job, "military", inputs, TacticalOverview)
                            await persist()
                        record["approval"] = await self.compute(
                            job, "gameplay", {**inputs, "militaryOverview": record["overview"]}, PeaceApproval
                        )
                        record["status"] = "complete"
                        await persist()
                    except (ValueError, TimeoutError) as error:
                        record.update(status="failed", error=str(error))
                        await persist()
                        return {"error": "PEACE_CONSULTATION_UNAVAILABLE", "applied": False, "consultation": record}
                if not record["approval"]["approve"]:
                    return {"error": "MAIN_DECLINED_PEACE", "applied": False, "consultation": record}
                latest = await job.connection.tool("player_turn", {})
                current = await service.io.call(gateway.wars.state)
                if "error" in latest or tactical_basis(latest, current, terms) != basis:
                    return {
                        "error": "PEACE_REVIEW_STALE",
                        "applied": False,
                        "instruction": "Read the changed position and reconsider the terms before requesting consultation again.",
                    }
                if name == "player_command":
                    payload["revision"] = latest["revision"]
                else:
                    payload["expectedRevision"] = latest["revision"]
                return None
            finally:
                gateway.consultation_hold.set()
                if tracks_wait:
                    job.completed_wait += time.monotonic() - job.wait_started
                    job.wait_started = None
                if caller_record and job.purpose == "communication":
                    if old_role_state:
                        caller_record["roleState"] = old_role_state
                    else:
                        caller_record.pop("roleState", None)
                    # Reacquire the caller's lease before continuing its model.
                    if service.chat.running:
                        while not (await service.chat.social("job_claim", job=job.id, purpose=job.purpose))["granted"]:
                            await asyncio.sleep(0.25)

    async def compute(self, caller, role, inputs, schema):
        """A fresh bounded conversation with no live connection or mutation tools."""
        service = self.service
        identifier = "peace" + secrets.token_hex(10)
        main_record = service.records.get(service.active_id, {})
        from civ_agent.effort_decisions import base_settings

        settings = (
            base_settings(main_record)
            if "settings" in main_record
            else dict((service.match or {}).get("effectiveSettings", service.default_settings))
        )
        if role == "military":
            settings.update(
                {("reasoningEffort" if k == "reasoning_effort" else k): v for k, v in service.military_model.items()}
            )
        record = {
            "activationId": identifier,
            "purpose": "consultation",
            "modelPhase": "peace_" + role,
            "civilization": service.civilization,
            "epoch": caller.epoch,
            "gameTurn": inputs["position"].get("turn"),
            "phase": "STARTING",
            "settings": settings,
            "usage": {},
            "modelRequests": 0,
            "consultationFor": caller.id,
        }
        service.records[identifier] = record
        frozen = {"observation": inputs["position"], "reads": {"player_turn": inputs["position"]}, "catalogue": []}
        job = JobAccess(
            identifier,
            "consultation",
            caller.epoch,
            OfflineConnection(),
            {},
            verified=True,
            catalogue=[],
            review_inputs=frozen,
            review_evidence=ReviewEvidence(frozen),
        )
        done = asyncio.Event()
        completions, answers, usages = {}, {}, {}
        process = None
        granted = False

        async def notification(method, params):
            if method in HISTORY_METHODS:
                await service.io.call(service.history.notify, identifier, record.get("threadId"), method, params)
            if method == "thread/tokenUsage/updated":
                usages[params.get("turnId")] = params.get("tokenUsage", {}).get("total", {})
            elif method == "turn/completed":
                turn = params.get("turn", {})
                completions[turn.get("id")] = turn.get("status")
                done.set()
            elif method == "item/completed" and params.get("item", {}).get("type") == "agentMessage":
                item = params["item"]
                if item.get("phase") in {None, "final_answer"}:
                    answers[params.get("turnId")] = item.get("text", "")
            elif method in {"service/disconnected", "service/transportFailure", "service/approvalRequired"}:
                if record["phase"] != "COMPLETED":
                    record["error"] = "CONSULTATION_PROCESS_FAILED"
                    done.set()

        try:
            async with asyncio.timeout(180):
                if service.chat.running:
                    while True:
                        grant = await service.chat.social("job_claim", job=identifier, purpose="consultation")
                        if grant["granted"]:
                            granted = True
                            break
                        if grant.get("reason") in {"MODEL_INVOCATION_LIMIT", "ROLE_CAPACITY_TOO_SMALL"}:
                            raise ValueError(grant["reason"])
                        await asyncio.sleep(0.25)
                await service.io.call(service.accounting.start, identifier, caller.epoch, granted)
                service.gateway.jobs[job.token] = job
                await service.io.call(service.history.begin, record, None, job.token)
                instruction = (
                    "Use only the frozen permitted observations supplied. Do not access files, execute actions or request approvals. "
                    "Distinguish facts, assessments and unknowns. Return the required JSON. "
                    + (
                        "As military, assess goals, tactical position, likely gains/losses continuing, immediate threats and what peace gives up. You advise; main decides."
                        if role == "military"
                        else "As main, approve or decline the exact peace terms after considering military's overview and current strategic goals. Explain your decision, including any disagreement."
                    )
                    + "\n\n"
                    + CAMPAIGN_GUIDANCE
                )
                snapshot = await service.io.call(service.memory.snapshot, role, inputs["position"].get("turn"))
                inputs = {**inputs, "privateMemory": service.memory.context(snapshot)}
                prompt = json.dumps(inputs, ensure_ascii=False)
                await service.io.call(service.history.startup_context, identifier, instruction, role="developer")
                await service.io.call(service.history.startup_context, identifier, prompt)
                workspace = private_directory(service.workspace / "consultations" / identifier)
                process = service.process_factory(
                    service.executable, workspace, service.gateway_url, service.gateway_ca_file, notification
                )
                await process.start(job.token, settings)
                thread = await process.request(
                    "thread/start",
                    {
                        "cwd": str(workspace),
                        "approvalPolicy": "never",
                        "sandbox": "workspace-write",
                        "developerInstructions": instruction,
                        **codex_thread_settings(settings, fast=service.fast),
                    },
                )
                record["threadId"] = thread["thread"]["id"]
                effective = {
                    "provider": thread.get("modelProvider"),
                    "model": thread.get("model"),
                    "reasoningEffort": thread.get("reasoningEffort"),
                }
                if any(settings.get(k) is not None and settings[k] != effective[k] for k in effective):
                    raise ValueError("CONSULTATION_SETTINGS_CHANGED")
                record["effectiveSettings"] = effective
                result = await process.request(
                    "turn/start",
                    {
                        "threadId": record["threadId"],
                        "input": [{"type": "text", "text": prompt}],
                        "approvalPolicy": "never",
                        "model": effective["model"],
                        "outputSchema": schema.model_json_schema(),
                        **({"effort": settings["reasoningEffort"]} if settings.get("reasoningEffort") else {}),
                    },
                )
                turn = result["turn"]["id"]
                await service.io.call(service.history.bind_turn, identifier, turn)
                record.update(turnId=turn, phase="RUNNING")
                await service._save(record)
                while turn not in completions and not record.get("error"):
                    done.clear()
                    try:
                        await asyncio.wait_for(done.wait(), 10)
                    except TimeoutError:
                        if granted and not (await service.chat.social("job_heartbeat", job=identifier))["granted"]:
                            raise ValueError("CONSULTATION_PERMIT_EXPIRED") from None
                if record.get("error") or completions.get(turn) != "completed":
                    raise ValueError("CONSULTATION_PROCESS_FAILED")
                answer = schema.model_validate_json(answers.get(turn, "")).model_dump()
                from civ_agent.agent_service import USAGE_KEYS

                merge_model_usage(record, usages.get(turn, {}), USAGE_KEYS)
                record.update(phase="COMPLETED", answer=answer)
                return answer
        finally:
            job.closed = True
            service.gateway.jobs.pop(job.token, None)
            if process:
                try:
                    await process.close()
                except BaseException:
                    self.unconfirmed_process = process
                    service.failure = "STOP_UNCONFIRMED"
                    service.stop_event.set()
                    record.update(phase="RUNNING", error="STOP_UNCONFIRMED")
                    await service._save(record)
                    raise
            if record["phase"] != "COMPLETED":
                record["phase"] = "FAILED"
            reconciled = await service.io.call(reconcile_provider_usage, record, service.provider_usage)
            record["usageComplete"] = reconciled or record.get("usageReported", False)
            await service.io.call(service.accounting.sample, record, 0)
            await service._save(record)
            if granted:
                with contextlib.suppress(OSError, ValueError, TimeoutError):
                    await service.flush_telemetry()
                    await service.chat.social("job_release", job=identifier, usage=record["usage"])
