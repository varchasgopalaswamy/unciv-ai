"""Event routing is deterministic; dormant roles never start a model or block handoff."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from civ_agent.economy_roles import TurnRoles
from civ_agent.economy_schedule import EconomySchedule
from civ_agent.service_io import ServiceIO
from tests.test_structured_memory import create, setup


def observation(turn=4):
    return {
        "epoch": "g1",
        "revision": "r1",
        "turn": turn,
        "phase": "ACTIVE",
        "canEndTurn": True,
        "cities": [
            {
                "handle": "city1",
                "name": "Rome",
                "puppet": False,
                "constructionQueue": ["Worker"],
                "constructionOptions": ["Worker", "Granary"],
                "constructionChoice": {"chosenTurn": 1},
                "development": {
                    "population": 2,
                    "unassignedPopulation": 0,
                    "foodStored": 5,
                    "turnsToGrowth": 4,
                    "starving": False,
                    "builtBuildings": ["Palace"],
                    "tiles": [
                        {"position": {"x": 0, "y": 0}, "worked": True, "yields": [{"stat": "Food", "amount": "2"}]}
                    ],
                },
                "management": {"specialists": [{"name": "Scientist", "slots": 0, "assigned": 0}]},
                "purchases": {"constructions": [{"name": "Granary", "available": False, "goldCost": 200}]},
            }
        ],
        "economy": {
            "treasury": 100,
            "availableGold": 100,
            "goldPerTurn": 5,
            "happiness": 4,
            "cityConnections": [{"name": "Rome", "connectedToCapital": True}],
        },
    }


@pytest.fixture
def scheduled(tmp_path):
    memory = setup(tmp_path)
    memory.start_job("economy", "economy", {}, game_turn=4)
    create(
        memory,
        {
            "type": "city_plan",
            "city": "city1",
            "reasons": {"construction": "Build a Worker"},
            "purchaseTargets": ["Granary"],
        },
        "economy",
    )
    memory.finish_job("economy", True)
    schedule, view = EconomySchedule(memory, 5), observation()
    schedule.complete(view, 4)
    yield schedule, view
    memory.engine.dispose()


def test_ordinary_progress_and_transport_changes_do_not_wake(scheduled):
    schedule, view = scheduled
    view.update(turn=5, revision="r2")
    view["economy"].update(treasury=105, availableGold=105, happiness=3, goldPerTurn=6)
    view["cities"][0]["development"].update(foodStored=10, turnsToGrowth=3)
    view["cities"][0]["constructionEstimates"] = {"Worker": {"turns": 2, "productionStored": 20}}
    status = schedule.assess(view, 5, [])
    assert status["lastReviewTurn"] == status["lastFullReviewTurn"] == 4
    assert status["nextReviewTurn"] == 9
    assert not status["reasons"] and not status["pendingReasons"] and not status["cities"]
    assert not EconomySchedule(schedule.memory, 5).assess(view, 5, [])["reasons"]
    assert schedule.assess(view, 9, [])["reasons"] == ["Periodic economy review due"]


@pytest.mark.parametrize(
    ("change", "reason", "immediate"),
    [
        (lambda v: v["cities"].append({"handle": "city2", "name": "Antium"}), "Newly owned city", True),
        (lambda v: v["cities"][0]["development"].update(population=3), "Population", False),
        (lambda v: v["cities"][0]["development"].update(unassignedPopulation=1), "Unassigned citizens", True),
        (lambda v: v["cities"][0]["development"].update(starving=True), "Food deficit", True),
        (lambda v: v["cities"][0].update(constructionQueue=[]), "Construction", True),
        (lambda v: v["cities"][0]["development"]["builtBuildings"].append("Granary"), "Construction", False),
        (lambda v: v["cities"][0]["constructionChoice"].update(chosenTurn=5), "Construction", False),
        (
            lambda v: v["cities"][0]["development"].update(constructions=[{"index": 0, "productionPerTurn": 0}]),
            "Stalled construction",
            True,
        ),
        (lambda v: v["cities"][0]["constructionOptions"].append("Library"), "Economic options", True),
        (lambda v: v["cities"][0]["management"]["specialists"][0].update(slots=2), "Economic options", True),
        (lambda v: v["cities"][0]["development"]["tiles"][0].update(blockaded=True), "Workable tiles", False),
        (lambda v: v["cities"][0]["purchases"]["constructions"][0].update(available=True), "Planned purchase", True),
        (lambda v: v["economy"].update(happiness=-1), "Happiness", True),
        (lambda v: v["economy"].update(goldPerTurn=-5), "Gold income", True),
        (lambda v: v["economy"].update(resources=[{"name": "Iron", "available": 2}]), "Resource access", True),
        (lambda v: v["economy"].update(goldenAge={"turnsRemaining": 10}), "Golden age", False),
        (lambda v: v.update(research={"technologies": [{"name": "Writing", "researched": True}]}), "Technology", False),
        (
            lambda v: v.update(diplomacy={"knownCivilizations": [{"civilizationId": "Greece", "atWar": True}]}),
            "War started",
            True,
        ),
    ],
)
def test_urgent_events_wake_and_routine_changes_wait_for_review(scheduled, change, reason, immediate):
    schedule, view = scheduled
    change(view)
    status = schedule.assess(view, 5, [])
    assert bool(status["reasons"]) is immediate
    assert any(reason in r for r in status["reasons" if immediate else "pendingReasons"])
    if not immediate:
        assert schedule.assess(view, 9, [])["fullReview"]
    schedule.complete(view, 5)
    assert not schedule.assess(view, 5, [])["reasons"]


def test_routine_requests_wait_for_scheduled_review_and_explicit_dates_are_honored(scheduled):
    schedule, view = scheduled
    request = {"id": "one", "status": "pending", "priority": "routine", "turn": 5}
    assert not schedule.assess(view, 5, [request])["reasons"]
    assert not schedule.assess(view, 6, [request])["reasons"]
    assert schedule.assess(view, 6, [request])["pendingReasons"] == ["Routine economic request: one"]
    assert schedule.assess(view, 9, [request])["fullReview"]
    request.update(priority="urgent")
    assert schedule.assess(view, 5, [request])["reasons"]
    request.update(status="deferred")
    assert not schedule.assess(view, 6, [request])["reasons"]
    request.update(reviewTurn=7)
    assert not schedule.assess(view, 6, [request])["reasons"]
    assert schedule.assess(view, 7, [request])["reasons"]


def test_schedule_rewinds_with_memory_and_epoch_changes_alone_do_not_wake(scheduled, tmp_path):
    schedule, view = scheduled
    path = tmp_path / "review-checkpoint.json"
    checksum = schedule.memory.backup(path)
    schedule.complete(view, 8)
    schedule.memory.restore(path, checksum, "g2")
    view["epoch"] = "g2"
    status = schedule.assess(view, 5, [])
    assert status["lastFullReviewTurn"] == status["lastReviewTurn"] == 4
    assert status["nextReviewTurn"] == 9 and not status["reasons"]


def test_completion_cannot_acknowledge_economic_changes_the_model_never_saw(scheduled):
    schedule, view = scheduled
    observed = copy.deepcopy(view)
    view["cities"][0]["development"]["population"] = 3
    assert not schedule.complete(view, 5, observed)
    assert schedule.assess(view, 5, [])["lastReviewTurn"] == 4
    assert any("Population" in r for r in schedule.assess(view, 5, [])["pendingReasons"])
    assert schedule.complete(view, 5, view)
    assert not schedule.assess(view, 6, [])["reasons"]


def test_no_city_waits_for_founding_and_missing_plan_wakes(scheduled):
    schedule, view = scheduled
    empty = {**view, "cities": []}
    assert not schedule.assess(empty, 12, [])["reasons"]
    assert schedule.assess(empty, 12, [])["nextReviewTurn"] is None
    view["cities"][0]["handle"] = "unplanned"
    assert any("Missing city plan" in r for r in schedule.assess(view, 5, [])["reasons"])


def test_dormant_turn_wakes_for_urgent_request_and_coalesces_events(scheduled, tmp_path):
    async def run():
        schedule, view = scheduled
        view["turn"] = 5
        io = ServiceIO()
        service = SimpleNamespace(
            memory=schedule.memory, io=io, economy_interval=5, records={}, state_dir=tmp_path, _save=AsyncMock()
        )
        roles = TurnRoles(service, {"epoch": "g1", "gameTurn": 5, "activationId": "main"})
        main = SimpleNamespace(purpose="gameplay", epoch="g1", memory={"gameTurn": 5}, observation=view)
        roles.register(main)
        try:
            await roles.initialize_economy(view)
            assert roles.ready["economy"] and not roles.economy_start.is_set()
            await roles.tool(main, "economy_request", {"text": "Build a Library", "priority": "urgent"})
            assert not roles.ready["economy"] and roles.economy_start.is_set()
            assert roles.economy_status["phase"] == "running"
            # Concurrent observations cannot start a second review.
            await roles.synchronize_economy({**view, "revision": "r3"})
            assert roles.economy_requested
        finally:
            await io.close()

    asyncio.run(run())


def test_quiet_turns_start_no_economy_process_and_periodic_review_still_runs(tmp_path):
    from civ_agent.agent_service import AgentService
    from tests.test_agent_service import FakeFactory

    class Factory(FakeFactory):
        def __init__(self):
            super().__init__()
            self.economy_starts = 0

        def __call__(self, *args):
            process = super().__call__(*args)
            self.economy_starts += process.economy
            return process

        def connection(self, *args, **kwargs):
            host = super().connection(*args, **kwargs)
            host.view["cities"][0]["constructionQueue"] = ["Worker"]
            return host

    async def run():
        factory = Factory()
        service = AgentService(
            "Rome",
            tmp_path / "workspace",
            tmp_path / "state",
            "S" * 43,
            "https://localhost:8765/mcp",
            None,
            process_factory=factory,
            connection_factory=factory.connection,
        )
        factory.service = service
        service.memory.bind("g1")
        service.memory.start_job("previous", "economy", {}, game_turn=0)
        create(
            service.memory,
            {"type": "city_plan", "city": "city1", "reasons": {"construction": "Keep the queue"}},
            "previous",
        )
        service.memory.finish_job("previous", True)
        baseline = {**factory.connection().view, "epoch": "g1", "phase": "ACTIVE"}
        EconomySchedule(service.memory, 5).complete(baseline, 0)
        try:
            for turn in (1, 2, 5):
                await service.start(
                    {
                        "activationId": f"main{turn}",
                        "epoch": "g1",
                        "civilization": "Rome",
                        "threadId": None,
                        "gameTurn": turn,
                        "handoff": turn,
                        "mcpUrl": service.mcp_url,
                        "playerToken": "P" * 43,
                        "prompt": "Play",
                        "model": None,
                        "timeoutSeconds": 20,
                        "leaseSeconds": 10,
                    }
                )
                assert service.active_task is not None
                await asyncio.wait_for(asyncio.shield(service.active_task), 8)
                assert service.failure is None, service.status()
                assert service.turn_roles is not None and service.turn_roles.committed
                await service.finalize(
                    {"activationId": f"main{turn}", "epoch": "g1", "gameTurn": turn, "committedHandoff": turn + 1}
                )
                if turn < 5:
                    assert factory.economy_starts == 0
                    assert not any(r.get("parentActivation") == f"main{turn}" for r in service.records.values())
                    assert service.status()["economyUsageComplete"]
                else:
                    assert factory.economy_starts == 1
                    assert service.records[f"main{turn}"]["roles"]["economySchedule"]["lastReviewTurn"] == 5
                if turn == 1:
                    await service.close()
                    service = AgentService(
                        "Rome",
                        tmp_path / "workspace",
                        tmp_path / "state",
                        "S" * 43,
                        "https://localhost:8765/mcp",
                        None,
                        process_factory=factory,
                        connection_factory=factory.connection,
                    )
                    factory.service = service
        finally:
            await service.close()

    asyncio.run(run())


@pytest.mark.parametrize("event", ["urgent", "economic_changes", "required_city_choice"])
def test_dormant_economy_can_start_late_and_must_finish_before_handoff(tmp_path, event):
    from civ_agent.agent_service import AgentService
    from tests.test_agent_service import FakeFactory, StartupHost

    class Factory(FakeFactory):
        def __init__(self):
            super().__init__()
            self.host = StartupHost(self)
            self.host.view["cities"][0]["constructionQueue"] = ["Worker"]
            self.economy_starts = 0
            self.event_sent = False

        def connection(self, *args, **kwargs):
            return self.host

        def __call__(self, *args):
            process = super().__call__(*args)
            self.economy_starts += process.economy
            request = process.request

            async def with_event(method, payload):
                if method == "turn/start":
                    if process.economy:
                        assert not self.host.executed  # END_TURN cannot get ahead of the late role.
                        if event == "required_city_choice":
                            self.host.view.update(canEndTurn=True, pending=[])
                    elif not self.event_sent:
                        self.event_sent = True
                        assert self.economy_starts == 0
                        if event == "urgent":
                            await self.service.gateway.invoke(
                                process.job,
                                "economy_request",
                                {"text": "Reconsider growth", "priority": "urgent"},
                                "new-request",
                            )
                        elif event == "economic_changes":
                            city = self.host.view["cities"][0]
                            city["development"] = {"population": 3, "unassignedPopulation": 1}
                            city["constructionQueue"] = ["Granary"]
                            self.host.revision = "changed-economy"
                        else:
                            self.host.view.update(canEndTurn=False, pending=[{"kind": "CHOOSE_CONSTRUCTION"}])
                            self.host.revision = "required-choice"
                return await request(method, payload)

            process.request = with_event
            return process

    async def run():
        factory = Factory()
        service = AgentService(
            "Rome",
            tmp_path / "workspace",
            tmp_path / "state",
            "S" * 43,
            "https://localhost:8765/mcp",
            None,
            process_factory=factory,
            connection_factory=factory.connection,
        )
        factory.service = service
        service.memory.bind("g1")
        service.memory.start_job("previous", "economy", {}, game_turn=0)
        create(
            service.memory,
            {"type": "city_plan", "city": "city1", "reasons": {"construction": "Keep the queue"}},
            "previous",
        )
        service.memory.finish_job("previous", True)
        EconomySchedule(service.memory, 5).complete({**factory.host.view, "epoch": "g1"}, 0)
        try:
            await service.start(
                {
                    "activationId": "late",
                    "epoch": "g1",
                    "civilization": "Rome",
                    "threadId": None,
                    "gameTurn": 1,
                    "handoff": 0,
                    "mcpUrl": service.mcp_url,
                    "playerToken": "P" * 43,
                    "prompt": "Play",
                    "model": None,
                    "timeoutSeconds": 20,
                    "leaseSeconds": 10,
                }
            )
            assert service.active_task is not None
            await asyncio.wait_for(asyncio.shield(service.active_task), 8)
            assert service.failure is None, service.status()
            assert service.turn_roles is not None and service.turn_roles.committed
            assert factory.economy_starts == 1
            assert [command["kind"] for command in factory.host.executed] == ["END_TURN"]
        finally:
            await service.close()

    asyncio.run(run())
