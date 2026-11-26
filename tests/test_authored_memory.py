"""Native notebook tools retain public CAS, receipts and trusted role boundaries."""

import asyncio
import json
import re
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.capabilities import Caching
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.memory import MemoryConflictError, MemoryOperation, MemoryOperationConflictError

from civ_agent.orchestration.authored_documents import AuthoredDocuments
from civ_agent.orchestration.authored_memory import (
    AuthoredMemoryService,
    GatewayMemoryStore,
    authored_memory_capability,
    authored_prefix,
    contribution_prefix,
    decode_document,
)
from civ_agent.orchestration.contracts import GatewayDependencies, GatewayJob, PlayerRole, Scope, digest
from civ_agent.orchestration.gateway_registry import GatewayRegistry
from civ_agent.orchestration.store import JournalConflict
from tests.domain_fixtures import managed
from tests.test_gateway_execution import executor


def operation(name):
    return MemoryOperation(id=digest(name), fingerprint=digest(["arguments", name]))


@pytest.mark.parametrize("state", ["owned", "lost", "reassigned", "unavailable"])
def test_city_plan_checks_live_ownership_after_invalidation_without_delivery_credit(tmp_path, state):
    async def run():
        async with setup(tmp_path, purpose="economy") as fixture:
            job, gateway = fixture.job, fixture.domain.gateway
            gateway.invalidate_role_observation(job)
            prior = job.prior_role_observation
            details = list(job.details_read)
            if state == "lost":
                fixture.domain.host.view["cities"] = [
                    row for row in fixture.domain.host.view["cities"] if row["handle"] != "city2"
                ]
            elif state == "reassigned":
                gateway.role_coordinator.overrides["city2"] = {"reason": "Main handles this city"}
            elif state == "unavailable":

                async def unavailable(_name, _args):
                    return {"error": "UNAVAILABLE"}

                job.connection.tool = unavailable
            content = json.dumps({"type": "city_plan", "city": "city2", "reasons": {"growth": "Grow"}})
            path = fixture.prefix + "city-city2.md"
            if state == "owned":
                saved = await fixture.proxy.write(path, content, expected_version=None, operation=operation("save"))
                assert saved.version
                await fixture.proxy.delete(path, expected_version=saved.version, operation=operation("delete"))
            elif state == "unavailable":
                with pytest.raises(JournalConflict, match="AUTHORED_CITY_STATE_UNAVAILABLE"):
                    await fixture.proxy.write(path, content, expected_version=None, operation=operation("save"))
            else:
                with pytest.raises(ModelRetry, match="A city plan must name"):
                    await fixture.proxy.write(path, content, expected_version=None, operation=operation("save"))
            assert job.observation is None and job.prior_role_observation is prior
            assert job.details_read == details and job.role_refresh_required
            assert await fixture.service.store.read(path, max_chars=8000) is None

    asyncio.run(run())


@asynccontextmanager
async def setup(tmp_path, *, purpose: PlayerRole = "gameplay"):
    async with managed(tmp_path / "domain") as domain:
        job = domain.main if purpose == "gameplay" else domain.economy
        if purpose == "communication":
            job = await domain.gateway.open("communication", "communication", "g1", domain.host, {}, 0)
        _, state = await executor(domain, job)
        identity = GatewayJob(
            scope=Scope(game="game", player="p0", epoch="g1", activation="authored"), job=job.id, purpose=purpose
        )

        async def resolve(record):
            return job

        registry = GatewayRegistry(domain.gateway, game="game", player="p0", resolve=resolve)
        await registry.register(identity, state, await domain.gateway.tools(job))
        service = AuthoredMemoryService(registry, tmp_path / "authored.sqlite3")

        class Client:
            async def post(self, name, request):
                assert name == "memory"
                return (await service.invoke(request)).model_dump(mode="json")

        client = Client()
        proxy = GatewayMemoryStore(client, identity)
        try:
            yield SimpleNamespace(
                domain=domain,
                job=job,
                registry=registry,
                service=service,
                identity=identity,
                client=client,
                proxy=proxy,
                prefix=authored_prefix(identity.scope),
                path=tmp_path / "authored.sqlite3",
            )
        finally:
            await registry.close()


def test_native_tools_write_read_search_over_authorized_local_service_and_keep_provenance(tmp_path):
    async def run():
        async with setup(tmp_path) as fixture:
            client = fixture.client
            calls = []
            content = json.dumps({"type": "note", "text": "May renew the offer; this is not a promise."})

            def model(messages, info):
                calls.append(messages)
                if len(calls) == 1:
                    return ModelResponse(
                        parts=[ToolCallPart("write_memory", {"file": "note-offer.md", "content": content}, "write")]
                    )
                if len(calls) == 2:
                    return ModelResponse(parts=[ToolCallPart("read_memory", {"file": "note-offer.md"}, "read")])
                if len(calls) == 3:
                    return ModelResponse(parts=[ToolCallPart("search_memory", {"query": "renew"}, "search")])
                return ModelResponse(parts=[TextPart("done")])

            agent = Agent(
                FunctionModel(model),
                deps_type=GatewayDependencies,
                capabilities=[Caching(), authored_memory_capability(client)],
            )
            result = await agent.run(
                "Record a private assessment.", deps=GatewayDependencies(**fixture.identity.model_dump())
            )
            assert result.output == "done" and len(calls) == 4
            stored = await fixture.service.store.read(fixture.prefix + "note-offer.md", max_chars=8000)
            assert stored is not None and stored.content.strip() == content
            assert stored.metadata["job"] == fixture.identity.job and stored.metadata["purpose"] == "gameplay"
            assert stored.metadata["player"] == "p0" and stored.operation_id
            proxy = GatewayMemoryStore(client, fixture.identity)
            found = await proxy.search(
                fixture.prefix, "renew", limit=10, max_files=100, max_chars=1000, max_file_chars=8000
            )
            assert len(found.matches) == 1 and "not a promise" in found.matches[0].snippet
            assert await proxy.list_paths(fixture.prefix, limit=10) == [fixture.prefix + "note-offer.md"]

    asyncio.run(run())


def test_native_receipt_survives_service_reopen_and_retirement_without_authorizing_new_writes(tmp_path):
    async def run():
        async with setup(tmp_path) as fixture:
            path = fixture.prefix + "note-receipt.md"
            content = json.dumps({"type": "note", "text": "Keep exact authored text."})
            op = operation("first")
            first = await fixture.proxy.write(path, content, expected_version=None, operation=op)
            reopened = AuthoredMemoryService(fixture.registry, fixture.path)
            from civ_agent.orchestration.authored_memory import AuthoredMemoryRequest

            request = AuthoredMemoryRequest.model_validate(
                {
                    "identity": fixture.identity.model_dump(),
                    "call": {
                        "action": "write",
                        "path": path,
                        "content": content,
                        "expected_version": None,
                        "operation": {"id": op.id, "fingerprint": op.fingerprint},
                    },
                }
            )
            replay = await reopened.invoke(request)
            assert isinstance(replay.result, dict)
            assert replay.result["version"] == first.version and replay.result["replayed"]
            await fixture.registry.retire(fixture.identity)
            assert (await fixture.proxy.get_operation(op)).version == first.version
            assert (await fixture.proxy.write(path, content, expected_version=None, operation=op)).replayed
            with pytest.raises(JournalConflict, match="RETIRED"):
                await fixture.proxy.write(path, content, expected_version=first.version, operation=operation("fresh"))
            changed = MemoryOperation(id=op.id, fingerprint=digest("changed"))
            with pytest.raises(MemoryOperationConflictError):
                await fixture.proxy.get_operation(changed)
            with fixture.domain.memory.transaction() as db:
                fixture.domain.memory._set(db, "epoch", "g2")
            with pytest.raises(JournalConflict, match="EPOCH"):
                await fixture.proxy.get_operation(op)

    asyncio.run(run())


def test_native_cas_schema_namespace_and_trusted_metadata_reject_unsafe_edits(tmp_path):
    async def run():
        async with setup(tmp_path) as fixture:
            path = fixture.prefix + "belief-offer.md"
            content = json.dumps({"type": "belief", "claim": "Unverified statement", "uncertainty": "high"})
            first = await fixture.proxy.write_with_metadata(
                path,
                content,
                expected_version=None,
                operation=operation("write"),
                metadata={"purpose": "military", "job": "forged"},
            )
            stored = await fixture.proxy.read(path, max_chars=8000)
            assert stored.metadata["purpose"] == "gameplay" and stored.metadata["job"] != "forged"
            with pytest.raises(MemoryConflictError):
                await fixture.proxy.write(path, content, expected_version=None, operation=operation("stale"))
            with pytest.raises(ModelRetry, match="Document JSON"):
                await fixture.proxy.write(
                    fixture.prefix + "strategy.md",
                    '{"type":"strategy"}',
                    expected_version=None,
                    operation=operation("invalid"),
                )
            with pytest.raises(JournalConflict):
                await fixture.proxy.read("other/p0/g1/notes/belief-offer.md", max_chars=8000)
            with pytest.raises(ModelRetry):
                await fixture.proxy.read(fixture.prefix + "../belief.md", max_chars=8000)
            with pytest.raises(JournalConflict):
                await fixture.proxy.list_paths("", limit=10)
            assert (await fixture.proxy.read(path, max_chars=8000)).version == first.version

    asyncio.run(run())


def test_economy_native_city_plan_write_and_delete_use_actual_owned_city_policy(tmp_path):
    async def run():
        async with setup(tmp_path, purpose="economy") as fixture:
            fixture.domain.roles.overrides["city1"] = "Main manages this city."
            content = json.dumps({"type": "city_plan", "city": "city2", "reasons": {"construction": "Retain queue"}})
            path = fixture.prefix + "city-city2.md"
            first = await fixture.proxy.write(path, content, expected_version=None, operation=operation("own-city"))
            with pytest.raises(ModelRetry, match="city plan"):
                await fixture.proxy.write(
                    fixture.prefix + "city-city1.md",
                    content.replace("city2", "city1"),
                    expected_version=None,
                    operation=operation("other-city"),
                )
            with pytest.raises(ModelRetry, match="role"):
                await fixture.proxy.write(
                    fixture.prefix + "note-role.md",
                    json.dumps({"type": "note", "text": "No global edits"}),
                    expected_version=None,
                    operation=operation("role"),
                )
            fixture.domain.roles.overrides["city2"] = "Main took ownership after the plan was saved."
            with pytest.raises(ModelRetry, match="city plan"):
                await fixture.proxy.delete(
                    path, expected_version=first.version, operation=operation("delete-after-transfer")
                )
            fixture.domain.roles.overrides.pop("city2")
            assert (
                await fixture.proxy.delete(path, expected_version=first.version, operation=operation("delete-own"))
            ).existed

    asyncio.run(run())


def test_communication_native_contributions_are_immutable_and_never_become_shared_strategy(tmp_path):
    async def run():
        async with setup(tmp_path, purpose="communication") as fixture:
            content = json.dumps({"type": "strategy", "assessment": "A suggestion, not an adopted plan"})
            path = fixture.prefix + contribution_prefix(fixture.identity) + "offer.md"
            first = await fixture.proxy.write(path, content, expected_version=None, operation=operation("contribute"))
            assert decode_document(path, await fixture.proxy.read(path, max_chars=8000))["category"] == "contribution"
            with pytest.raises(ModelRetry, match="immutable"):
                await fixture.proxy.write(path, content, expected_version=first.version, operation=operation("amend"))
            with pytest.raises(ModelRetry, match="role"):
                await fixture.proxy.write(
                    fixture.prefix + "strategy.md", content, expected_version=None, operation=operation("shared")
                )
            assert (
                await fixture.proxy.write(path, content, expected_version=None, operation=operation("contribute"))
            ).replayed

    asyncio.run(run())


def test_lazy_client_and_contribution_schema_are_resolved_at_native_tool_execution(tmp_path):
    async def run():
        async with setup(tmp_path, purpose="communication") as fixture:
            resolutions, requests = [], []

            def client():
                resolutions.append("activity")
                return fixture.client

            def model(messages, info):
                requests.append(messages)
                instructions = info.instructions or ""
                assert len(instructions) < 10000
                assert "native Planning tools" in instructions
                assert "cannot be edited/deleted" in instructions
                prefix = re.search(r"files named (contribution-[a-f0-9]{16}-)<label>\.md", instructions)
                assert prefix is not None
                schemas, _ = json.JSONDecoder().raw_decode(instructions.split("Document schemas: ", 1)[1])
                assert schemas == {}
                assert "plain Markdown suggestions" in instructions
                if len(requests) == 1:
                    assert not resolutions  # Agent/run construction and scope resolution perform no RPC.
                    return ModelResponse(
                        parts=[
                            ToolCallPart(
                                "write_memory",
                                {
                                    "file": prefix.group(1) + "offer.md",
                                    "content": json.dumps(
                                        {"type": "belief", "claim": "The offer might be renewed", "uncertainty": "high"}
                                    ),
                                },
                                "contribution",
                            )
                        ]
                    )
                return ModelResponse(parts=[TextPart("done")])

            capability = authored_memory_capability(client)
            agent = Agent(FunctionModel(model), deps_type=GatewayDependencies, capabilities=[Caching(), capability])
            assert not resolutions
            result = await agent.run(
                "Record an uncertain suggestion.", deps=GatewayDependencies(**fixture.identity.model_dump())
            )
            assert result.output == "done" and resolutions
            paths = await fixture.service.store.list_paths(fixture.prefix, limit=10)
            assert paths == [fixture.prefix + contribution_prefix(fixture.identity) + "offer.md"]

    asyncio.run(run())


def test_strategy_baselines_are_native_metadata_and_reader_is_transient_and_opaque(tmp_path):
    async def run():
        async with setup(tmp_path) as fixture:
            choice = {
                "id": "growth",
                "objective": "Grow",
                "investment": "Granary",
                "expectedObservations": ["More food"],
                "reviewAfterTurns": 3,
                "reconsiderWhen": "Threats",
            }
            content = {"type": "strategy", "assessment": "Grow safely", "decisions": [choice]}
            path = fixture.prefix + "strategy.md"
            first = await fixture.proxy.write(
                path, json.dumps(content), expected_version=None, operation=operation("strategy")
            )
            initial = decode_document(path, await fixture.proxy.read(path, max_chars=8000))
            content["assessment"] = "Same chosen investment, revised prose"
            second = await fixture.proxy.write(
                path, json.dumps(content), expected_version=first.version, operation=operation("prose")
            )
            same = decode_document(path, await fixture.proxy.read(path, max_chars=8000))
            assert same["body"]["_decisionBaselines"] == initial["body"]["_decisionBaselines"]
            choice["investment"] = "Worker"
            await fixture.proxy.write(
                path, json.dumps(content), expected_version=second.version, operation=operation("changed-investment")
            )
            reader = AuthoredDocuments(fixture.path)
            try:
                records = await asyncio.to_thread(reader.read, fixture.identity.scope)
                baseline = records["strategy.md"]["body"]["_decisionBaselines"]["growth"]
                assert baseline["id"] != initial["body"]["_decisionBaselines"]["growth"]["id"]
                assert baseline["position"] is None
                assert isinstance(records["strategy.md"]["version"], str)
                records["strategy.md"]["body"]["assessment"] = "Local projection only"
                assert (
                    "Local projection"
                    not in (await asyncio.to_thread(reader.read, fixture.identity.scope))["strategy.md"]["body"][
                        "assessment"
                    ]
                )
            finally:
                reader.close()

    asyncio.run(run())
