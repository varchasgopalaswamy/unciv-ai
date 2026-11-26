"""Overview recovery, read bundles and exact shared native values preserve evidence."""

import asyncio
import copy
import json

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from sqlalchemy import func, select

from civ_agent.delivery_options import content_arguments, full_delivery
from civ_agent.entity_sections import CityDetails
from civ_agent.inspection_bundle import InspectionBundle
from civ_agent.memory_schema import calls
from civ_agent.observations import DetailQuery
from civ_agent.payload_updates import apply_detail_response, encoded, normalized_detail_query
from civ_agent.response_compaction import compact_response, expand_response
from civ_agent.review_grounding import ReviewEvidence
from civ_agent.schema_briefs import schema_brief
from civ_agent.shared_values import compact_shared_values, expand_shared_values
from civ_agent.startup_context import core_definitions, prepare_startup, startup_prompt
from civ_agent.state_updates import apply_overview_update, apply_player_turn, overview_update, stamp
from civ_agent.strategic_review import REVIEW_READ_TOOLS
from tests.domain_fixtures import managed


@pytest.mark.parametrize("arguments", [{"delivery": "full"}, {"full": True}, {"delivery": "full", "full": True}])
def test_recovery_preserves_legacy_calls_without_changing_information_identity(arguments):
    model = CityDetails.model_validate({"target": "city1", **arguments})
    assert full_delivery(model.model_dump(exclude_unset=True))
    assert content_arguments(model.model_dump(exclude_unset=True)) == {"target": "city1"}
    assert normalized_detail_query({"tool": "city_details", "arguments": model.model_dump(exclude_unset=True)}) == {
        "tool": "city_details",
        "arguments": {"target": "city1"},
    }
    schema = CityDetails.model_json_schema()
    assert "full" not in schema["properties"] and schema["properties"]["delivery"]["default"] == "auto"
    Draft202012Validator(schema).validate({"target": "city1", "sections": ["all"], "delivery": "auto"})


def test_conflicting_recovery_controls_and_mutating_or_recursive_bundles_are_rejected():
    with pytest.raises(ValidationError):
        CityDetails.model_validate({"target": "city1", "delivery": "auto", "full": True})
    for tool in ("player_command", "chat_send", "memory_update", "inspect_bundle"):
        with pytest.raises(ValidationError):
            InspectionBundle.model_validate({"queries": [{"tool": tool, "arguments": {}}]})


def test_prepared_overview_reads_refresh_native_state_without_resending_unchanged_data(tmp_path):
    async def run():
        async with managed(tmp_path) as m:
            await prepare_startup(m.gateway, m.main)
            m.gateway.commit_startup_coordination(m.main)
            baseline = copy.deepcopy(m.main.delivered_overview)
            native_reads = []
            native = m.host.tool

            async def tracked(name, arguments):
                if name == "player_turn":
                    native_reads.append(arguments)
                return await native(name, arguments)

            m.host.tool = tracked
            same = await m.gateway.call(m.main, "player_turn", {}, "same")
            assert same["stateUpdate"]["kind"] == "unchanged" and "cities" not in same
            assert apply_overview_update(baseline, same["stateUpdate"]) == m.main.delivered_overview
            m.host.view["cities"][0]["name"] = "Renamed Rome"
            m.host.revision = "changed"
            changed = await m.gateway.call(m.main, "player_turn", {}, "changed")
            current = apply_overview_update(baseline, expand_response(changed)["stateUpdate"])
            assert current == m.main.delivered_overview
            assert current["cities"][0]["name"] == "Renamed Rome"
            recovered = await m.gateway.call(m.main, "player_turn", {"delivery": "full"}, "recover")
            assert recovered["cities"] == current["cities"] and "stateUpdate" not in recovered
            assert len(native_reads) == 3
            assert await m.gateway.call(m.main, "player_turn", {}, "same") == same
            assert len(native_reads) == 3  # A durable duplicate never observes again.
            assert m.main.delivered_overview == apply_player_turn(None, recovered)
            m.gateway.reset_coordination(m.main)
            reset = await m.gateway.call(m.main, "player_turn", {}, "new-conversation")
            assert "cities" in reset and "stateUpdate" not in reset

    asyncio.run(run())


def test_view_scope_changes_and_missing_baselines_always_recover_full_data(tmp_path):
    async def run():
        async with managed(tmp_path) as m:
            await prepare_startup(m.gateway, m.main)
            m.gateway.commit_startup_coordination(m.main)
            focused = await m.gateway.call(m.main, "player_turn", {"cities": ["city1"]}, "focused")
            assert [city["handle"] for city in focused["cities"]] == ["city1"]
            expanded = await m.gateway.call(m.main, "player_turn", {}, "expanded")
            assert len(expanded["cities"]) == 2 and "stateUpdate" not in expanded
            m.main.delivered_overview = None
            missing = await m.gateway.call(m.main, "player_turn", {}, "missing")
            restored = apply_player_turn(None, missing)
            assert restored == m.main.delivered_overview
            assert "cities" in restored

    asyncio.run(run())


def test_unchanged_updates_preserve_invalidation_types_and_missing_base_errors():
    before = stamp({"cities": [], "facts": {"value": True}}, "job", "epoch", "r1")
    after = stamp({"cities": [], "facts": {"value": True}}, "job", "epoch", "r2")
    invalidated = [DetailQuery(tool="city_details", arguments={"target": "city1", "sections": ["purchases"]})]
    update = overview_update(before, after, invalidated, allow_unchanged=True)
    assert update["kind"] == "unchanged" and update["invalidatedDetails"]
    assert apply_overview_update(before, update) == after
    assert apply_overview_update(after, update) == after
    with pytest.raises(ValueError, match="FULL_OVERVIEW_REQUIRED"):
        apply_overview_update(None, update)
    integer = stamp({"cities": [], "facts": {"value": 1}}, "job", "epoch", "r3")
    assert overview_update(after, integer, [], allow_unchanged=True)["kind"] == "delta"


def test_inspection_bundle_delivers_only_requested_evidence_and_replays_durably(tmp_path):
    async def run():
        async with managed(tmp_path) as m:
            arguments = {
                "queries": [
                    {"tool": "city_details", "arguments": {"target": "city1", "sections": ["citizens"]}},
                    {"tool": "construction_details", "arguments": {"target": "city1", "names": ["Granary"]}},
                ]
            }
            result = await m.gateway.call(m.main, "inspect_bundle", arguments, "bundle")
            assert result["remaining"] == 0 and "error" not in result
            expanded = expand_response(result)
            assert [item["tool"] for item in expanded["results"]] == ["city_details", "construction_details"]
            cache = {}
            for item in expanded["results"]:
                state = apply_detail_response(cache, item["result"])
                assert state["value"]
            assert any(
                q.tool == "city_details" and q.arguments["sections"] == ["citizens"] for q in m.main.details_read
            )
            assert not any(
                q.tool == "city_details" and "purchases" in q.arguments.get("sections", []) for q in m.main.details_read
            )
            before = copy.deepcopy((m.main.detail_seen, m.main.details_read))
            with m.memory.transaction() as db:
                count = db.execute(select(func.count()).select_from(calls)).scalar_one()
            assert await m.gateway.call(m.main, "inspect_bundle", arguments, "bundle") == result
            with m.memory.transaction() as db:
                assert db.execute(select(func.count()).select_from(calls)).scalar_one() == count
            assert (m.main.detail_seen, m.main.details_read) == before
            assert not m.host.executed

    asyncio.run(run())


def test_bundle_validates_every_query_before_reading_and_stops_on_unavailable_state(tmp_path):
    async def run():
        async with managed(tmp_path) as m:
            before = copy.deepcopy(m.main.details_read)
            result = await m.gateway.call(
                m.main,
                "inspect_bundle",
                {
                    "queries": [
                        {"tool": "city_details", "arguments": {"target": "city1"}},
                        {"tool": "construction_details", "arguments": {"target": "city1", "horizonTurns": 10}},
                    ]
                },
                "invalid",
            )
            assert result["queryIndex"] == 1 and result["error"]
            assert m.main.details_read == before
            m.gateway.forget_observation(m.main)
            stopped = await m.gateway.call(
                m.main,
                "inspect_bundle",
                {
                    "queries": [
                        {"tool": "city_details", "arguments": {"target": "city1"}},
                        {"tool": "city_details", "arguments": {"target": "city2"}},
                    ]
                },
                "stopped",
            )
            assert stopped["error"] == "INSPECTION_FAILED" and stopped["remaining"] == 1
            assert len(stopped["results"]) == 1 and not m.main.details_read

    asyncio.run(run())


def test_accepted_bundle_drains_after_checkpoint_closes_admission(tmp_path):
    async def run():
        async with managed(tmp_path) as m:
            original = m.gateway._admitted_call

            async def intercept(job, name, arguments, call_id, *, source):
                if source == "internal":
                    m.gateway.request_checkpoint()
                return await original(job, name, arguments, call_id, source=source)

            m.gateway._admitted_call = intercept
            try:
                result = await asyncio.wait_for(
                    m.gateway.call(
                        m.main,
                        "inspect_bundle",
                        {
                            "queries": [
                                {"tool": "city_details", "arguments": {"target": "city1"}},
                                {"tool": "city_details", "arguments": {"target": "city2"}},
                            ]
                        },
                        "checkpoint",
                    ),
                    3,
                )
                assert result["remaining"] == 0 and m.gateway.calls_in_flight == 0
            finally:
                m.gateway.release_checkpoint()

    asyncio.run(run())


def test_bundle_survives_caller_disconnect_and_replays_without_repeating_reads(tmp_path):
    async def run():
        async with managed(tmp_path) as m:
            entered, released = asyncio.Event(), asyncio.Event()
            original = m.gateway._admitted_call
            reads = []

            async def intercept(job, name, arguments, call_id, *, source):
                if source == "internal":
                    reads.append(name)
                    entered.set()
                    await released.wait()
                return await original(job, name, arguments, call_id, source=source)

            m.gateway._admitted_call = intercept
            arguments = {
                "queries": [
                    {"tool": "city_details", "arguments": {"target": "city1"}},
                    {"tool": "city_details", "arguments": {"target": "city2"}},
                ]
            }
            caller = asyncio.create_task(m.gateway.invoke(m.main, "inspect_bundle", arguments, "disconnect"))
            await asyncio.wait_for(entered.wait(), 3)
            accepted = list(m.main.pending_calls)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            released.set()
            result = (await asyncio.wait_for(asyncio.gather(*accepted), 3))[0]
            assert result["remaining"] == 0 and reads == ["city_details", "city_details"]
            assert await m.gateway.call(m.main, "inspect_bundle", arguments, "disconnect") == result
            assert reads == ["city_details", "city_details"]

    asyncio.run(run())


def test_review_bundles_use_frozen_evidence_without_notebook_reads_or_host_actions(tmp_path):
    async def run():
        async with managed(tmp_path) as m:
            inputs = {
                "observation": await m.host.tool("player_turn", {}),
                "reads": {"player_turn": {}},
                "civilization": "Rome",
            }
            catalogue = await m.gateway.tools(m.main)
            m.main.review_inputs = inputs
            m.main.review_evidence = ReviewEvidence(inputs)
            m.main.catalogue = [tool for tool in catalogue if tool["name"] in REVIEW_READ_TOOLS]
            with m.memory.transaction() as db:
                count = db.execute(select(func.count()).select_from(calls)).scalar_one()
            result = await m.gateway.call(
                m.main,
                "inspect_bundle",
                {
                    "queries": [
                        {"tool": "city_details", "arguments": {"target": "city1", "sections": ["production"]}},
                        {"tool": "city_details", "arguments": {"target": "city2", "sections": ["citizens"]}},
                    ]
                },
                "review-bundle",
            )
            assert result["remaining"] == 0
            assert all(item["result"].get("source") for item in result["results"])
            denied = await m.gateway.call(
                m.main,
                "inspect_bundle",
                {
                    "queries": [
                        {"tool": "memory_read", "arguments": {"ids": [m.strategy]}},
                    ]
                },
                "denied-review",
            )
            assert denied["error"] == "TOOL_NOT_PERMITTED"
            with m.memory.transaction() as db:
                assert db.execute(select(func.count()).select_from(calls)).scalar_one() == count
            assert not m.host.executed

    asyncio.run(run())


def test_shared_values_preserve_typed_native_values_and_ignore_authored_material():
    repeated = {"available": True, "cost": 1, "missing": None, "description": "Native warning. " * 40}
    original = {
        "data": {
            "entity": {
                "moves": [
                    {"context": copy.deepcopy(repeated), "warnings": ["Full consequences " * 20]},
                    {"context": copy.deepcopy(repeated), "warnings": ["Full consequences " * 20]},
                ]
            }
        },
        "records": [{"reasons": {"context": repeated}}],
        "messages": [{"context": repeated}],
    }
    saved = copy.deepcopy(original)
    compact = compact_shared_values(original)
    assert compact["sharedValues"] and len(encoded(compact)) < len(encoded(original))
    assert encoded(expand_shared_values(compact)) == encoded(original)
    assert compact["records"] == original["records"] and compact["messages"] == original["messages"]
    assert original == saved and compact_shared_values(compact) == compact
    assert expand_shared_values(json.loads(json.dumps(compact, sort_keys=True))) == original
    assert compact_shared_values(compact, full=True) == original
    malformed = copy.deepcopy(compact)
    malformed["sharedValues"]["unused"] = repeated
    with pytest.raises(ValueError, match="FULL_SHARED_VALUES_REQUIRED"):
        expand_shared_values(malformed)


def test_shared_state_fields_expand_before_overview_reconstruction():
    common = {"Food": "1", "Science": "2", "basis": "Complete native meaning " * 30}
    current = stamp(
        {"cities": [{"handle": "a", "yields": common}, {"handle": "b", "yields": common}]}, "job", "epoch", "r1"
    )
    wire = compact_response({"stateUpdate": overview_update(None, current, [])})
    assert "sharedValues" in wire["stateUpdate"]
    assert apply_overview_update(None, wire["stateUpdate"]) == current
    assert expand_response(wire)["stateUpdate"]["overview"] == current


def test_startup_defers_only_command_schema_and_preserves_complete_role_index(tmp_path):
    async def run():
        async with managed(tmp_path) as m:
            catalogue = await m.gateway.tools(m.economy)
            saved = copy.deepcopy(catalogue)
            definitions = core_definitions(catalogue, "economy")
            brief = next(t for t in definitions if t["name"] == "mcp__civ__player_command")
            complete = next(t for t in catalogue if t["name"] == "player_command")
            assert set(brief["commandIndex"]) == set(complete["inputSchema"]["discriminator"]["mapping"])
            assert "inputSchema" not in brief and brief["schemaRead"]["tool"] == "mcp__civ__command_help"
            help = await m.gateway.call(m.economy, "command_help", {"kinds": ["SET_CITY_FOCUS"]}, "help")
            Draft202012Validator(help["inputSchema"]).validate(
                {"kind": "SET_CITY_FOCUS", "target": "city2", "selection": "FoodFocus"}
            )
            denied = await m.gateway.call(m.economy, "command_help", {"kinds": ["DECLARE_WAR"]}, "forbidden-help")
            assert denied["error"] == "COMMAND_NOT_PERMITTED"
            assert len(encoded(brief)) < len(encoded(complete)) / 2 and catalogue == saved
            prepared = await prepare_startup(m.gateway, m.economy)
            prompt = startup_prompt(prepared, "economy", "")
            assert prompt.metadata()["layoutVersion"] == 13 and "commandIndex" not in prompt.instructions

    asyncio.run(run())


def test_schema_trimming_preserves_constraints_and_literal_title_fields():
    schema = {
        "title": "Root",
        "type": "object",
        "properties": {
            "title": {"title": "Title", "type": "string", "minLength": 1},
            "nested": {"title": "Nested", "type": "object", "default": {"title": "Literal fact"}},
        },
        "required": ["title"],
        "additionalProperties": False,
    }
    brief = schema_brief(schema)
    assert "title" not in brief and "title" in brief["properties"]
    assert brief["properties"]["nested"]["default"] == {"title": "Literal fact"}
    for example in ({"title": "Valid"}, {"title": ""}, {}, {"title": "Valid", "extra": 1}):
        assert Draft202012Validator(schema).is_valid(example) == Draft202012Validator(brief).is_valid(example)
