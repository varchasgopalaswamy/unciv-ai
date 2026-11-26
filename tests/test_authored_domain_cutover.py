"""Native files are authored authority; domain projections grant no delivery credit."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from civ_agent import memory_schema as tables
from civ_agent.civilization_state import CivilizationState, MemoryFormatError
from civ_agent.economy_roles import TurnRoles
from civ_agent.economy_schedule import EconomySchedule
from civ_agent.memory_records import MEMORY_TYPES
from civ_agent.memory_snapshots import load_snapshot
from civ_agent.memory_store import RecordStore
from civ_agent.operator_connection import Operator
from civ_agent.orchestration.authored_documents import AuthoredDocuments
from civ_agent.orchestration.authored_memory import (
    AuthoredMemoryService,
    AuthoredMetadata,
    GatewayMemoryStore,
    authored_prefix,
    document_kind,
)
from civ_agent.orchestration.communication import NativeCommunication
from civ_agent.orchestration.communication_store import CommunicationStore, communication_scope
from civ_agent.orchestration.contracts import (
    CommunicationBatch,
    CommunicationRun,
    GatewayJob,
    ModelSelection,
    PrepareRole,
    Scope,
    TurnTicket,
)
from civ_agent.orchestration.coordinator import NativeCoordinator
from civ_agent.orchestration.store import WorkflowStore
from tests.domain_fixtures import Host, StartupHost, observation, run_async
from tests.test_authored_memory import operation
from tests.test_communication_store import event


@asynccontextmanager
async def domain(tmp_path):
    memory = CivilizationState(tmp_path / "domain", "Rome")
    memory.bind("g1")
    reader = AuthoredDocuments(tmp_path / "authored.sqlite3")
    scope = Scope(game="game", player="p0", epoch="g1", activation="test")
    reads = []

    def read(epoch, turn):
        reads.append((epoch, turn))
        return reader.read(scope.model_copy(update={"epoch": epoch}), turn)

    memory.enable_authored(read)
    with memory.transaction() as db:
        memory._set(db, "gameTurn", 4)
    try:
        yield SimpleNamespace(memory=memory, reader=reader, scope=scope, reads=reads)
    finally:
        reader.close()
        memory.engine.dispose()


async def write(m, filename, body, *, version=None, turn=4, baselines=None):
    metadata = AuthoredMetadata(
        game=m.scope.game,
        player=m.scope.player,
        epoch=m.scope.epoch,
        kind=document_kind(filename),
        job="native-author",
        purpose="gameplay",
        createdTurn=turn,
        updatedTurn=turn,
        decisionBaselines=json.dumps(baselines or {}),
    )
    return await m.reader.store.write_with_metadata(
        authored_prefix(m.scope) + filename,
        body if isinstance(body, str) else json.dumps(body),
        expected_version=version,
        metadata=metadata.strings(),
    )


def choice(label="growth"):
    return {
        "id": label,
        "objective": "Grow the city",
        "investment": "Prioritize food",
        "expectedObservations": ["Population increases"],
        "reviewAfterTurns": 2,
        "reconsiderWhen": "Food becomes scarce",
        "subjects": ["city1"],
    }


@run_async
async def test_native_records_are_transaction_cached_filtered_copied_and_never_written_to_domain_sql(tmp_path):
    async with domain(tmp_path) as m:
        mutation = await write(m, "strategy.md", {"type": "strategy", "assessment": "Grow", "decisions": [choice()]})
        await write(
            m, "city-city1.md", {"type": "city_plan", "city": "city1", "reasons": {"construction": "Keep Worker"}}
        )
        with m.memory.transaction() as db:
            connection_info = db.info
            first = RecordStore(db).all(categories={"strategy"})
            assert set(first) == {"strategy.md"} and first["strategy.md"]["version"] == mutation.version
            assert isinstance(mutation.version, str)
            first["strategy.md"]["body"]["decisions"][0]["objective"] = "Mutated local projection"
            second = RecordStore(db).all(keys=["strategy.md"])
            assert second["strategy.md"]["body"]["decisions"][0]["objective"] == "Grow the city"
            assert not RecordStore(db).all(categories={"city_plan"}, keys=["strategy.md"])
            assert set(RecordStore(db).all(categories={"city_plan"})) == {"city-city1.md"}
            assert len(m.reads) == 1
            for category in MEMORY_TYPES | {"contribution"}:
                with pytest.raises(ValueError, match="NATIVE_AUTHORED_MEMORY_REQUIRED"):
                    RecordStore(db).write("forged", category, {})
            assert db.scalar(select(func.count()).select_from(tables.records)) == 0
        assert "authored_reader" not in connection_info and "authored_records" not in connection_info
        updated = await write(
            m, "strategy.md", {"type": "strategy", "assessment": "Reconsider"}, version=mutation.version
        )
        with m.memory.transaction() as db:
            assert RecordStore(db).all(keys=["strategy.md"])["strategy.md"]["version"] == updated.version
        assert len(m.reads) == 2
        with pytest.raises(RuntimeError, match="abort assessment"):
            with m.memory.transaction() as db:
                aborted_info = db.info
                RecordStore(db).all(categories={"strategy"})
                raise RuntimeError("abort assessment")
        assert "authored_reader" not in aborted_info and "authored_records" not in aborted_info


@run_async
async def test_typed_expiry_is_readonly_and_plain_native_notes_have_no_invented_lifecycle(tmp_path):
    async with domain(tmp_path) as m:
        plan = await write(m, "city-city1.md", {"type": "city_plan", "city": "city1", "reasons": {}, "expiresTurn": 5})
        note = await write(m, "MEMORY.md", "Plain notes with no automatic expiry or procedural handoff")
        with m.memory.transaction() as db:
            m.memory._set(db, "gameTurn", 6)
        with m.memory.transaction() as db:
            projected = RecordStore(db).all()
            RecordStore(db, 6).expire()
            assert projected["city-city1.md"]["body"]["state"] == "retired"
            assert projected["MEMORY.md"]["body"]["state"] == "active"
            assert projected["city-city1.md"]["version"] == plan.version
            assert projected["MEMORY.md"]["version"] == note.version
            assert not db.execute(tables.records.select()).all()
        raw = await m.reader.store.read(authored_prefix(m.scope) + "city-city1.md", max_chars=8000)
        assert raw.metadata["state"] == "active" and raw.version == plan.version


@run_async
async def test_economy_schedule_uses_native_plan_purchase_targets_and_deadline_without_legacy_copy(tmp_path):
    async with domain(tmp_path) as m:
        plan = {
            "type": "city_plan",
            "city": "city1",
            "reasons": {"purchases": "Buy Granary when affordable"},
            "purchaseTargets": ["Granary"],
            "reviewTurn": 7,
        }
        first = await write(m, "city-city1.md", plan)
        view, schedule = observation(), EconomySchedule(m.memory, 5)
        assert schedule.complete(view, 4)
        view["cities"][0]["purchases"]["constructions"][0]["available"] = True
        status = schedule.assess(view, 5, [])
        assert "Planned purchase affordable: Rome" in status["reasons"]
        assert schedule.complete(view, 5, view, cities=["city1"], full=False)
        assert not schedule.assess(view, 6, [])["reasons"]
        assert schedule.assess(view, 7, [])["cities"] == ["city1"]
        raw = await m.reader.store.read(authored_prefix(m.scope) + "city-city1.md", max_chars=8000)
        assert raw.version == first.version and json.loads(raw.content) == plan
        with m.memory.transaction() as db:
            assert not db.execute(tables.records.select().where(tables.records.c.category == "city_plan")).all()


@run_async
async def test_native_snapshots_and_post_action_delivery_do_not_present_documents_or_invent_intent_source_credit(
    tmp_path,
):
    async with domain(tmp_path) as m:
        await write(
            m,
            "strategy.md",
            {"type": "strategy", "assessment": "Native background only", "decisions": [choice()]},
            baselines={"growth": {"id": "stable-choice", "authoredTurn": 4, "position": None}},
        )
        await write(m, "city-city1.md", {"type": "city_plan", "city": "city1", "reasons": {"focus": "Grow"}})
        snapshot = m.memory.start_job("main", "gameplay", {}, game_turn=4)
        assert not snapshot["records"] and snapshot["contextIds"] == [] and not snapshot["delivered"]
        payload = {
            "requestId": "native",
            "epoch": "g1",
            "revision": "before",
            "kind": "SET_CITY_FOCUS",
            "target": "city1",
            "selection": "FoodFocus",
        }
        prepared = {
            "operationId": "native",
            "transport": "player_command",
            "payload": payload,
            "turn": 4,
            "intent": {
                "cityPlan": "city-city1.md",
                "strategy": "strategy.md",
                "decision": "growth",
                "reason": "Chosen food",
            },
            "initiativeDecisions": [],
        }
        result = m.memory.record_native_outcome(
            "main",
            prepared,
            {"requestId": "native", "epoch": "g1", "kind": "SET_CITY_FOCUS", "actionSummary": "Food selected."},
        )
        assert result["intentFeedback"]["linked"] is False
        assert result["intentFeedback"]["unverifiedCityPlan"] == "city-city1.md"
        assert not m.memory.confirmed_city_actions()
        host = Host()
        view = await host.tool("player_turn", {})
        view["turn"] = 4
        _, reply = m.memory.deliver_player(
            view,
            "main",
            {"turn": 4},
            "after",
            "player_command",
            {},
            lambda records, changed: {"records": records, "changed": changed},
            record_call=False,
        )
        assert reply == {"records": {}, "changed": {}}
        with m.memory.transaction() as db:
            saved = load_snapshot(db, db.scalar(select(tables.jobs.c.snapshot).where(tables.jobs.c.id == "main")))
            assert all(row["category"] not in MEMORY_TYPES | {"contribution"} for row in saved["records"].values())
            assert "strategy.md" not in saved["delivered"] and "city-city1.md" not in saved["delivered"]
        feedback = m.memory.decision_feedback(view)
        assert feedback["choices"][0]["baseline"] == "stable-choice"
        assert feedback["choices"][0]["actions"] == []
        assert feedback["choices"][0]["observedChanges"]["available"] is False
        assert feedback["strategyHistory"]["available"] is False
        current = await m.reader.store.read(authored_prefix(m.scope) + "strategy.md", max_chars=8000)
        expired = await write(
            m,
            "strategy.md",
            {"type": "strategy", "assessment": "Expired", "decisions": [choice()], "expiresTurn": 4},
            version=current.version,
            baselines={"growth": {"id": "stable-choice", "authoredTurn": 4, "position": None}},
        )
        assert m.memory.decision_feedback(view)["choices"] == []
        await write(
            m,
            "strategy.md",
            {"type": "strategy", "assessment": "Replaced choice", "decisions": []},
            version=expired.version,
        )
        assert m.memory.decision_feedback(view)["choices"] == []


def test_enabling_native_authority_rejects_any_legacy_authored_rows(tmp_path):
    memory = CivilizationState(tmp_path / "domain", "Rome")
    try:
        with memory.transaction() as db:
            RecordStore(db).write("old", "note", {"type": "note", "text": "Old format", "state": "active"})
        with pytest.raises(MemoryFormatError, match="fresh match"):
            memory.enable_authored(lambda *_: {})
        assert not memory.authored_enabled
    finally:
        memory.engine.dispose()


@run_async
async def test_explicit_native_coordinator_optin_prepares_no_custom_memory_context_or_legacy_tools(tmp_path):
    store = WorkflowStore(tmp_path / "worker", player="p0")
    root = NativeCoordinator(
        game="game",
        player="p0",
        civilization="Rome",
        state_dir=tmp_path / "coordinator",
        store=store,
        operator=cast(Operator, SimpleNamespace()),
        host_ca=tmp_path / "unused-ca.pem",
    )
    assert not root.memory.authored_enabled
    reader = AuthoredDocuments(tmp_path / "authored.sqlite3")
    root.configure_authored_memory(reader)
    scope = Scope(game="game", player="p0", epoch="g1", activation="turn1")
    ticket = TurnTicket(scope=scope, turn=0, handoff=0, index=0)
    root.memory.bind("g1")
    store.bind_epoch("game", "g1")
    record = {
        "activationId": "turn1",
        "epoch": "g1",
        "gameTurn": 0,
        "settings": {},
        "roleJobs": {},
        "requests": {},
        "bootstraps": {},
        "ticket": ticket.model_dump(mode="json"),
    }
    root.record = record
    root.records["turn1"] = record
    root.game_state["request"] = {"selection": ModelSelection(model="test").model_dump(), "request_limit": 4}
    root.turn_roles = TurnRoles(root, record)
    root.gateway.role_coordinator = root.turn_roles
    host = StartupHost(SimpleNamespace(service=SimpleNamespace(records=root.records, active_id="turn1")))
    root.load_turn = AsyncMock(return_value=host)
    try:
        request = await root.prepare_role(PrepareRole(ticket=ticket, purpose="gameplay"))
        assert request is not None
        startup = store.get(request.scope, request.startup)
        catalogue = store.get(request.scope, request.catalogue)
        assert "memory" not in json.loads(startup["user"])
        assert not {"memory_read", "memory_search", "memory_update"} & {item["name"] for item in catalogue}
        registered = root.registry.load(GatewayJob(scope=request.scope, job=request.job, purpose=request.purpose))
        assert registered.state.data["memory"]["contextIds"] == []
        job = root.turn_roles.jobs["gameplay"]
        for tool in ("memory_read", "memory_search", "memory_update"):
            assert (await root.gateway.call(job, tool, {}, "forbidden-" + tool))["error"] == "TOOL_NOT_PERMITTED"
        service = AuthoredMemoryService(root.registry, tmp_path / "authored.sqlite3")

        class Client:
            async def post(self, name, value):
                assert name == "memory"
                return (await service.invoke(value)).model_dump(mode="json")

        binding = GatewayJob(scope=request.scope, job=request.job, purpose=request.purpose)
        proxy = GatewayMemoryStore(Client(), binding)
        path = authored_prefix(binding.scope) + "strategy.md"
        content = {"type": "strategy", "assessment": "Develop the city", "decisions": [choice()]}
        first = await proxy.write(path, json.dumps(content), expected_version=None, operation=operation("first"))
        view = await host.tool("player_turn", {})
        feedback = root.memory.decision_feedback(view)["choices"][0]
        assert feedback["version"] == first.version and feedback["baseline"]
        assert not feedback["observedChanges"]["available"]
        content["assessment"] = "Retain the same investment"
        second = await proxy.write(
            path, json.dumps(content), expected_version=first.version, operation=operation("second")
        )
        assert root.memory.decision_feedback(view)["choices"][0]["baseline"] == feedback["baseline"]
        deleted = await proxy.delete(path, expected_version=second.version, operation=operation("retire"))
        assert deleted.existed
        replay = await proxy.delete(path, expected_version=second.version, operation=operation("retire"))
        assert replay.replayed and replay.existed and replay.version == deleted.version
        assert root.memory.decision_feedback(view)["choices"] == []

        await proxy.write(
            authored_prefix(binding.scope) + "note-background.md",
            json.dumps({"type": "note", "text": "Private authored context loaded only through native tools"}),
            expected_version=None,
            operation=operation("note"),
        )
        communication = NativeCommunication(root, SimpleNamespace())
        root.communication = communication
        communication.connection = AsyncMock(return_value=host)
        inbox = CommunicationStore(root.memory, store, communication_scope("game", "p0", "g1"))
        inbox.ingest([event()], 1)
        chat_request = await communication.prepare(
            CommunicationBatch(
                run=CommunicationRun(scope=inbox.scope, selection=ModelSelection(model="test")), notices=inbox.pending()
            )
        )
        assert chat_request is not None
        assert "memory" not in json.loads(store.get(chat_request.scope, chat_request.startup)["user"])
        chat_catalogue = store.get(chat_request.scope, chat_request.catalogue)
        assert not {"memory_read", "memory_search", "memory_update"} & {item["name"] for item in chat_catalogue}
        with root.memory.transaction() as db:
            assert not db.execute(
                tables.records.select().where(tables.records.c.category.in_(MEMORY_TYPES | {"contribution"}))
            ).all()
    finally:
        await root.close()
        store.close()
