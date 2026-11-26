"""Local routing preserves immutable broker identities and native send receipts."""

import asyncio
import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from civ_agent.advisory_answers import failed_answers
from civ_agent.decisions_policy import DecisionsSettings
from civ_agent.decisions_tasks import TASK_VERSION, request_body, request_metadata
from civ_agent.diplomacy_decisions import parse_answer
from civ_agent.orchestration.advisories import NativeAdvisories
from civ_agent.orchestration.contracts import NativeAuxiliaryTask, Scope, digest
from civ_agent.orchestration.delivery_contracts import DeliveryAdmission
from civ_agent.orchestration.routing import NativeRouting
from civ_agent.orchestration.routing_contracts import routing_metadata, routing_submission
from civ_agent.orchestration.routing_models import FinishRouting, RoutingReply
from civ_agent.orchestration.store import JournalConflict, WorkflowStore
from tests.test_diplomacy_contracts import opened
from tests.test_diplomacy_routing import decision, event


@asynccontextmanager
async def local_routing(path, *, lose_reply=False, accept_send=True, purpose="communication"):
    async with opened(path / "domain", purpose=purpose) as (gateway, job, host):
        store = WorkflowStore(path / "artifacts", player="p0")
        store.bind_epoch("game", "g1")
        with gateway.memory.transaction() as db:
            gateway.memory._set(db, "nativeGatewayOwner", {"version": 1, "game": "game", "player": "p0"})

        async def get(key, default=None):
            with gateway.memory.transaction() as db:
                return gateway.memory._meta(db, key, default)

        async def put(key, value):
            with gateway.memory.transaction() as db:
                gateway.memory._set(db, key, value)

        def check_owner(scope):
            if (scope.game, scope.player) != ("game", "p0"):
                raise JournalConflict("COORDINATOR_OWNER_CONFLICT")

        root = SimpleNamespace(
            game="game",
            player="p0",
            civilization="Rome",
            memory=gateway.memory,
            gateway=gateway,
            io=gateway.io,
            store=store,
            get=get,
            put=put,
            check_owner=check_owner,
            communication=None,
            connection=host,
            game_state={"lease": "lease"},
        )
        broker = SimpleNamespace(backend="luna", route=AsyncMock(), lookup_route=AsyncMock())
        policy = DecisionsSettings(decisions_enabled=True, decisions_backend="luna")
        router = NativeRouting(root, broker, policy, human_players={"Greece"})
        gateway.routing = router
        scope = Scope(game="game", player="p0", epoch="g1", activation="communication")
        await router.set_hold(scope, False)
        original, receipts, observed = host.tool, {}, []

        async def tool(name, args):
            arguments = args
            if name == "player_communication":
                if arguments["op"] == "receipt":
                    return copy.deepcopy(receipts.get(arguments["requestId"], {"error": "NOT_ENACTED"}))
                if arguments["op"] == "send":
                    draft = next(
                        d for d in router.store.drafts(epoch="g1") if d.get("requestId") == arguments["requestId"]
                    )
                    admission = DeliveryAdmission.model_validate(draft["admission"])
                    effect = await get("nativeDelivery:" + arguments["requestId"])
                    assert effect["prepared"]["payload"] == arguments
                    assert admission.task_id in {r for r in router.tasks}
                    assert draft["status"] == "sending"
                    observed.append(copy.deepcopy(arguments))
                    if lose_reply and not accept_send:
                        raise OSError("reply lost before native admission")
                    result = await original(name, arguments)
                    receipts[arguments["requestId"]] = copy.deepcopy(result)
                    if lose_reply:
                        raise OSError("reply lost after native commit")
                    return result
            result = await original(name, arguments)
            if name == "player_communication" and arguments["op"] == "state":
                result.update(routingMetadataAvailable=True, humanPlayers=["Greece"], trades=[])
            return result

        host.tool = tool

        async def resolve(name, **fields):
            assert name == "resolve_request" and gateway.paused
            assert fields["transport"] == "player_communication" and fields["leaseId"] == "lease"
            payload = fields["request"]
            return {"receipt": copy.deepcopy(receipts.get(payload["requestId"], {"error": "REQUEST_CANCELLED"}))}

        root.op = AsyncMock(side_effect=resolve)
        try:
            yield SimpleNamespace(
                root=root,
                router=router,
                broker=broker,
                gateway=gateway,
                job=job,
                host=host,
                scope=scope,
                receipts=receipts,
                observed=observed,
            )
        finally:
            await router.close()
            store.close()


@pytest.mark.parametrize("purpose", ["gameplay", "communication"])
@pytest.mark.parametrize("completed", [False, True])
def test_native_routing_enables_delivered_main_acknowledgements(tmp_path, purpose, completed):
    async def run():
        async with local_routing(tmp_path, purpose=purpose) as r:
            assert r.root.memory.routing_enabled
            r.root.memory.ingest("g1", [event(101), event(105)], 105)
            r.router.store.route("g1", [1, 2], decision("respond_now"), "urgent")
            unseen = await r.gateway.call(r.job, "chat_read", {"handledMessages": ["m101"]}, "unseen")
            assert unseen["error"] == (
                "MESSAGES_NOT_DELIVERED_TO_JOB" if purpose == "gameplay" else "MESSAGE_ACKNOWLEDGEMENT_GAMEPLAY_ONLY"
            )
            await r.gateway.call(r.job, "chat_read", {}, "read")
            ack = await r.gateway.call(r.job, "chat_read", {"handledMessages": ["m101"]}, "ack")
            assert ("error" not in ack) == (purpose == "gameplay")
            assert len(r.router.store.incoming()) == 2
            if purpose != "gameplay":
                return
            r.root.memory.finish_job(r.job.id, completed)
            remaining = [row["event"]["id"] for row in r.router.store.incoming()]
            assert remaining == ([105] if completed else [101, 105])

    asyncio.run(run())


def test_local_send_persists_exact_effect_before_dispatch_and_reuses_receipt(tmp_path):
    async def run():
        async with local_routing(tmp_path) as r:
            result = await r.gateway.call(r.job, "chat_send", {"text": "Original message", "to": "Greece"}, "send")
            assert result["delivered"] is True and len(r.observed) == 1
            again = await r.gateway.call(r.job, "chat_send", {"text": "Original message", "to": "Greece"}, "send")
            assert again == result and len(r.observed) == 1
            draft = r.router.store.get(result["deliveryId"], epoch="g1")
            assert draft["status"] == "sent" and draft["message"]["text"] == "Original message"
            assert draft["admission"]["task_id"] and draft["admission"]["run_id"]
            assert "workflowId" not in draft["admission"]
            r.broker.route.assert_not_awaited()
            r.router.raise_if_failed()

    asyncio.run(run())


@pytest.mark.parametrize("accept_send", [True, False])
def test_pending_send_with_lost_reply_is_settled_on_manual_restart_without_resend(tmp_path, accept_send):
    async def run():
        async with local_routing(tmp_path, lose_reply=True, accept_send=accept_send) as r:
            with pytest.raises(OSError, match="reply lost"):
                await r.router.send(
                    r.job,
                    {"epoch": "g1", "op": "send", "text": "Keep the original text", "to": "Greece"},
                    "original-call",
                    immediate=True,
                )
            assert len(r.observed) == 1
            draft = r.router.store.drafts(epoch="g1")[0]
            assert draft["status"] == "sending"
            r.gateway.paused = True
            r.gateway.run_failure = None
            r.gateway.memory.resume_run()
            with r.gateway.memory.transaction() as db:
                r.gateway.memory._set(db, "localRecoveryBarrier", {"epoch": "g1", "attempt": "old-attempt"})
            await r.router.set_hold(r.scope, True)
            await r.router.retire_pending(r.scope)
            r.root.op.assert_awaited_once()
            assert len(r.observed) == 1
            after = r.router.store.get(draft["deliveryId"], epoch="g1")
            assert after["status"] == ("sent" if accept_send else "failed")
            if accept_send:
                assert after["message"]["text"] == "Keep the original text"
            else:
                assert after["error"] == "REQUEST_CANCELLED"
            assert len(r.host.writes) == int(accept_send)
            assert r.root.op.await_args.kwargs["request"] == r.observed[0]

    asyncio.run(run())


def test_held_unsent_draft_is_cancelled_without_native_send_on_restart(tmp_path):
    async def run():
        async with local_routing(tmp_path) as r:
            await r.router.set_hold(r.scope, True)
            result = await r.router.send(
                r.job,
                {"epoch": "g1", "op": "send", "text": "Unsent intent", "to": "Greece"},
                "unsent",
                immediate=True,
            )
            assert result["delivered"] is False and not r.observed
            r.gateway.paused = True
            await r.router.retire_pending(r.scope)
            draft = r.router.store.get(result["deliveryId"], epoch="g1")
            assert draft["status"] == "cancelled" and draft["payload"]["text"] == "Unsent intent"
            r.root.op.assert_not_awaited()
            assert not r.observed

    asyncio.run(run())


def test_routing_reuses_broker_result_and_has_no_scheduler_client(tmp_path):
    async def run():
        async with local_routing(tmp_path) as r:
            r.broker.lookup_route.side_effect = lambda identity, **_: {
                "requestId": identity,
                "fallback": "broker_unavailable",
            }
            bundle = {"schemaVersion": 1, "player": "Rome", "direction": "incoming", "events": []}
            first = await r.router.classify(r.scope, "incoming-1", "incoming", bundle)
            second = await r.router.classify(r.scope, "incoming-1", "incoming", bundle)
            assert first == second and first["choice"] == "respond_now"
            assert r.broker.lookup_route.await_count == 1
            assert not hasattr(r.router, "client")
            r.broker.route.assert_not_awaited()
            links = await r.router.results(r.scope)
            assert len(links) == 1

    asyncio.run(run())


def routing_answer(r, submission, *, requests=2):
    """Same immutable decision, with the changing broker account from the failure."""
    metadata = routing_metadata(submission, r.router.settings)
    answers = [
        {
            "name": name,
            "type": "choice",
            "choice": selected,
            "confidence": 0.99,
            "probabilities": [{"value": selected, "probability": 0.99}, {"value": other, "probability": 0.01}],
        }
        for name, selected, other in (
            ("delivery_needed", "needed", "not_needed"),
            ("delivery_timing", "can_wait", "now"),
        )
    ]
    return {
        **metadata,
        **parse_answer("outgoing", {"answers": answers}, thresholds=metadata["thresholds"]),
        "latencyMs": 757,
        "usage": {"input_tokens": 1112, "output_tokens": 0},
        "routingAccounting": {"requests": requests, "inputTokens": 1835 if requests == 2 else 2947},
    }


def test_outgoing_drain_reuses_committed_routing_after_other_classifier_requests(tmp_path):
    async def run():
        async with local_routing(tmp_path) as r:
            answers = []

            async def route(operation, direction, bundle, **_):
                answer = routing_answer(r, routing_submission(operation, direction, bundle, "luna"))
                answers.append(answer)
                return answer

            async def lookup(identity, **_):
                if not answers:
                    return {"requestId": identity, "fallback": "not_submitted"}
                return {**answers[0], "routingAccounting": {"requests": 3, "inputTokens": 2947}}

            r.broker.route.side_effect, r.broker.lookup_route.side_effect = route, lookup
            result = await r.router.send(
                r.job, {"epoch": "g1", "op": "send", "text": "Original batched text", "to": "France"}, "batched"
            )
            assert result["disposition"] == "queued" and not r.observed
            await r.root.io.call(r.router.store.update, "g1", result["deliveryId"], urgent=True)
            # Draining re-enters the recorded outgoing plan after its first
            # routing task completed. A live account snapshot is not a new result.
            await r.router.drain(r.scope)
            await r.router.drain(r.scope)
            assert r.broker.lookup_route.await_count == r.broker.route.await_count == 1
            assert len(r.observed) == len(r.host.writes) == 1
            assert r.observed[0]["text"] == "Original batched text"
            assert r.router.store.get(result["deliveryId"], epoch="g1")["status"] == "sent"
            r.router.raise_if_failed()

    asyncio.run(run())


def test_routing_outcome_hash_ignores_live_account_totals_but_retains_request_usage(tmp_path):
    async def run():
        async with local_routing(tmp_path) as r:
            request = r.router.freeze(
                r.scope, "original-routing", "outgoing", {"player": "Rome", "direction": "outgoing"}
            )
            submission = r.router.inputs(request)
            r.broker.lookup_route.side_effect = [routing_answer(r, submission, requests=count) for count in (2, 3)]
            first, later = await r.router.exchange(request), await r.router.exchange(request)
            assert first == later
            outcome = r.root.store.get(request.scope, first.outcome)
            assert "routingAccounting" not in outcome
            assert outcome["usage"] == {"input_tokens": 1112, "output_tokens": 0} and outcome["latencyMs"] == 757
            accepted = await r.router.finish(FinishRouting(request=request, reply=first))
            assert await r.router.finish(FinishRouting(request=request, reply=later)) == accepted
            r.broker.route.assert_not_awaited()
            assert not r.observed

    asyncio.run(run())


@pytest.mark.parametrize("initial", ["batch", "retained_accounting", "broker_unavailable"])
def test_restarted_routing_reuses_validated_receipt_without_another_broker_exchange(tmp_path, initial):
    async def run():
        async with local_routing(tmp_path) as r:
            request = r.router.freeze(
                r.scope, "finished-routing", "outgoing", {"player": "Rome", "direction": "outgoing"}
            )
            answer = (
                routing_answer(r, r.router.inputs(request))
                if initial != "broker_unavailable"
                else {"requestId": request.request_id, "fallback": initial}
            )
            r.broker.lookup_route.return_value = answer
            reply = await r.router.exchange(request)
            if initial == "retained_accounting":
                # A previously committed envelope remains immutable even when
                # it predates the exclusion of live aggregate accounting.
                outcome = {**r.root.store.get(request.scope, reply.outcome), "routingAccounting": {"requests": 2}}
                reply = RoutingReply(outcome=r.root.store.put(request.scope, outcome))
            accepted = await r.router.finish(FinishRouting(request=request, reply=reply))
            r.broker.lookup_route.side_effect = AssertionError("Committed routing must not exchange again")
            restarted = NativeRouting(r.root, r.broker, r.router.settings)
            try:
                task = await restarted.start(request, "routing")
                assert await task == accepted
                assert restarted.result(request, accepted)["choice"] == (
                    "send_now" if initial == "broker_unavailable" else "batch"
                )
                assert r.broker.lookup_route.await_count == 1 and not r.observed
                restarted.raise_if_failed()
            finally:
                await restarted.close()

    asyncio.run(run())


def test_per_request_usage_changes_still_cannot_replace_a_committed_decision(tmp_path):
    async def run():
        async with local_routing(tmp_path) as r:
            request = r.router.freeze(
                r.scope, "finished-routing", "outgoing", {"player": "Rome", "direction": "outgoing"}
            )
            first = routing_answer(r, r.router.inputs(request))
            changed = {**first, "usage": {"input_tokens": 2224, "output_tokens": 0}}
            r.broker.lookup_route.side_effect = [first, changed]
            accepted = await r.router.finish(FinishRouting(request=request, reply=await r.router.exchange(request)))
            with pytest.raises(JournalConflict, match="WORKFLOW_REQUEST_CHANGED"):
                await r.router.finish(FinishRouting(request=request, reply=await r.router.exchange(request)))
            assert r.root.store.receipt(request.scope, "routing-result") == accepted.model_dump(mode="json")
            assert r.root.store.get(request.scope, accepted.outcome)["usage"] == first["usage"]
            assert not r.observed
            r.broker.route.assert_not_awaited()

    asyncio.run(run())


def test_committed_routing_replay_keeps_full_request_and_epoch_fences(tmp_path):
    async def run():
        async with local_routing(tmp_path) as r:
            request = r.router.freeze(
                r.scope, "finished-routing", "outgoing", {"player": "Rome", "direction": "outgoing"}
            )
            r.broker.lookup_route.return_value = routing_answer(r, r.router.inputs(request))
            accepted = await r.router.finish(FinishRouting(request=request, reply=await r.router.exchange(request)))
            # Same result ID does not permit changing the frozen auxiliary task.
            changed = request.model_copy(
                update={"task": NativeAuxiliaryTask(kind="outgoing_routing", key=digest("different-task"))}
            )
            with pytest.raises(JournalConflict, match="WORKFLOW_REQUEST_CHANGED"):
                await (await r.router.start(changed, "routing"))
            assert r.root.store.receipt(request.scope, "routing-result") == accepted.model_dump(mode="json")
            r.root.store.bind_epoch("game", "g2", previous="g1")
            with pytest.raises(JournalConflict, match="WORKFLOW_EPOCH_CONFLICT"):
                await r.router.finish(FinishRouting(request=request, reply=RoutingReply(outcome=accepted.outcome)))
            assert r.broker.lookup_route.await_count == 1 and not r.observed

    asyncio.run(run())


def test_advisory_instances_use_their_own_broker_and_reuse_original_request(tmp_path):
    async def run():
        async with local_routing(tmp_path / "one") as one, local_routing(tmp_path / "two") as two:
            instances, brokers = [], []
            evidence = {
                "version": TASK_VERSION,
                "player": "Rome",
                "preservation": [{"key": "source", "version": 1, "record": {"body": {"text": "Enemy ships."}}}],
            }
            for r in (one, two):
                settings = DecisionsSettings(decisions_enabled=True, decisions_backend="jev")

                async def batch(value, *, settings=settings, **_):
                    body = request_body(value, settings)
                    return {**request_metadata(body, value), **failed_answers(body, 0.5, "budget_exhausted")}

                broker = SimpleNamespace(backend="jev", batch=AsyncMock(side_effect=batch), lookup=AsyncMock())
                instances.append(NativeAdvisories(r.root, broker, settings))
                brokers.append(broker)
            first, second = await asyncio.gather(*(instance.evaluate(evidence) for instance in instances))
            assert first == second
            assert await instances[0].evaluate(evidence) == first
            assert [broker.batch.await_count for broker in brokers] == [1, 1]
            assert all(not hasattr(instance, "client") for instance in instances)
            await asyncio.gather(*(instance.drain() for instance in instances))

    asyncio.run(run())
