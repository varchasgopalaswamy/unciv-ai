"""Provider contracts and host execution agree at every decision boundary."""

import copy
import json
from unittest.mock import AsyncMock

import pytest
from jsonschema import ValidationError as SchemaError
from pydantic import ValidationError

from civ_agent.decision_contracts import DecisionContract, closed
from civ_agent.game_commands import COMMANDS, command_schema
from civ_agent.memory_records import MemoryUpdate
from civ_agent.proposal_gateway import READ_TOOLS
from civ_agent.startup_context import startup_instructions
from civ_agent.strict_schema import StrictSchema, UnsupportedSchema
from civ_agent.structured_decisions import DecisionLoop, continuation, journal
from tests.structured_helpers import decision_text
from tests.test_economy_optimization import managed
from tests.test_economy_roles import run_async


def sample(shape, definitions):
    if "$ref" in shape:
        return sample(definitions[shape["$ref"].split("/")[-1]], definitions)
    if "const" in shape:
        return shape["const"]
    if "enum" in shape:
        return shape["enum"][0]
    if choices := shape.get("anyOf", shape.get("oneOf")):
        return sample(choices[0], definitions)
    kind = shape.get("type")
    if kind == "object":
        return {key: sample(shape["properties"][key], definitions) for key in shape.get("required", [])}
    if kind == "array":
        return [sample(shape["items"], definitions) for _ in range(shape.get("minItems", 0))]
    return {"string": "value", "integer": max(1, shape.get("minimum", 0)), "number": 1, "boolean": True, "null": None}[
        kind
    ]


@run_async
async def test_role_contracts_are_closed_stable_and_preserve_every_operation(tmp_path):
    async with managed(tmp_path) as m:
        m.host.list_tools = AsyncMock(
            return_value=[{"name": name, "description": name} for name in READ_TOOLS | {"player_command"}]
        )
        military = await m.gateway.open("military", "military", "g1", m.host, {}, 0)
        m.roles.register(military)
        communication = await m.gateway.open("communication", "communication", "g1", m.host, {}, 0)
        for job in (m.main, m.economy, military, communication):
            job.catalogue = None
            catalogue = await m.gateway.tools(job)
            compact = DecisionContract(job.purpose, catalogue)
            verbose = DecisionContract(job.purpose, catalogue, compact=False)
            instructions = startup_instructions(
                {"purpose": job.purpose, "overviewView": job.purpose, "readInstructions": {}}
            )
            assert "delivery=full" not in instructions and "delivery=full" not in json.dumps(compact.schema)
            assert "recoverBaseline=missing" in instructions
            assert len(json.dumps(compact.schema)) < len(json.dumps(verbose.schema))
            assert compact.schema == DecisionContract(job.purpose, catalogue).schema
            assert compact.names == {t["name"] for t in catalogue} - {
                "inspect_bundle",
                "command_help",
                "finish_role",
                "economy_wait",
            }
            original = compact.codec.original
            wire = copy.deepcopy(compact.schema)
            definitions = compact.codec.definitions
            exercised = set()
            for variant in original["properties"]["decision"]["anyOf"]:
                kind = variant["properties"]["kind"]["const"]
                if kind not in {"inspect", "act"}:
                    value = sample(variant, definitions)
                    assert compact.parse(compact.encode(value)) == verbose.parse(verbose.encode(value))
                    continue
                for operation in variant["properties"]["operations"]["items"]["anyOf"]:
                    value = sample(operation, definitions)
                    # A mutation/read union has its default read variant in the
                    # canonical schema; execution forbids mixing the two.
                    if kind == "act" and value["tool"] == "war_goals":
                        value["arguments"] = {
                            "action": "initial",
                            "opponent": "Greece",
                            "goals": [
                                {
                                    "outcome": "Hold",
                                    "completion": "City secure",
                                    "acceptableCosts": "Limited",
                                    "reconsiderWhen": "Peace",
                                }
                            ],
                            "reason": "Defend",
                        }
                    if value["tool"] == "commitment_offer":
                        value["arguments"]["participants"] = ["Rome", "Greece"]
                    if value["tool"] == "memory_update":
                        value["arguments"]["changes"] = []
                    decision = {"kind": kind, "operations": [value]}
                    assert compact.parse(compact.encode(decision)) == verbose.parse(verbose.encode(decision))
                    exercised.add(value["tool"])
            assert exercised == compact.names
            assert compact.schema == wire
            assert compact.costs() == DecisionContract(job.purpose, catalogue).costs()


def test_unsupported_formats_and_reference_intersections_fail_closed():
    for field in (
        {"type": ["string", "null"]},
        {"type": "string", "format": "invented"},
        {"$ref": "#/$defs/name", "minLength": 2},
    ):
        with pytest.raises(UnsupportedSchema):
            StrictSchema({**closed({"field": field}), "$defs": {"name": {"type": "string"}}})


def test_definition_deduplication_checks_hash_collisions(monkeypatch):
    from civ_agent import strict_schema

    monkeypatch.setattr(strict_schema, "fingerprint", lambda _: "collision")
    with pytest.raises(UnsupportedSchema, match="collision"):
        StrictSchema(
            {
                **closed({"first": {"$ref": "#/$defs/first"}, "second": {"$ref": "#/$defs/second"}}),
                "$defs": {"first": {"type": "string"}, "second": {"type": "integer"}},
            }
        )


def test_every_command_variant_and_invalid_aliases():
    contract = DecisionContract("gameplay", [{"name": "player_command", "inputSchema": command_schema()}])
    schema = COMMANDS.json_schema()
    for kind, ref in schema["discriminator"]["mapping"].items():
        command = sample(schema["$defs"][ref.split("/")[-1]], schema["$defs"])
        command["kind"] = kind
        decision = {"kind": "act", "operations": [{"tool": "player_command", "arguments": {"command": command}}]}
        assert contract.parse(contract.encode(decision))["operations"][0]["arguments"] == command
    research = {
        "kind": "act",
        "operations": [
            {"tool": "player_command", "arguments": {"command": {"kind": "SET_RESEARCH_QUEUE", "queue": ["Mining"]}}}
        ],
    }
    valid = json.loads(contract.encode(research))
    arguments = valid["decision"]["operations"][0]["arguments"]
    for key, value in (("technology", "Mining"), ("technologies", ["Mining"]), ("selection", "Mining")):
        broken = copy.deepcopy(valid)
        body = broken["decision"]["operations"][0]["arguments"]["command"]
        body.pop("queue")
        body[key] = value
        with pytest.raises(SchemaError):
            contract.parse(json.dumps(broken))
    arguments["intent"] = "My reason"
    with pytest.raises(SchemaError):
        contract.parse(json.dumps(valid))


def test_nullable_default_and_typed_patch_preserve_omission_and_clear():
    contract = DecisionContract("gameplay", [{"name": "memory_update"}])
    for fields in ({"reviewTurn": None}, {"reviewTurn": 9}, {"priority": 4}, {"reasons": {"growth": "Grow now"}}):
        change = {"op": "amend", "id": "mem1", "fields": fields}
        decision = {"kind": "act", "operations": [{"tool": "memory_update", "arguments": {"changes": [change]}}]}
        result = contract.parse(contract.encode(decision))["operations"][0]["arguments"]
        assert result == {"changes": [change]}
        assert MemoryUpdate.model_validate(result).model_dump()["changes"][0]["fields"] == fields
    with pytest.raises(ValidationError):
        MemoryUpdate.model_validate({"changes": [{"op": "amend", "id": "mem1", "fields": {"invented": "arbitrary"}}]})


def test_inspection_transport_is_automatic_with_explicit_recovery_only():
    contract = DecisionContract("gameplay", [{"name": "unit_details"}])
    decision = {"kind": "inspect", "operations": [{"tool": "unit_details", "arguments": {"target": "u1"}}]}
    assert contract.parse(contract.encode(decision)) == decision
    for reason in ("missing", "mismatched"):
        recovery = copy.deepcopy(decision)
        recovery["operations"][0]["recoverBaseline"] = reason
        parsed = contract.parse(contract.encode(recovery))
        assert parsed["operations"][0]["arguments"] == {"target": "u1", "delivery": "full"}
    invalid = json.loads(contract.encode(decision))
    invalid["decision"]["operations"][0]["arguments"]["delivery"] = "full"
    with pytest.raises(SchemaError):
        contract.parse(json.dumps(invalid))


@pytest.mark.parametrize(
    "keyword,value",
    [
        ("allOf", []),
        ("uniqueItems", True),
        ("additionalProperties", True),
        ("patternProperties", {".*": {"type": "string"}}),
    ],
)
def test_unsupported_schema_fails_closed(keyword, value):
    with pytest.raises(UnsupportedSchema):
        StrictSchema({**closed({}), keyword: value})


@run_async
async def test_batch_validation_receipts_replay_and_ownership_stop(tmp_path):
    async with managed(tmp_path) as m:
        await m.gateway.call(m.main, "player_turn", {}, "initial")
        loop = DecisionLoop(m.gateway, m.main, DecisionContract("gameplay", await m.gateway.tools(m.main)), {})
        await loop.begin("position")
        valid = {
            "kind": "act",
            "operations": [
                {"tool": "city_override", "arguments": {"city": "city1", "reason": "Emergency"}},
                {
                    "tool": "player_command",
                    "arguments": {"command": {"kind": "QUEUE_CONSTRUCTION", "target": "city1", "selection": "Granary"}},
                },
            ],
        }
        malformed = json.loads(loop.contract.encode(valid))
        malformed["decision"]["operations"][1]["arguments"]["command"]["invented"] = True
        result = await loop.complete(json.dumps(malformed))
        assert result["error"] == "INVALID_STRUCTURED_DECISION" and not m.roles.overrides
        await loop.begin(result)
        response = await loop.complete(loop.contract.encode(valid))
        assert response["receipts"][0]["status"] == "confirmed"
        assert response["receipts"][1]["status"] == "unexecuted"
        assert response["receipts"][1]["reason"] == "PREREQUISITES_CHANGED"
        assert await loop.execute() == response
        saved = journal(m.memory, response["decisionId"])
        assert saved is not None
        assert saved["operationIds"] == [r["operationId"] for r in response["receipts"]]
        assert continuation(response)["toolOutput"]["retain"]


@run_async
async def test_read_batch_bounds_and_no_actions_from_incomplete_output(tmp_path):
    async with managed(tmp_path) as m:
        contract = DecisionContract("gameplay", await m.gateway.tools(m.main))
        loop = DecisionLoop(m.gateway, m.main, contract, {})
        operation = {"tool": "player_turn", "arguments": {}}
        for count in (0, 13):
            with pytest.raises((SchemaError, ValueError)):
                contract.encode({"kind": "inspect", "operations": [operation] * count})
        await loop.begin("position")
        with pytest.raises(ValueError, match="INCOMPLETE_GENERATION"):
            await loop.complete(contract.encode({"kind": "finish", "arguments": {}}), "interrupted")
        assert not m.roles.ready["gameplay"]
        assert loop.current is not None
        saved = journal(m.memory, loop.current["id"])
        assert saved is not None and saved["status"] == "generation_failed"


@run_async
async def test_military_completion_has_no_model_authored_basis(tmp_path):
    async with managed(tmp_path) as m:
        job = await m.gateway.open("military", "military", "g1", m.host, {}, 0)
        m.roles.register(job)
        contract = DecisionContract("military", await m.gateway.tools(job))
        assert '"basis"' not in json.dumps(contract.schema)
        loop = DecisionLoop(m.gateway, job, contract, {})
        await loop.begin("position")
        result = await loop.complete(
            contract.encode({"kind": "finish", "arguments": {"decisions": [{"targets": ["unit1"], "reason": "Hold"}]}})
        )
        assert result["error"] == "MILITARY_REVIEW_NOT_DELIVERED"
        with pytest.raises((ValueError, SchemaError)):
            decision_text(contract.schema, {"kind": "finish", "arguments": {"basis": "invented"}})


def test_inspection_subject_shapes_and_bounds():
    contract = DecisionContract("gameplay", [{"name": "player_inspect"}, {"name": "construction_details"}])
    for arguments in (
        {"subject": "UNIT", "target": "unit1"},
        {"subject": "UNIT", "destination": {"x": 1, "y": 0}},
        {"subject": "REPLAY"},
    ):
        decision = {"kind": "inspect", "operations": [{"tool": "player_inspect", "arguments": arguments}]}
        assert contract.parse(contract.encode(decision))["operations"][0]["arguments"] == arguments
    for arguments in (
        {"subject": "ROAD"},
        {"subject": "WONDERS", "limit": 101},
        {"subject": "WONDERS", "target": "unit1"},
    ):
        with pytest.raises((ValueError, SchemaError)):
            contract.encode({"kind": "inspect", "operations": [{"tool": "player_inspect", "arguments": arguments}]})
    with pytest.raises((ValueError, SchemaError)):
        contract.encode(
            {
                "kind": "inspect",
                "operations": [{"tool": "construction_details", "arguments": {"target": "city1", "horizonTurns": 10}}],
            }
        )
