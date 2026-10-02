"""Concurrent, player-scoped opening advice exists before the first gameplay request."""

import asyncio
import copy
import json

import pytest

from civ_agent import service_recovery
from civ_agent.agent_service import AgentService, ServiceError
from civ_agent.strategic_review import advice_prompt
from tests.test_proposal_gateway import Host
from tests.test_strategic_adviser import ReviewFactory, ReviewHost
from tests.test_strategic_review import VICTORIES
from tests.test_victory_snapshot import position


class OpeningHost(ReviewHost):
    def __init__(self, factory):
        super().__init__(factory)
        self.civilization = factory.service.civilization
        self.turn = 0
        self.requests = []
        self.view.update(
            cities=[],
            civilizationProfile={"name": self.civilization, "ability": {"name": self.civilization + "_ABILITY"}},
        )
        self.view["map"]["units"][0].update(name="Settler", military=False)
        self.view["map"]["units"][0]["position"]["x"] = {"Rome": 0, "Greece": 10, "France": -10}[self.civilization]

    async def tool(self, name, args):
        self.requests.append((name, copy.deepcopy(args)))
        if name == "player_turn":
            return {
                **copy.deepcopy(self.view),
                "epoch": "epoch-one",
                "revision": "opening",
                "turn": self.turn,
                "phase": "PAUSED" if self.civilization == "Rome" else "NOT_YOUR_TURN",
                "rules": {"victoryTypes": VICTORIES},
            }
        if name == "victory_progress":
            return position(self.turn, VICTORIES)
        if name == "player_communication":
            if args["op"] == "state":
                return {
                    "civilization": self.civilization,
                    "epoch": "epoch-one",
                    "context": {"turn": self.turn},
                    "offers": [],
                    "consents": [],
                    "agreements": [],
                    "messages": [],
                }
            if args["op"] == "read":
                if args.get("waitSeconds"):
                    await asyncio.Future()
                return {"events": [], "nextCursor": args["cursor"], "hasMore": False}
            return {"granted": True}
        if name in {"research_details", "policy_details"}:
            field = "technologies" if name == "research_details" else "policies"
            return {"turn": self.turn, field: [{"name": "TEST", "description": "Public rules"}]}
        return await Host.tool(self, name, args)


def test_opening_reviews_run_concurrently_then_each_civ_uses_its_own_report(tmp_path, monkeypatch):
    async def run():
        services, factories, hosts = {}, {}, {}
        for civ in ("Rome", "Greece", "France"):
            factory = ReviewFactory()
            service = AgentService(
                civ,
                tmp_path / civ / "workspace",
                tmp_path / civ / "private",
                "S" * 43,
                "https://localhost:8765/mcp",
                None,
                process_factory=factory,
                connection_factory=factory.connection,
                fast=True,
            )
            factory.service = service
            services[civ], factories[civ], hosts[civ[0] * 43] = service, factory, OpeningHost(factory)
        monkeypatch.setattr("civ_agent.chat_runtime.HostConnection", lambda url, token, *args, **kwargs: hosts[token])
        try:
            # Connecting waits for the shared phase. All three reviews must
            # reach RUNNING before any player makes its first move.
            for civ, service in services.items():
                await service.chat.connect(
                    {
                        "epoch": "epoch-one",
                        "playerToken": civ[0] * 43,
                        "model": "gpt-5.6-luna",
                        "reasoningEffort": "max",
                        "timeoutSeconds": 30,
                    }
                )
            assert all(not f.review_started.is_set() for f in factories.values())
            for service in services.values():
                await service.review_phase({"epoch": "epoch-one", "gameTurn": 0, "stage": "hold"})
                assert (await service_recovery.barrier(service, True))["ready"]
            await asyncio.gather(
                *(
                    service.review_phase({"epoch": "epoch-one", "gameTurn": 0, "stage": "start"})
                    for service in services.values()
                )
            )
            await asyncio.wait_for(asyncio.gather(*(f.review_started.wait() for f in factories.values())), 3)
            for civ, service in services.items():
                task = service.reviews.store.pending()
                assert task is not None and task["turn"] == 0 and task["attempts"] == 1
                assert service.active_id is None
                inputs = json.loads(task["inputs"])
                assert inputs["actions"]["entries"] == []
                assert inputs["reads"]["player_turn"]["civilizationProfile"]["name"] == civ
                prompt = advice_prompt(inputs)
                assert "Opening position before the first move, turn 0" in prompt.user
                assert civ + "_ABILITY" in prompt.user
                assert all(other + "_ABILITY" not in prompt.user for other in services if other != civ)
                assert (await service.review_phase({"stage": "status", "epoch": "epoch-one", "gameTurn": 0}))[
                    "pendingReview"
                ]
                claim = next(args for name, args in hosts[civ[0] * 43].requests if args.get("op") == "job_claim")
                assert claim["purpose"] == "review" and "parentJob" not in claim
                assert factories[civ].calls[0][1]["model"] == "gpt-5.6-luna"
                assert factories[civ].calls[0][1]["config"]["model_reasoning_effort"] == "max"

            rome, factory = services["Rome"], factories["Rome"]
            payload = {
                "activationId": "first-move",
                "epoch": "epoch-one",
                "civilization": "Rome",
                "gameTurn": 0,
                "handoff": 0,
                "threadId": None,
                "mcpUrl": rome.mcp_url,
                "playerToken": "P" * 43,
                "prompt": "Play one turn",
                "model": "gpt-5.6-luna",
                "reasoningEffort": "max",
                "timeoutSeconds": 30,
                "leaseSeconds": 10,
            }
            with pytest.raises(ServiceError, match="STRATEGIC_REVIEW_PENDING"):
                await rome.start(payload)
            assert factory.review_process is not None
            await factory.review_process.finish_review()
            async with asyncio.timeout(3):
                while rome.reviews.store.pending() is not None or rome.reviews.active:
                    await asyncio.sleep(0.01)
            assert (await rome.review_phase({"stage": "status", "epoch": "epoch-one", "gameTurn": 0}))[
                "pendingReview"
            ] is None
            assert all(services[civ].reviews.store.pending() is not None for civ in ("Greece", "France"))
            with pytest.raises(ServiceError, match="REVIEW_PHASE_ACTIVE"):
                await rome.start(payload)
            # Repeated preparation does not capture or pay for completed work.
            previous = len(hosts["R" * 43].requests)
            await rome.reviews.prepare_round(0)
            assert len(hosts["R" * 43].requests) == previous

            # Freeze an unfinished peer review and restore it with its attempt
            # count, even though it has no preceding gameplay job.
            greece = services["Greece"]
            await service_recovery.barrier(greece, True)
            saved = await service_recovery.capture(greece, "opening")
            restored = await service_recovery.restore(
                greece, {"id": saved["id"], "sha256": saved["sha256"], "epoch": "restored"}
            )
            assert restored["pendingReviews"]
            greece.chat.epoch = "restored"
            assert (await greece.review_phase({"stage": "status", "epoch": "restored", "gameTurn": 0}))[
                "pendingReview"
            ]["attempts"] == 1
            # Restart only the interrupted reviewer from its frozen input.
            greece.chat.connection = hosts["G" * 43]
            factories["Greece"].review_started.clear()
            await greece.review_phase({"stage": "start", "epoch": "restored", "gameTurn": 0})
            await asyncio.wait_for(factories["Greece"].review_started.wait(), 3)
            assert factories["Greece"].review_process is not None
            await factories["Greece"].review_process.finish_review()
            assert factories["France"].review_process is not None
            await factories["France"].review_process.finish_review()
            async with asyncio.timeout(3):
                while any(
                    service.reviews.store.pending() is not None or service.reviews.active
                    for service in services.values()
                ):
                    await asyncio.sleep(0.01)
            # All reports are ready; the operator releases the shared phase.
            for service in services.values():
                await service.review_phase({"epoch": service.chat.epoch, "gameTurn": 0, "stage": "release"})
                await service_recovery.barrier(service, False)
            factory.complete = False
            await rome.start(payload)
            async with asyncio.timeout(3):
                while rome.status()["phase"] != "RUNNING":
                    await asyncio.sleep(0.01)
            user = [p for method, p in factory.calls if method == "turn/start"][-1]["input"][0]["text"]
            assert "Latest completed strategic advice" in user and '"asOfTurn":0' in user
        finally:
            await asyncio.gather(*(service.close() for service in services.values()))

    asyncio.run(run())
