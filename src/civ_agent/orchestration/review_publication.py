"""Capture frozen reviews and publish advice independently of turn activation."""

import json

from pydantic_ai.messages import ToolCallPart
from sqlalchemy import select

from civ_agent import memory_schema as tables
from civ_agent.execution_policy import coordinator_execution_policy
from civ_agent.orchestration.contracts import ReviewRequest, Scope, digest
from civ_agent.orchestration.providers import ProviderClients
from civ_agent.orchestration.review import ReviewServices, delivered_reads, validate_report
from civ_agent.orchestration.store import JournalConflict
from civ_agent.orchestration.transcripts import read as read_transcript
from civ_agent.review_capture import ReviewCapture
from civ_agent.review_store import ReviewStore
from civ_agent.runtime.review_cycles import ReviewCycle
from civ_agent.strategic_review import StrategicAdvice, review_due


class NativeReviews:
    def __init__(self, root, communication, *, publication_checkpoint=None):
        self.root, self.communication = root, communication
        self.store = ReviewStore(root.memory)
        self.clients = ProviderClients()
        self.validation = ReviewServices(root.store, self.clients)
        self.publication_checkpoint = publication_checkpoint

    @staticmethod
    def key(target_turn):
        return "nativeReviewCycle:" + str(target_turn)

    def _load(self, target_turn):
        with self.root.memory.transaction() as db:
            body = self.root.memory._meta(db, self.key(target_turn))
            if body is None:
                return None
            cycle = ReviewCycle.model_validate(body)
            self.root.check_owner(cycle.scope)
            if cycle.scope.epoch != self.root.memory._meta(db, "epoch"):
                raise JournalConflict("REVIEW_EPOCH_CONFLICT")
            return cycle

    async def for_target(self, turn):
        return await self.root.io.call(self._load, turn)

    async def initialize_import(self, turn):
        """Mark an unavailable predecessor review at a fresh raw-save boundary.

        No historical observation or advice is synthesized. Future scheduled
        reviews still require evidence captured before their native handoff.
        """
        root = self.root
        async with root.registry.dispatch:
            status = await root.lease()
            origin = {"epoch": status["epoch"], "turn": turn}
            scope = Scope(
                game=root.game,
                player=root.player,
                epoch=status["epoch"],
                activation=digest([root.game, root.player, origin, "import-review-unavailable"]),
            )

            def install():
                with root.memory.transaction() as db:
                    prior = root.memory._meta(db, "nativeReviewImportOrigin")
                    if prior is not None:
                        if prior != origin:
                            raise JournalConflict("REVIEW_IMPORT_ORIGIN_CONFLICT")
                        return
                    if turn <= 0 or status["turn"] != turn:
                        raise JournalConflict("REVIEW_IMPORT_POSITION_CHANGED")
                    if root.memory._meta(db, "epoch") != scope.epoch:
                        raise JournalConflict("REVIEW_EPOCH_CONFLICT")
                    if review_due(turn - 1, root.review_interval) and root.memory._meta(db, self.key(turn)) is None:
                        cycle = ReviewCycle(
                            scope=scope,
                            source_turn=turn - 1,
                            target_turn=turn,
                            kind="periodic",
                            status="UNAVAILABLE",
                        )
                        root.memory._set(db, self.key(turn), cycle.model_dump(mode="json"))
                    root.memory._set(db, "nativeReviewImportOrigin", origin)

            await root.io.call(install)

    async def pending(self, handoff=None):
        def load():
            with self.root.memory.transaction() as db:
                return [
                    ReviewCycle.model_validate(json.loads(raw))
                    for raw in db.execute(
                        select(tables.meta.c.value).where(tables.meta.c.key.like("nativeReviewCycle:%"))
                    ).scalars()
                    if json.loads(raw) is not None
                ]

        pending = []
        for cycle in await self.root.io.call(load):
            self.root.check_owner(cycle.scope)
            if cycle.scope.epoch != (await self.root.lease())["epoch"]:
                raise JournalConflict("REVIEW_EPOCH_CONFLICT")
            if cycle.status != "PENDING":
                continue
            if not cycle.committed:
                if handoff is not None and cycle.source_handoff is not None and handoff > cycle.source_handoff:
                    cycle = await self.commit(cycle)
                else:
                    await self.discard_unstarted(cycle)
                    continue
            pending.append(cycle)
        return pending

    async def commit(self, cycle):
        current = await self.for_target(cycle.target_turn)
        if current is None or current.id != cycle.id:
            raise JournalConflict("REVIEW_CYCLE_BINDING_CONFLICT")
        current.committed = True
        await self.root.put(self.key(current.target_turn), current.model_dump(mode="json"))
        return current

    async def discard_unstarted(self, cycle):
        """A definite failed handoff must not retain a stale end-of-turn view."""

        def discard():
            with self.root.memory.transaction() as db:
                raw = self.root.memory._meta(db, self.key(cycle.target_turn))
                if raw is None:
                    return
                current = ReviewCycle.model_validate(raw)
                if current.id != cycle.id or current.request is not None or current.committed:
                    raise JournalConflict("REVIEW_CYCLE_ALREADY_STARTED")
                db.execute(
                    tables.reviews.delete().where(
                        tables.reviews.c.job == cycle.id, tables.reviews.c.status == "PENDING"
                    )
                )
                self.root.memory._set(db, self.key(cycle.target_turn), None)

        await self.root.io.call(discard)

    def _require_target(self, turn):
        cycle = self._load(turn)
        required = turn == 0 or review_due(turn - 1, self.root.review_interval)
        if cycle is None:
            if required:
                raise JournalConflict("REVIEW_CYCLE_MISSING")
            return
        if cycle.status == "PENDING":
            raise JournalConflict("REVIEW_BARRIER_REQUIRED")

    async def require_target(self, turn):
        await self.root.io.call(self._require_target, turn)

    def require(self, turn):
        self.root.check_owner(turn.scope)
        self._require_target(turn.turn)

    async def capture(self, source_turn: int, target_turn: int, *, opening=False, handoff=None):
        """Read permitted evidence before handoff; no model or turn transition here."""
        root = self.root
        async with root.registry.dispatch:
            existing = await self.for_target(target_turn)
            if existing is not None:
                return existing
            state = await root.get("advisories", {})
            early = state.get("earlyRequestTurn") == source_turn
            if not opening and not early and not review_due(source_turn, root.review_interval):
                return None
            epoch = (await root.lease())["epoch"]
            scope = Scope(game=root.game, player=root.player, epoch=epoch, activation="review-capture")
            kind = "opening" if opening else "early" if early else "periodic"
            old = await root.io.call(self.store.at_turn, source_turn)
            if old is not None and old["status"] in {"COMPLETED", "UNAVAILABLE"}:
                # A local-runtime upgrade reuses committed advice; it never
                # recaptures a historical world position or repeats its model.
                cycle = ReviewCycle(
                    scope=scope.model_copy(
                        update={"activation": digest([scope.model_dump(), kind, source_turn, target_turn, old["job"]])}
                    ),
                    source_turn=source_turn,
                    target_turn=target_turn,
                    kind=kind,
                    status="READY" if old["status"] == "COMPLETED" else "UNAVAILABLE",
                )
            elif old is not None:
                # Older local runs captured at next-turn admission. Keep their
                # dated evidence rather than reading a different world now.
                def adopt():
                    with root.memory.transaction() as db:
                        raw = db.execute(
                            select(tables.reviews.c.inputs).where(
                                tables.reviews.c.job == old["job"], tables.reviews.c.status == "PENDING"
                            )
                        ).scalar_one()
                        inputs = json.loads(raw)
                        if inputs["observation"]["epoch"] != epoch or inputs["civilization"] != root.civilization:
                            raise JournalConflict("REVIEW_EPOCH_CONFLICT")
                        adopted_scope = scope.model_copy(
                            update={
                                "activation": digest([scope.model_dump(), kind, source_turn, target_turn, old["job"]])
                            }
                        )
                        adopted = ReviewCycle(
                            scope=adopted_scope,
                            source_turn=source_turn,
                            target_turn=target_turn,
                            kind=kind,
                            inputs=root.store.put(adopted_scope, inputs),
                        )
                        db.execute(
                            tables.reviews.update().where(tables.reviews.c.job == old["job"]).values(job=adopted.id)
                        )
                        root.memory._set(db, self.key(target_turn), adopted.model_dump(mode="json"))
                        return adopted

                return await root.io.call(adopt)
            else:
                connection = await self.communication.connection(epoch)

                async def read(name, arguments):
                    result = await connection.tool(name, arguments)
                    if "error" in result:
                        raise JournalConflict("REVIEW_CAPTURE_FAILED")
                    return result

                snapshot = await read("player_turn", {})
                if (snapshot.get("epoch"), snapshot.get("turn")) != (epoch, source_turn):
                    raise JournalConflict("REVIEW_POSITION_CHANGED")
                tools = root.gateway.tool_catalogue(await connection.list_tools(), "gameplay")
                inputs = await ReviewCapture(root, self.store, root.review_interval).capture(
                    snapshot, read, tools, source_turn, epoch
                )
                inputs["reviewTiming"] = {"kind": kind, "sourceTurn": source_turn, "targetTurn": target_turn}
                scope = scope.model_copy(
                    update={
                        "activation": digest(
                            [root.game, root.player, epoch, kind, source_turn, target_turn, digest(inputs)]
                        )
                    }
                )
                reference = root.store.put(scope, inputs)
                cycle = ReviewCycle(
                    scope=scope,
                    source_turn=source_turn,
                    target_turn=target_turn,
                    kind=kind,
                    inputs=reference,
                    committed=opening,
                    source_handoff=handoff,
                )

                def prepare_cycle(db):
                    root.memory._set(db, self.key(target_turn), cycle.model_dump(mode="json"))

                await root.io.call(
                    self.store.prepare,
                    cycle.id,
                    inputs,
                    root.get_selection("review").model_dump(mode="json"),
                    root.managed_session.settings.review_timeout_seconds,
                    after_preparation=prepare_cycle,
                )
                return cycle
            await root.put(self.key(target_turn), cycle.model_dump(mode="json"))
            return cycle

    async def begin(self, cycle: ReviewCycle, generation: str):
        current = await self.for_target(cycle.target_turn)
        if current is None or current.id != cycle.id:
            raise JournalConflict("REVIEW_CYCLE_BINDING_CONFLICT")
        if current.status != "PENDING":
            return None
        if not current.committed:
            raise JournalConflict("REVIEW_HANDOFF_REQUIRED")
        if current.inputs is None:
            raise JournalConflict("REVIEW_CYCLE_INPUT_REQUIRED")
        scope = current.scope.model_copy(update={"activation": digest([current.id, generation])})
        request = ReviewRequest(
            execution_policy=coordinator_execution_policy(self.root),
            scope=scope,
            inputs=self.root.store.put(scope, self.root.store.get(current.scope, current.inputs)),
            selection=self.root.get_selection("review"),
            provider_contract=self.root.get_provider_contract("review"),
            request_limit=self.root.get_request_limit("review"),
            attempt_limit=self.root.review_attempts,
            timeout_seconds=self.root.managed_session.settings.review_timeout_seconds,
            strategy_guide=self.root.managed_session.settings.strategy_guide,
            civilization_guide=self.root.managed_session.settings.civilization_guide,
            managed=True,
            source_turn=current.source_turn,
            target_turn=current.target_turn,
            review_cycle_id=current.id,
        )
        current.request = request
        await self.root.put(self.key(current.target_turn), current.model_dump(mode="json"))
        return request

    async def _publication_cycle(self, cycle: ReviewCycle, request: ReviewRequest):
        current = await self.for_target(cycle.target_turn)
        if current is None or (current.id, current.request) != (cycle.id, request):
            raise JournalConflict("REVIEW_CYCLE_BINDING_CONFLICT")
        identity = ("scope", "source_turn", "target_turn", "kind", "inputs", "source_handoff")
        if any(getattr(current, name) != getattr(cycle, name) for name in identity):
            raise JournalConflict("REVIEW_CYCLE_BINDING_CONFLICT")
        if not current.committed or (request.source_turn, request.target_turn, request.review_cycle_id) != (
            current.source_turn,
            current.target_turn,
            current.id,
        ):
            raise JournalConflict("REVIEW_CYCLE_BINDING_CONFLICT")
        self.root.check_owner(request.scope)
        if (await self.root.lease())["epoch"] != cycle.scope.epoch:
            raise JournalConflict("REVIEW_EPOCH_CONFLICT")
        if current.inputs is None or self.root.store.get(request.scope, request.inputs) != self.root.store.get(
            current.scope, current.inputs
        ):
            raise JournalConflict("REVIEW_INPUT_CHANGED")
        last = await self.root.io.call(self.store.last_completed_turn)
        if last is not None and last > cycle.source_turn:
            raise JournalConflict("REVIEW_PUBLICATION_REWOUND")
        return current

    async def unavailable_before_model(self, cycle: ReviewCycle, request: ReviewRequest):
        """A bounded wait can end before any model step; do not invent a transcript."""
        async with self.root.registry.dispatch:
            current = await self._publication_cycle(cycle, request)
            if current.status != "PENDING":
                return
            current.status = "UNAVAILABLE"

            def publish_cycle(db):
                self.root.memory._set(db, self.key(cycle.target_turn), current.model_dump(mode="json"))

            await self.root.io.call(
                self.store.unavailable, cycle.id, "REVIEW_WAIT_EXHAUSTED", after_publication=publish_cycle
            )

    async def publish(self, cycle: ReviewCycle, request: ReviewRequest, result):
        root = self.root
        async with root.registry.dispatch:
            current = await self._publication_cycle(cycle, request)
            if result.scope != request.scope or root.store.receipt(request.scope, "report") != result.model_dump(
                mode="json"
            ):
                raise JournalConflict("REVIEW_RECEIPT_REQUIRED")
            advice = None
            if result.status == "COMPLETED":
                messages = await read_transcript(root.store, request.scope, result.history, agent="strategic_review_v1")
                reports = [
                    part
                    for message in messages
                    for part in message.parts
                    if isinstance(part, ToolCallPart) and part.tool_name == "submit_report"
                ]
                if not reports:
                    raise JournalConflict("REVIEW_SUBMISSION_REQUIRED")
                advice = validate_report(
                    request,
                    StrategicAdvice.model_validate(reports[-1].args_as_dict()),
                    delivered_reads(messages),
                    worker=self.validation,
                )
                if advice != root.store.get(request.scope, result.report):
                    raise JournalConflict("REVIEW_REPORT_CHANGED")
            current.status = "READY" if advice is not None else "UNAVAILABLE"

            def publish_cycle(db):
                root.memory._set(db, self.key(cycle.target_turn), current.model_dump(mode="json"))

            if advice is not None:
                await root.io.call(self.store.complete, cycle.id, advice, after_publication=publish_cycle)
            else:
                await root.io.call(
                    self.store.unavailable, cycle.id, "REVIEW_UNAVAILABLE", after_publication=publish_cycle
                )
            if self.publication_checkpoint is not None:
                await self.publication_checkpoint("review-publication", "review", cycle.id)

    async def close(self):
        await self.clients.close()
