"""Per-event communication scheduling and durable original-text delivery."""

import asyncio
import hashlib
import json
import time

from civ_agent.civilization_state import StateConflict
from civ_agent.conversation_context import audience
from civ_agent.diplomacy_decisions import BATCH_SECONDS, CHOICES, TIMEOUT_SECONDS, fallback
from civ_agent.diplomacy_store import DiplomacyStore, delivery_status, tuple_thread
from civ_agent.memory_store import encode


class DiplomacyRouting:
    def __init__(self, gateway, client, *, clock=time.time, human_players=(), settings=None):
        self.gateway, self.client, self.clock = gateway, client, clock
        self.store = DiplomacyStore(gateway.memory, clock)
        self.human_players = set(human_players)
        self.classifier_lock = asyncio.Lock()
        self.delivery_lock = asyncio.Lock()
        self.classifications = set()
        self.active_drafts = set()
        self.generation = 0
        self.timeout = settings.decisions_timeout_seconds if settings is not None else TIMEOUT_SECONDS
        self.min_confidence = settings.decisions_min_confidence if settings is not None else 0.90

    def suspend(self):
        self.generation += 1
        for task in self.classifications:
            task.cancel()

    async def classify(self, direction, bundle):
        generation = self.generation
        try:
            # Waiting behind another classification consumes the same budget;
            # concurrent sends must not accumulate five seconds each in a queue.
            async with asyncio.timeout(self.timeout), self.classifier_lock:
                self.check_generation(generation)
                task = asyncio.create_task(self.client.classify(direction, bundle))
                self.classifications.add(task)
                try:
                    result = await task
                finally:
                    self.classifications.discard(task)
        except TimeoutError:
            result = fallback(direction, "timeout")
        except asyncio.CancelledError:
            if generation != self.generation:
                raise StateConflict("ROUTING_INTERRUPTED") from None
            raise
        except (OSError, ValueError):
            self.check_generation(generation)
            result = fallback(direction, "service_failure")
        if self.gateway.accounting is not None:
            await self.gateway.io.call(
                self.gateway.accounting.classifier,
                direction,
                result if isinstance(result, dict) else fallback(direction, "malformed_broker_output"),
            )
        self.check_generation(generation)
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("choice"), str)
            or result.get("choice") not in CHOICES[direction]
        ):
            return fallback(direction, "malformed_broker_output")
        if result["choice"] != next(iter(CHOICES[direction])):
            confidence = result.get("confidence")
            if (
                not isinstance(confidence, (int, float))
                or isinstance(confidence, bool)
                or not self.min_confidence <= confidence <= 1
            ):
                return fallback(direction, "low_confidence")
        return result

    def protected(self, thread, social):
        # Public chat reaches all chat recipients. Any human in the audience
        # makes the entire thread immediate, including messages authored by AI.
        members = set(social.get("chatRecipients", social.get("channels", []))) if thread[0] else set(thread[1])
        if members & (self.human_players | set(social.get("humanPlayers", []))):
            return True
        if not social.get("routingMetadataAvailable", False):
            return True  # Older hosts cannot establish that a negotiation is absent.
        members.discard(self.gateway.memory.civilization)
        for proposal in social.get("trades", []):
            participants = set(proposal.get("participants", [])) | {proposal.get("author"), proposal.get("partner")}
            participants.discard(None)
            if proposal.get("status", "PENDING") == "PENDING" and participants & members:
                return True
        return False

    async def expire_incoming(self, epoch):
        """A backlog of separate threads cannot extend the oldest wait budget."""
        generation = self.generation
        for row in await self.gateway.io.call(self.store.incoming):
            state = row["routing"]
            if state.get("choice") or state.get("receivedAt", self.clock()) + BATCH_SECONDS > self.clock():
                continue
            async with self.gateway.admit():
                self.check_generation(generation)
                await self.gateway.io.call(
                    self.store.route,
                    epoch,
                    [row["rowId"]],
                    fallback("incoming", "routing_deadline"),
                    "deadline-" + str(row["rowId"]),
                )
                thread = audience(row["event"].get("body", {}), self.gateway.memory.civilization)
                if thread is not None:
                    await self.urgent_thread(epoch, thread)

    async def route_incoming(self, epoch, social, refresh=None):
        generation = self.generation
        rows = await self.gateway.io.call(self.store.incoming)
        groups = {}
        for row in rows:
            event = row["event"]
            thread = audience(event.get("body", {}), self.gateway.memory.civilization)
            if (
                event["kind"] != "message"
                or event.get("body", {}).get("nativeAction")
                or thread is None
                or self.protected(thread, social)
            ):
                if row["routing"].get("choice") != "respond_now":
                    async with self.gateway.admit():
                        self.check_generation(generation)
                        await self.gateway.io.call(
                            self.store.route,
                            epoch,
                            [row["rowId"]],
                            fallback("incoming", "protected"),
                            "protected-" + str(row["rowId"]),
                        )
                        if thread is not None:
                            await self.urgent_thread(epoch, thread)
                continue
            if row["routing"].get("choice") == "no_separate_response":
                continue
            groups.setdefault(thread, []).append(row)
        for thread, pending in groups.items():
            # Reclassify a coalesced bundle only when there are new events. Its
            # old deadline persists even if the new answer is batch again.
            if all(row["routing"].get("choice") for row in pending):
                continue
            ids = [row["rowId"] for row in pending]
            bundle = await self.gateway.io.call(self.store.bundle, thread, [row["event"]["body"] for row in pending])
            digest = hashlib.sha256(encode({"epoch": epoch, "ids": ids, "bundle": bundle}).encode()).hexdigest()
            decision = await self.classify("incoming", bundle)
            if refresh is not None:
                try:
                    social = await refresh()
                    if self.protected(thread, social):
                        decision = fallback("incoming", "protected")
                except (OSError, ValueError, TimeoutError):
                    decision = fallback("incoming", "protection_refresh_failed")
            async with self.gateway.admit():
                self.check_generation(generation)
                await self.gateway.io.call(self.store.route, epoch, ids, decision, digest)
                if decision["choice"] == "respond_now":
                    await self.urgent_thread(epoch, thread)

    async def urgent_thread(self, epoch, thread):
        for draft in await self.gateway.io.call(self.store.drafts):
            if tuple_thread(draft["thread"]) == thread and draft["status"] == "queued":
                await self.gateway.io.call(self.store.update, epoch, draft["deliveryId"], deadline=self.clock())

    def check_generation(self, generation):
        if generation != self.generation or self.gateway.checkpoint_requested:
            raise StateConflict("ROUTING_INTERRUPTED")

    async def send(self, job, payload, call_id, *, immediate=False):
        generation = self.generation
        social = await self.gateway.read_social(job, delivered=False)
        recipients = payload.get("recipients") or ([payload["to"]] if payload.get("to") else [])
        thread = (False, tuple(sorted({self.gateway.memory.civilization, *recipients}))) if recipients else (True, ())
        # Exclude the delivery worker until the new draft is marked active, and
        # preserve the existing concurrent-conversation freshness rule.
        async with self.delivery_lock, self.gateway.dispatch:
            self.gateway.check(job)
            update = await self.gateway.conversation_update(job, payload)
            if update:
                return update
            draft = await self.gateway.io.call(self.store.draft, job, call_id, payload, thread)
            if draft["status"] == "classifying":
                self.active_drafts.add(draft["deliveryId"])
        if draft["status"] == "classifying":
            try:
                if immediate or self.protected(thread, social):
                    decision = fallback("outgoing", "immediate" if immediate else "protected")
                else:
                    bundle = await self.gateway.io.call(
                        self.store.bundle, thread, [], exclude_delivery=draft["deliveryId"]
                    )
                    bundle["proposedMessage"] = {"author": self.gateway.memory.civilization, "text": payload["text"]}
                    decision = await self.classify("outgoing", bundle)
                    # A negotiation may have opened while the classifier was
                    # running. Never apply its delayed answer to protected text.
                    try:
                        if self.protected(thread, await self.gateway.read_social(job, delivered=False)):
                            decision = fallback("outgoing", "protected")
                    except (OSError, ValueError, TimeoutError):
                        decision = fallback("outgoing", "protection_refresh_failed")
                self.check_generation(generation)
                draft = await self.gateway.io.call(
                    self.store.update,
                    job.epoch,
                    draft["deliveryId"],
                    only_status="classifying",
                    status="omitted" if decision["choice"] == "omit" else "queued",
                    decision=decision,
                )
                if decision["choice"] == "send_now":
                    await self.flush_thread(job.epoch, thread, connection=job.connection)
            finally:
                self.active_drafts.discard(draft["deliveryId"])
        return delivery_status(await self.gateway.io.call(self.store.get, draft["deliveryId"]))

    async def control(self, job, arguments):
        async with self.delivery_lock:
            draft = await self.gateway.io.call(self.store.get, arguments["deliveryId"])
            if draft is None:
                raise StateConflict("UNKNOWN_DELIVERY")
            action = arguments.get("action", "status")
            if action == "cancel" and draft["status"] in {"queued", "omitted", "classifying"}:
                draft = await self.gateway.io.call(
                    self.store.update, job.epoch, draft["deliveryId"], status="cancelled", override="cancel"
                )
            elif action == "immediate" and draft["status"] in {"queued", "omitted", "classifying", "sending"}:
                await self.gateway.io.call(self.store.update, job.epoch, draft["deliveryId"], override="immediate")
                await self._deliver(job.epoch, draft["deliveryId"], job.connection)
                draft = await self.gateway.io.call(self.store.get, draft["deliveryId"])
            return delivery_status(draft)

    async def _deliver(self, epoch, id, connection):
        if self.gateway.paused or self.gateway.checkpoint_requested or self.gateway.run_failure:
            return
        async with self.gateway.admit():
            async with self.gateway.dispatch:
                request = await self.gateway.io.call(self.store.prepare_delivery, epoch, id)
                if request is None:
                    return
                self.gateway.in_flight += 1
                try:
                    if request["result"] is not None:
                        result = json.loads(request["result"])
                    else:
                        result = await connection.tool(
                            "player_communication", {"op": "receipt", "epoch": epoch, "requestId": request["id"]}
                        )
                        if result.get("error") == "NOT_ENACTED":
                            result = await connection.tool("player_communication", json.loads(request["payload"]))
                    await self.gateway.io.call(self.store.settle, id, request, result)
                except BaseException:
                    await self.gateway.io.call(self.gateway.memory.uncertain, request["id"])
                    raise
                finally:
                    self.gateway.in_flight -= 1

    async def flush_thread(self, epoch, thread, connection=None):
        if connection is None:
            return  # The dispatch loop flushes due drafts with its own connection.
        async with self.delivery_lock:
            for draft in await self.gateway.io.call(self.store.drafts):
                if tuple_thread(draft["thread"]) == thread and draft["status"] in {"queued", "sending"}:
                    await self._deliver(epoch, draft["deliveryId"], connection)

    async def flush_due(self, epoch, connection):
        async with self.delivery_lock:
            for draft in await self.gateway.io.call(self.store.drafts):
                if (
                    draft["status"] == "sending"
                    or draft["status"] == "classifying"
                    and draft["deliveryId"] not in self.active_drafts
                    or draft["status"] == "queued"
                    and draft["deadline"] <= self.clock()
                ):
                    await self._deliver(epoch, draft["deliveryId"], connection)

    async def reconcile(self, epoch, connection):
        for draft in await self.gateway.io.call(self.store.drafts):
            if draft["status"] != "sending":
                continue
            pending = await self.gateway.io.call(self.gateway.memory.pending)
            request = next((row for row in pending if row["id"] == draft["requestId"]), None)
            if request is None:
                request = await self.gateway.io.call(self.store.delivery_request, draft["requestId"])
            result = (
                json.loads(request["result"])
                if request["result"]
                else await connection.tool(
                    "player_communication", {"op": "receipt", "epoch": epoch, "requestId": request["id"]}
                )
            )
            if result.get("error") == "NOT_ENACTED":
                # Finish only a delivery already admitted before the barrier.
                result = await connection.tool("player_communication", json.loads(request["payload"]))
            await self.gateway.io.call(self.store.settle, draft["deliveryId"], request, result)

    async def statuses(self):
        return [
            delivery_status(draft)
            for draft in await self.gateway.io.call(self.store.drafts)
            if draft["status"] not in {"sent", "cancelled", "failed"}
        ]
