"""War coordination is durable and independent of model execution order."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from jsonschema import Draft202012Validator

from civ_agent.civilization_state import CivilizationState
from civ_agent.economy_roles import TurnRoles
from civ_agent.game_config import GameConfig, Player
from civ_agent.peace_review import PeaceReviews, peace_terms
from civ_agent.protocol import JsonObject
from civ_agent.service_io import ServiceIO
from civ_agent.tool_schemas import tool_model
from civ_agent.war_state import WarStore, goal_versions, tactical_basis, war_opponents
from tests.test_economy_roles import run_async

GOALS = [
    {
        "outcome": "Secure our border",
        "completion": "Remove the visible invasion force",
        "priority": 1,
        "acceptableCosts": "Preserve experienced units",
        "reconsiderWhen": "Enemy reinforcements arrive",
    }
]


def position(*, war=True) -> JsonObject:
    return {
        "turn": 4,
        "revision": "r1",
        "diplomacy": {
            "knownCivilizations": [{"civilizationId": "Greece", "atWar": war, "defeated": False}],
            "cityStates": [],
        },
        "cities": [{"handle": "city1"}],
        "map": {
            "units": [
                {"handle": "soldier", "military": True, "position": {"x": 0, "y": 0}},
                {"handle": "scout", "military": True, "position": {"x": 1, "y": 0}},
                {"handle": "worker", "military": False, "position": {"x": 0, "y": 0}},
            ]
        },
    }


def store(tmp_path):
    memory = CivilizationState(tmp_path, "Rome")
    memory.bind("e1")
    return WarStore(memory)


def apply(wars, role, action, **kwargs):
    return wars.apply("e1", role, "war_goals", {"action": action, **kwargs}, str((role, action, kwargs)))


def test_living_majors_citystates_and_barbarians():
    view = position()
    view["diplomacy"]["knownCivilizations"] += [
        {"civilizationId": "Barbarians", "atWar": True},
        {"civilizationId": "Egypt", "atWar": True, "defeated": True},
    ]
    view["diplomacy"]["cityStates"] = [{"civilizationId": "Geneva", "atWar": True}]
    assert war_opponents(view) == {"Greece", "Geneva"}


@run_async
async def test_initial_goals_require_reason_in_advertised_contract_and_recover_without_role_confusion(tmp_path):
    from tests.test_diplomacy_contracts import opened

    async with opened(tmp_path, "gameplay") as (gateway, main, host):
        service = SimpleNamespace(memory=gateway.memory, io=gateway.io, records={}, _save=AsyncMock())
        roles = TurnRoles(service, {"epoch": "g1", "gameTurn": 0, "activationId": main.id})
        gateway.role_coordinator = roles
        roles.register(main)
        await roles.synchronize_war(position())
        arguments = {"action": "initial", "opponent": "Greece", "goals": GOALS}
        result = await gateway.call(main, "war_goals", arguments, "missing-reason")
        assert result["error"] == "INVALID_TOOL_ARGUMENTS" and not result["applied"]
        assert result["recoverable"]
        assert result["invalidFields"] == [{"fields": ["reason"], "code": "missing"}]
        assert "reason" in result["expectedArguments"]["required"]
        assert next(iter(roles.war_state["episodes"].values()))["version"] == 0
        blocked = await gateway.call(main, "finish_role", {}, "missing-brief")
        assert blocked["error"] == "INITIAL_WAR_GOALS_REQUIRED" and blocked["recoverable"]
        assert blocked["opponents"] == ["Greece"]
        assert "reason" in blocked["recovery"]["instruction"]
        schema = tool_model("war_goals", "gameplay").model_json_schema()
        assert not Draft202012Validator(schema).is_valid(arguments)
        arguments["reason"] = "Defend the frontier while preserving our army."
        assert Draft202012Validator(schema).is_valid(arguments)
        accepted = await gateway.call(main, "war_goals", arguments, "complete-brief")
        assert accepted["initialGoals"]["reason"] == arguments["reason"]
        assert next(iter(roles.war_state["episodes"].values()))["version"] == 1
        assert "episodes" in await gateway.call(main, "war_goals", {}, "default-read")


def test_initial_goals_bind_successful_war_and_rewar_has_new_episode(tmp_path):
    wars = store(tmp_path)
    apply(wars, "gameplay", "initial", opponent="Greece", goals=GOALS, reason="Stop expansion")
    wars.synchronize("e1", position(war=False))
    assert not wars.state()["episodes"]
    state = wars.synchronize("e1", position())
    episode = next(iter(state["episodes"].values()))
    assert episode["version"] == 1 and episode["goals"] == GOALS
    wars.synchronize("e1", position(war=False))
    state = wars.synchronize("e1", position())
    assert len(state["episodes"]) == 2
    assert sum(e["active"] for e in state["episodes"].values()) == 1
    assert next(e for e in state["episodes"].values() if e["active"])["version"] == 0


@pytest.mark.parametrize(
    "author,agree,finalizer,accepted",
    [
        ("gameplay", True, "military", True),
        ("military", True, "military", True),
        ("gameplay", False, "military", False),
        ("military", False, "gameplay", True),
    ],
)
def test_goal_revision_discussion_and_main_breaks_ties(tmp_path, author, agree, finalizer, accepted):
    wars = store(tmp_path)
    wars.synchronize("e1", position())
    apply(wars, "gameplay", "initial", opponent="Greece", goals=GOALS, reason="Defend")
    proposed = apply(wars, author, "propose", opponent="Greece", version=1, goals=GOALS, reason="Updated assessment")
    proposal = proposed["proposal"]["id"]
    assert (
        apply(wars, author, "finalize", opponent="Greece", version=1, proposal=proposal)["error"]
        == "COUNTERPART_RESPONSE_REQUIRED"
    )
    other = "military" if author == "gameplay" else "gameplay"
    apply(
        wars,
        other,
        "respond",
        opponent="Greece",
        version=1,
        proposal=proposal,
        agree=agree,
        reason="Battlefield assessment",
    )
    result = apply(
        wars, finalizer, "finalize", opponent="Greece", version=1, proposal=proposal, reason="Strategic decision"
    )
    assert ("episode" in result) == accepted
    if accepted:
        assert result["episode"]["version"] == 2
        assert result["episode"]["history"][-1]["decidedBy"] == finalizer
        assert (
            apply(wars, other, "propose", opponent="Greece", version=1, goals=GOALS, reason="Late proposal")["error"]
            == "STALE_GOAL_VERSION"
        )


def test_exact_retries_do_not_duplicate_proposals_and_state_reopens(tmp_path):
    wars = store(tmp_path)
    wars.synchronize("e1", position())
    args = {"action": "propose", "opponent": "Greece", "version": 0, "goals": GOALS, "reason": "Defend"}
    first = wars.apply("e1", "military", "war_goals", args, "same-call")
    assert wars.apply("e1", "military", "war_goals", args, "same-call") == first
    reloaded = WarStore(CivilizationState(tmp_path, "Rome"))
    assert reloaded.state() == wars.state()
    assert len(next(iter(reloaded.state()["episodes"].values()))["proposals"]) == 1


@run_async
async def test_dynamic_activation_escort_fencing_and_peacetime_ownership(tmp_path):
    memory = CivilizationState(tmp_path, "Rome")
    memory.bind("e1")
    io = ServiceIO()
    service = SimpleNamespace(memory=memory, io=io, records={}, _save=AsyncMock())
    roles = TurnRoles(service, {"epoch": "e1", "gameTurn": 4, "activationId": "main"})
    jobs = {}
    for role in ("gameplay", "economy", "military"):
        job = SimpleNamespace(
            id=role,
            purpose=role,
            epoch="e1",
            memory={"gameTurn": 4},
            observation=position(),
            gameplay_ended=asyncio.Event(),
            pending_calls={},
        )
        roles.register(job)
        jobs[role] = job
    try:
        await roles.synchronize_war(position())
        assert roles.military_active and not roles.ready["military"]
        jobs["military"].war_versions = goal_versions(roles.war_state)
        move = {"kind": "MOVE_UNIT", "target": "soldier", "destination": {"x": 1, "y": 0}}
        assert roles.command_problem(jobs["gameplay"], move)["error"] == "UNIT_OWNED_BY_OTHER_ROLE"
        assert roles.command_problem(jobs["military"], move) is None
        jobs["gameplay"].observation = None
        missing = await roles.role_tool(
            jobs["gameplay"],
            "escort_assignment",
            {"unit": "soldier", "civilian": "worker", "reason": "Road crew"},
            "stale-escort",
        )
        assert missing["error"] == "OBSERVATION_REQUIRED" and not missing["applied"]
        assert missing["recovery"]["reads"] == [{"tool": "player_turn", "arguments": {}}]
        assert not roles.war_state["escorts"]
        jobs["gameplay"].observation = position()
        await roles.role_tool(
            jobs["gameplay"],
            "escort_assignment",
            {"unit": "soldier", "civilian": "worker", "reason": "Road crew"},
            "escort",
        )
        assert roles.command_problem(jobs["gameplay"], move) is None
        assert roles.command_problem(jobs["military"], move)["error"] == "UNIT_OWNED_BY_OTHER_ROLE"
        assert (
            roles.command_problem(jobs["military"], {"kind": "NUCLEAR_STRIKE"})["error"] == "ROLE_ACTION_NOT_PERMITTED"
        )
        swap = {"kind": "SWAP_UNITS", "target": "scout", "destination": {"x": 0, "y": 0}}
        assert roles.command_problem(jobs["military"], swap)["error"] == "UNIT_OWNED_BY_OTHER_ROLE"
        assert (
            roles.command_problem(jobs["gameplay"], {"kind": "MOVE_AUTOMATED_UNITS"})["error"]
            == "ROLE_ACTION_NOT_PERMITTED"
        )
        roles.ready["gameplay"] = True
        await roles.synchronize_war(position(war=False))
        assert not roles.military_active and not roles.ready["military"]
        assert not jobs["military"].gameplay_ended.is_set()
        assert not roles.ready["gameplay"]
        # The same manager retains the army and can review peacetime priorities.
        message = await roles.role_tool(
            jobs["gameplay"],
            "role_message",
            {"to": "military", "text": "Peace was enacted; review readiness and staging."},
            "peace-notification",
        )
        assert "error" not in message
        assert roles.war_state["messages"][-1]["text"] == "Peace was enacted; review readiness and staging."
        assert not roles.ready["military"]
        assert roles.resumes["military"].is_set()
        assert roles.unit_owner({"handle": "scout", "military": True}) == "military"
        await roles.tool(jobs["military"], "finish_role", {})
        await roles.tool(jobs["economy"], "finish_role", {})
        await roles.tool(jobs["gameplay"], "finish_role", {})
        assert all(roles.ready.values())
    finally:
        await io.close()


def test_native_escort_inherited_persists_after_unpairing(tmp_path):
    wars = store(tmp_path)
    view = position()
    view["map"]["units"][0]["escort"] = {"partner": "worker", "active": True}
    wars.synchronize("e1", view)
    state = wars.synchronize("e1", position())
    assert "soldier" in state["escorts"]


def test_military_configuration_inherits_and_overrides():
    config = GameConfig()
    settings = config.settings(Player(name="Rome", military_model={"reasoning_effort": "high"}))
    assert settings["military_model"]["reasoning_effort"] == "high"
    assert settings["military_model"]["model"] == settings["model"]


@pytest.mark.parametrize(
    "tool,payload,selected,required",
    [
        ("player_command", {"kind": "CITY_STATE_ACTION", "selection": "makePeace", "target": "Geneva"}, None, True),
        (
            "trade_offer",
            {"partner": "Greece", "give": [{"type": "Treaty", "name": "Peace Treaty"}], "receive": []},
            None,
            True,
        ),
        ("trade_decide", {"decision": "accept"}, {"give": [{"type": "PeaceProposal", "name": "Egypt"}]}, True),
        ("trade_decide", {"decision": "decline"}, {"give": [{"type": "Treaty", "name": "Peace Treaty"}]}, False),
        ("chat_send", {"text": "We promise peace"}, None, False),
        ("trade_offer", {"give": [{"type": "Gold", "amount": 5}]}, None, False),
    ],
)
def test_binding_peace_paths(tool, payload, selected, required):
    assert (peace_terms(tool, payload, selected) is not None) == required


@run_async
@pytest.mark.parametrize(
    "failure,approve,stale", [(False, True, False), (True, True, False), (False, False, False), (False, True, True)]
)
async def test_peace_requires_overview_then_main_exact_terms(tmp_path, failure, approve, stale):
    wars = store(tmp_path)
    io = ServiceIO()
    hold = asyncio.Event()
    hold.set()
    gateway = SimpleNamespace(
        role_dispatch=asyncio.Lock(), wars=wars, jobs={}, consultation_hold=hold, suspend_negotiation=lambda job: None
    )
    service = SimpleNamespace(
        memory=wars.memory, io=io, gateway=gateway, records={}, chat=SimpleNamespace(running=False)
    )
    reviews = PeaceReviews(service)
    calls = []
    view = position()

    async def compute(caller, role, inputs, schema):
        calls.append(role)
        assert not hold.is_set()
        if failure:
            raise ValueError("MODEL_FAILED")
        if stale and role == "gameplay":
            view["map"]["units"][0]["health"] = 50
        return (
            {"recommendation": "Peace"} if role == "military" else {"approve": approve, "reason": "Protect our cities"}
        )

    reviews.compute = compute

    async def read(*_):
        return copy.deepcopy(view)

    job = SimpleNamespace(id="diplomat", epoch="e1", purpose="communication", connection=SimpleNamespace(tool=read))
    payload = {"kind": "CITY_STATE_ACTION", "selection": "makePeace", "target": "Geneva"}
    try:
        result = await reviews.authorize(job, "player_command", payload)
        assert hold.is_set()
        if failure:
            assert calls == ["military"] and result["error"] == "PEACE_CONSULTATION_UNAVAILABLE"
        else:
            assert calls == ["military", "gameplay"]
            if not approve:
                assert result["error"] == "MAIN_DECLINED_PEACE"
            elif stale:
                assert result["error"] == "PEACE_REVIEW_STALE"
            else:
                assert result is None and payload["revision"] == "r1"
                await reviews.authorize(job, "player_command", payload)
                assert calls == ["military", "gameplay"], "Exact valid approval is reused"
    finally:
        await io.close()


def test_peace_basis_binds_goals_terms_and_tactical_facts(tmp_path):
    wars = store(tmp_path)
    state = wars.synchronize("e1", position())
    view = position()
    basis = tactical_basis(view, state, {"gold": 10})
    view["revision"] = "unrelated-read-id"
    assert tactical_basis(view, state, {"gold": 10}) == basis
    assert tactical_basis(view, state, {"gold": 11}) != basis
    next(iter(state["episodes"].values()))["version"] += 1
    assert tactical_basis(view, state, {"gold": 10}) != basis


@run_async
async def test_three_actual_role_conversations_join_one_native_handoff(tmp_path):
    from civ_agent.agent_service import AgentService
    from tests.test_agent_service import FakeCodex, FakeFactory, StartupHost

    class WarHost(StartupHost):
        async def tool(self, name, args):
            result = await super().tool(name, args)
            if name == "player_turn":
                result["diplomacy"] = position()["diplomacy"]
            return result

    class Model(FakeCodex):
        async def start(self, token, settings):
            await super().start(token, settings)
            assert self.job is not None
            if self.job.purpose == "military":
                self.economy = True  # Existing fake implements a subordinate role protocol.

        async def request(self, method, payload):
            assert self.job is not None
            if method == "turn/start" and self.job.purpose == "gameplay":
                result = await self.factory.service.gateway.invoke(
                    self.job,
                    "war_goals",
                    {"action": "initial", "opponent": "Greece", "goals": GOALS, "reason": "Defend our border"},
                    "initial",
                )
                assert "error" not in result
            return await super().request(method, payload)

    class Factory(FakeFactory):
        def __call__(self, executable, workspace, mcp_url, ca_file, notification):
            model = Model(self, workspace, notification)
            self.instances.append(model)
            return model

        def connection(self, *args, **kwargs):
            return WarHost(self)

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
    try:
        await service.start(
            {
                "activationId": "turn1",
                "epoch": "g1",
                "civilization": "Rome",
                "threadId": None,
                "gameTurn": 0,
                "handoff": 0,
                "mcpUrl": "https://localhost:8765/mcp",
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
        assert {r.get("purpose", "gameplay") for r in service.records.values()} == {"gameplay", "economy", "military"}
        assert service.turn_roles is not None
        assert service.turn_roles.committed
        assert all(service.turn_roles.ready.values())
        assert all(process.closed for process in factory.instances)
    finally:
        await service.close()


def test_canonical_incoming_peace_includes_all_exact_terms():
    selected = {
        "proposal": "d1",
        "version": 2,
        "author": "Greece",
        "partner": "Rome",
        "clauses": [
            {
                "from": "Rome",
                "to": "Greece",
                "item": {"type": "Treaty", "name": "Peace Treaty", "amount": 1, "duration": 10},
            },
            {"from": "Rome", "to": "Greece", "item": {"type": "Gold", "name": "Gold", "amount": 50, "duration": -1}},
        ],
    }
    terms = peace_terms("trade_decide", {"decision": "accept"}, selected)
    assert terms is not None
    assert terms["clauses"] == selected["clauses"]
    assert terms["proposal"] == "d1" and terms["version"] == 2


def test_declaration_paths_include_native_replies_and_canonical_trades():
    from civ_agent.war_state import declaration_targets

    view = {
        "alerts": [
            {
                "handle": "alert1",
                "nativeDiplomacy": {"civilizationId": "Greece", "options": [{"name": "Refuse", "declaresWar": True}]},
            }
        ]
    }
    assert declaration_targets(
        "Rome",
        "chat_send",
        {"nativeAction": {"kind": "RESPOND_DIPLOMACY", "target": "alert1", "selection": "Refuse"}},
        None,
        view,
    ) == {"Greece"}
    selected = {"clauses": [{"from": "Rome", "to": "Greece", "item": {"type": "WarDeclaration", "name": "Egypt"}}]}
    assert declaration_targets("Rome", "trade_decide", {"decision": "accept"}, selected, {}) == {"Egypt"}
    assert not declaration_targets("Rome", "trade_decide", {"decision": "decline"}, selected, {})


@run_async
async def test_real_readonly_peace_conversation_and_accounting(tmp_path):
    import json

    from civ_agent.agent_service import AgentService
    from civ_agent.peace_review import TacticalOverview
    from tests.test_agent_service import FakeFactory

    class DecisionModel:
        def __init__(self, factory, notification):
            self.factory, self.notification = factory, notification
            self.closed = False

        async def start(self, token, settings):
            self.job = self.factory.service.gateway.jobs[token]
            assert await self.factory.service.gateway.tools(self.job) == []
            denied = await self.factory.service.gateway.call(self.job, "player_command", {"kind": "END_TURN"}, "attack")
            assert denied["error"] == "REVIEW_READ_ONLY"

        async def request(self, method, payload):
            if method == "thread/start":
                return {
                    "thread": {"id": "consult-thread"},
                    "modelProvider": payload["modelProvider"],
                    "model": payload.get("model") or "configured-model",
                    "reasoningEffort": "medium",
                }
            if method == "turn/start":
                answer = {
                    name: "Observed facts; inferred continuation costs; unknown reinforcements."
                    for name in TacticalOverview.model_fields
                }
                await self.notification(
                    "item/completed",
                    {
                        "threadId": "consult-thread",
                        "turnId": "c1",
                        "item": {
                            "id": "answer",
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": json.dumps(answer),
                        },
                    },
                )
                await self.notification(
                    "thread/tokenUsage/updated",
                    {
                        "threadId": "consult-thread",
                        "turnId": "c1",
                        "tokenUsage": {
                            "total": {
                                "inputTokens": 100,
                                "cachedInputTokens": 20,
                                "outputTokens": 30,
                                "reasoningOutputTokens": 10,
                            }
                        },
                    },
                )
                await self.notification(
                    "turn/completed", {"threadId": "consult-thread", "turn": {"id": "c1", "status": "completed"}}
                )
                return {"turn": {"id": "c1"}}
            raise AssertionError(method)

        async def close(self):
            self.closed = True
            await self.notification("service/disconnected", {})

    class Factory(FakeFactory):
        def __call__(self, executable, workspace, mcp_url, ca_file, notification):
            process = DecisionModel(self, notification)
            self.instances.append(process)
            return process

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
    try:
        service.memory.bind("e1")
        caller = SimpleNamespace(id="diplomat", epoch="e1")
        result = await service.peace_reviews.compute(
            caller, "military", {"position": position(), "warState": {}, "exactPeaceTerms": {}}, TacticalOverview
        )
        assert result["recommendation"]
        record = next(iter(service.records.values()))
        assert record["purpose"] == "consultation" and record["phase"] == "COMPLETED"
        assert not record.get("error"), "An expected disconnect during shutdown is not a consultation failure"
        assert record["usage"]["input_tokens"] == 100
        assert record["modelRequests"] == 1
        assert factory.instances[0].closed
        assert not service.gateway.jobs
    finally:
        await service.close()


def test_war_records_follow_checkpoint_rewind_and_reject_changed_retry(tmp_path):
    wars = store(tmp_path)
    wars.synchronize("e1", position())
    saved = tmp_path / "checkpoint.json"
    checksum = wars.memory.backup(saved)
    args = {"action": "initial", "opponent": "Greece", "goals": GOALS, "reason": "Defend"}
    assert "initialGoals" in wars.apply("e1", "gameplay", "war_goals", args, "initial")
    assert (
        wars.apply("e1", "gameplay", "war_goals", {**args, "reason": "Different"}, "initial")["error"]
        == "REQUEST_ID_CONFLICT"
    )
    wars.memory.restore(saved, checksum, "rewound")
    episode = next(iter(wars.state()["episodes"].values()))
    assert episode["version"] == 0 and not episode["history"]
    with pytest.raises(ValueError, match="STALE_EPOCH"):
        wars.apply("e1", "gameplay", "war_goals", args, "stale")


@run_async
async def test_military_reconsiders_new_goals_before_spending_movement(tmp_path):
    memory = CivilizationState(tmp_path, "Rome")
    memory.bind("e1")
    io = ServiceIO()
    service = SimpleNamespace(memory=memory, io=io, records={}, _save=AsyncMock())
    roles = TurnRoles(service, {"epoch": "e1", "gameTurn": 4, "activationId": "main"})
    job = SimpleNamespace(purpose="military", epoch="e1", observation=position(), war_versions={})
    roles.register(job)
    try:
        await roles.synchronize_war(position())
        move = {"kind": "MOVE_UNIT", "target": "soldier", "destination": {"x": 1, "y": 0}}
        blocked = roles.command_problem(job, move)
        assert blocked["error"] == "WAR_GOALS_CHANGED" and not blocked["applied"]
        assert roles.command_problem(job, move) is None
        episode = next(iter(roles.war_state["episodes"].values()))
        episode["version"] += 1
        assert roles.command_problem(job, move)["error"] == "WAR_GOALS_CHANGED"
    finally:
        await io.close()


def test_container_provisions_subordinate_provider_credentials():
    from civ_agent.container_supervisor import player_providers

    config = GameConfig()
    player = Player(name="Rome", military_model={"provider": "openrouter", "model": "vendor/model"})
    assert player_providers(config, player) == {"openai", "openrouter"}
    assert player_providers(config, Player(name="Greece")) == {"openai"}


@run_async
async def test_required_consultation_wait_does_not_exhaust_diplomacy_budget():
    import time

    from civ_agent.chat_runtime import ChatRuntime
    from civ_agent.proposal_gateway import JobAccess
    from civ_agent.review_runtime import OfflineConnection

    runtime = ChatRuntime(None)
    runtime.timeout = 1
    job = JobAccess("diplomat", "communication", "e1", OfflineConnection(), {})

    async def compute(id):
        runtime.jobs[id] = (job, None)
        job.wait_started = time.monotonic()
        await asyncio.sleep(1.5)
        job.completed_wait += time.monotonic() - job.wait_started
        job.wait_started = None
        await asyncio.sleep(0.02)

    runtime.compute = compute
    await runtime.compute_with_budget("diplomat")
    assert job.completed_wait >= 1.5
