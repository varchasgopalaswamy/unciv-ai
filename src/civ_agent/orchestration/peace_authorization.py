"""Exact native peace terms, frozen evidence and code-owned local consultations."""

import time
import uuid

from sqlalchemy import select

from civ_agent import memory_schema as tables
from civ_agent.execution_policy import coordinator_execution_policy
from civ_agent.orchestration.contracts import PeaceRequest, PeaceResult, Scope, digest
from civ_agent.orchestration.store import JournalConflict
from civ_agent.peace_contracts import PeaceApproval, TacticalOverview, peace_terms
from civ_agent.protocol import JsonObject
from civ_agent.war_state import tactical_basis


class NativePeace:
    def __init__(self, root, client=None, *, consult=None):
        self.root = root
        self.consult = consult or (client if callable(client) else None)
        self.generation = uuid.uuid4().hex

    def _scope(self, epoch: str, basis: str) -> Scope:
        return Scope(
            game=self.root.game,
            player=self.root.player,
            epoch=epoch,
            activation=digest([self.root.game, self.root.player, epoch, "peace", self.generation, basis]),
        )

    def _key(self, epoch: str, basis: str) -> str:
        return "nativePeace:v2:" + digest([self.root.game, self.root.player, epoch, self.generation, basis])

    async def _saved(self, epoch: str, basis: str) -> tuple[JsonObject, PeaceRequest] | None:
        saved = await self.root.get(self._key(epoch, basis))
        canonical = saved is not None
        if saved is None:
            saved = await self.root.get("nativePeace:" + basis)
        if saved is None:
            return None
        request = PeaceRequest.model_validate(saved["request"])
        if request.scope != self._scope(epoch, basis) or request.basis != basis:
            if canonical:
                raise JournalConflict("PEACE_REQUEST_BINDING_CONFLICT")
            # A basis-only staged record belongs to exactly one owner and
            # epoch. It cannot authorize or block a new timeline's request.
            return None
        return saved, request

    async def results(self, scope: Scope) -> list[PeaceResult]:
        self.root.check_owner(scope)

        def load():

            with self.root.memory.transaction() as db:
                if self.root.memory._meta(db, "epoch") != scope.epoch:
                    raise JournalConflict("PEACE_EPOCH_CONFLICT")
                return list(
                    db.execute(select(tables.meta.c.value).where(tables.meta.c.key.like("nativePeace:%"))).scalars()
                )

        import json

        results, seen = [], set()
        for body in await self.root.io.call(load):
            saved = json.loads(body)
            request = PeaceRequest.model_validate(saved["request"])
            if (request.scope.game, request.scope.player, request.scope.epoch) != (
                scope.game,
                scope.player,
                scope.epoch,
            ):
                continue
            staged = await self._saved(scope.epoch, request.basis)
            if staged is None:
                continue
            _, request = staged
            identifier = request.scope.identity("workflow", "peace")
            if identifier in seen:
                continue
            seen.add(identifier)
            result = self.root.store.receipt(request.scope, "peace-result")
            if result is None:
                if request.managed:
                    # Retained managed phases may be interrupted at native
                    # FINISHED. Only sealed consultations belong in this
                    # result projection; their job reports and SDK histories
                    # retain incomplete phase usage and evidence.
                    continue
                raise JournalConflict("PEACE_COMPLETION_UNCONFIRMED")
            published = PeaceResult.model_validate(result)
            if published.scope != request.scope or published.basis != request.basis:
                raise JournalConflict("PEACE_RESULT_BINDING_CONFLICT")
            results.append(published)
        return results

    async def _prepare(self, job, terms, *, managed=False):
        root = self.root
        view = await job.connection.tool("player_turn", {})
        if "error" in view or view.get("epoch") != job.epoch:
            return None
        state = await root.io.call(root.gateway.wars.synchronize, job.epoch, view)
        basis = tactical_basis(view, state, terms)
        staged = await self._saved(job.epoch, basis)
        if staged is None:
            memories = {}
            for role in ("military", "gameplay"):
                snapshot = await root.io.call(root.memory.snapshot, role, view["turn"])
                memories[role] = root.memory.context(snapshot)
            scope = self._scope(job.epoch, basis)
            inputs = {
                "position": view,
                "warState": state,
                "exactPeaceTerms": terms,
                "privateMemoryByRole": memories,
            }
            request = PeaceRequest(
                execution_policy=coordinator_execution_policy(root),
                managed=managed,
                provider_contract=root.get_provider_contract("peace_main") if managed else None,
                military_provider_contract=root.get_provider_contract("peace_military") if managed else None,
                scope=scope,
                basis=basis,
                inputs=root.store.put(scope, inputs),
                selection=root.get_selection("peace_main"),
                military_selection=root.get_selection("peace_military"),
                request_limit=root.get_request_limit("peace"),
            )
            saved = {"request": request.model_dump(mode="json")}
            await root.put(self._key(job.epoch, basis), saved)
        else:
            saved, request = staged
        if request.managed != managed:
            raise JournalConflict("PEACE_CONTROL_MODE_CONFLICT")
        return view, basis, saved, request

    async def authorize(self, job, name, payload, selected=None):
        terms = peace_terms(name, payload, selected)
        if terms is None:
            return None
        root = self.root
        tracks_wait = job.wait_started is None
        if tracks_wait:
            job.wait_started = time.monotonic()
        root.gateway.consultation_hold.clear()
        try:
            prepared = await self._prepare(job, terms, managed=getattr(root, "external_lease_id", None) is not None)
            if prepared is None:
                return {"error": "PEACE_OBSERVATION_UNAVAILABLE", "applied": False}
            view, basis, saved, request = prepared
            key = self._key(job.epoch, basis)
            if (
                request.basis != basis
                or (request.scope.game, request.scope.player, request.scope.epoch)
                != (root.game, root.player, job.epoch)
                or root.store.get(request.scope, request.inputs)["exactPeaceTerms"] != terms
            ):
                raise JournalConflict("PEACE_REQUEST_BINDING_CONFLICT")
            if saved.get("result") is None:
                receipt = root.store.receipt(request.scope, "peace-result")
                if receipt is None:
                    if self.consult is None:
                        raise JournalConflict("PEACE_RUNNER_NOT_CONFIGURED")
                    result = await self.consult(request)
                    receipt = root.store.receipt(request.scope, "peace-result")
                    if receipt != result.model_dump(mode="json"):
                        raise JournalConflict("PEACE_RECEIPT_REQUIRED")
                saved["result"] = receipt
                await root.put(key, saved)
            result = PeaceResult.model_validate(saved["result"])
            if result.scope != request.scope or result.basis != basis:
                raise JournalConflict("PEACE_RESULT_BINDING_CONFLICT")
            if root.store.receipt(request.scope, "peace-result") != result.model_dump(mode="json"):
                raise JournalConflict("PEACE_RECEIPT_REQUIRED")
            if result.status != "COMPLETED":
                return {"error": "PEACE_CONSULTATION_UNAVAILABLE", "applied": False, "recoverable": True}
            TacticalOverview.model_validate(result.overview)
            approval = PeaceApproval.model_validate(result.approval)
            if not approval.approve:
                return {
                    "error": "MAIN_DECLINED_PEACE",
                    "applied": False,
                    "consultation": result.model_dump(mode="json"),
                }
            latest = await job.connection.tool("player_turn", {})
            current = await root.io.call(root.gateway.wars.state)
            if "error" in latest or latest.get("epoch") != job.epoch or tactical_basis(latest, current, terms) != basis:
                return {
                    "error": "PEACE_REVIEW_STALE",
                    "applied": False,
                    "instruction": "Read the changed position and reconsider the exact terms.",
                }
            payload["revision" if name == "player_command" else "expectedRevision"] = latest["revision"]
            return None
        finally:
            root.gateway.consultation_hold.set()
            if tracks_wait:
                job.completed_wait += time.monotonic() - job.wait_started
                job.wait_started = None
