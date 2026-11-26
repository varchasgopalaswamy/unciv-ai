"""Exact delivery reconstruction across notebook, detail and overview scopes."""

import asyncio
import copy

import pytest

from civ_agent.construction_estimates import ESTIMATE_BASIS, INVESTMENT_BASIS
from civ_agent.construction_explanations import compact_explanations, expand_explanations
from civ_agent.memory_delivery import apply_memory_delivery, memory_response
from civ_agent.observations import DetailQuery, detail_scope
from civ_agent.payload_updates import apply_detail_response, detail_response, encoded, normalized_detail_query
from civ_agent.protocol import JsonObject
from civ_agent.state_updates import apply_overview_update, overview_update, stamp
from tests.domain_fixtures import managed


def note(id: str = "mem1", **changes) -> JsonObject:
    return {"id": id, "type": "note", "text": "Complete earlier explanation " * 100, "priority": 3, **changes}


def memory_first(records=None, *, scope="scope", sequence=1):
    records = records if records is not None else [note()]
    versions = {record["id"]: 1 for record in records}
    full = memory_response({"records": records}, versions, {}, scope, sequence)
    expanded, cache = apply_memory_delivery({}, full, scope=scope)
    assert expanded == {"records": records}
    return full, cache


def test_memory_reads_preserve_mixed_rows_order_pagination_and_empty_matches():
    _, cache = memory_first([note(), note("mem2")])
    original = {"records": [note("mem2", priority=4), note("mem3"), note()], "hasMore": True, "nextOffset": 23}
    saved = copy.deepcopy((original, cache))
    reply = memory_response(original, {"mem1": 1, "mem2": 2, "mem3": 1}, cache, "scope", 2)
    assert [item["kind"] for item in reply["memoryDelivery"]["items"]] == ["delta", "full", "unchanged"]
    assert [record["id"] for record in reply["records"]] == ["mem3"]
    expanded, updated = apply_memory_delivery(cache, reply, scope="scope")
    assert expanded == original
    assert (original, cache) == saved
    assert len(encoded(reply)) < len(encoded(original)) / 2
    empty = memory_response({"records": [], "hasMore": False, "nextOffset": 23}, {}, updated, "scope", 3)
    expanded, retained = apply_memory_delivery(updated, empty)
    assert expanded == {"records": [], "hasMore": False, "nextOffset": 23}
    assert retained == updated  # No match is not a deletion.


@pytest.mark.parametrize(
    "changed",
    [
        note(priority=True),
        note(**{"nested/~field": {"present": None}}),
        {"id": "mem1", "type": "note", "text": note()["text"], "rows": [{"id": "b"}, {"id": "a"}]},
        note(state="retired"),
    ],
)
def test_memory_changes_reconstruct_exact_types_removals_and_retirement(changed):
    first, cache = memory_first()
    reply = memory_response({"records": [changed]}, {"mem1": 2}, cache, "scope", 2)
    expanded, updated = apply_memory_delivery(cache, reply)
    assert encoded(expanded) == encoded({"records": [changed]})
    assert apply_memory_delivery(updated, reply)[1] == updated
    assert apply_memory_delivery(updated, first)[1] == updated
    with pytest.raises(ValueError, match="FULL_RECORD_REQUIRED"):
        apply_memory_delivery({}, reply)


def test_memory_full_recovery_history_and_write_version_fences():
    first, cache = memory_first()
    same = memory_response({"records": [note()]}, {"mem1": 1}, cache, "scope", 2)
    assert same["memoryDelivery"]["items"][0]["kind"] == "unchanged"
    full = memory_response({"records": [note()]}, {"mem1": 1}, cache, "scope", 3, full=True)
    assert apply_memory_delivery({}, full, minimum_versions={"mem1": 1})[0] == {"records": [note()]}
    # A newer write invalidated this read baseline. Old responses cannot revive it.
    assert apply_memory_delivery({}, first, minimum_versions={"mem1": 2})[1] == {}
    different = memory_response({"records": [note()]}, {"mem1": 1}, cache, "new-scope", 1)
    assert different["memoryDelivery"]["items"][0]["kind"] == "full"
    assert apply_memory_delivery(cache, different, scope="new-scope")[1]["mem1"]["scopeId"] == "new-scope"
    with pytest.raises(ValueError, match="scope mismatch"):
        apply_memory_delivery(cache, first, scope="new-scope")
    historical = {"records": [note(historical=True, evidence="h1")], "hasMore": False, "nextOffset": 1}
    unchanged = memory_response(historical, {}, cache, "scope", 4, historical=True)
    assert unchanged == historical and "memoryDelivery" not in unchanged
    assert apply_memory_delivery(cache, unchanged) == (historical, cache)


def test_memory_validation_is_atomic_and_checks_complete_record_identity():
    _, cache = memory_first([note(), note("mem2")])
    reply = memory_response(
        {"records": [note(priority=5), note("mem2", priority=5)]}, {"mem1": 2, "mem2": 2}, cache, "scope", 2
    )
    saved = copy.deepcopy(cache)
    reply["memoryDelivery"]["items"][1]["stateId"] = "invalid"
    with pytest.raises(ValueError, match="state mismatch"):
        apply_memory_delivery(cache, reply)
    assert cache == saved
    full, _ = memory_first()
    full["records"][0]["id"] = "different"
    with pytest.raises(ValueError, match="identities mismatch"):
        apply_memory_delivery({}, full)


def test_memory_same_content_new_version_and_small_record_size_fallback():
    _, cache = memory_first()
    original = {"records": [note()]}
    reply = memory_response(original, {"mem1": 2}, cache, "scope", 2)
    item = reply["memoryDelivery"]["items"][0]
    assert item["kind"] == "unchanged" and item["stateId"] != cache["mem1"]["stateId"]
    expanded, updated = apply_memory_delivery(cache, reply)
    assert expanded == original and updated["mem1"]["version"] == 2
    wrong = copy.deepcopy(reply)
    wrong["memoryDelivery"]["items"][0]["baseStateId"] = "different"
    with pytest.raises(ValueError, match="FULL_RECORD_REQUIRED"):
        apply_memory_delivery(cache, wrong)
    tiny = {"id": "mem1", "type": "note"}
    _, small_cache = memory_first([tiny])
    small = memory_response({"records": [tiny]}, {"mem1": 1}, small_cache, "scope", 2)
    # The baseline metadata itself would cost more than repeating this record.
    assert small["memoryDelivery"]["items"][0]["kind"] == "full"
    assert apply_memory_delivery(cache, reply, minimum_versions={"mem1": 3})[1] == {}


def test_gateway_hashes_the_delivered_observation_record_after_public_projection(tmp_path):
    async def run():
        async with managed(tmp_path) as m:
            args = {"ids": ["observation:turn"], "full": True}
            full = await m.gateway.call(m.main, "memory_read", args, "observation-full")
            expanded, cache = apply_memory_delivery({}, full, scope=m.main.coordination_scope)
            assert expanded["records"] and "observation:turn" in m.main.memory_read_seen
            assert "epoch" not in expanded["records"][0]["body"]
            repeat = await m.gateway.call(m.main, "memory_read", {"ids": args["ids"]}, "observation-repeat")
            assert repeat["memoryDelivery"]["items"][0]["kind"] == "unchanged"
            assert apply_memory_delivery(cache, repeat)[0]["records"] == expanded["records"]

    asyncio.run(run())


def query(tool="city_details", target="city1", **arguments):
    return normalized_detail_query({"tool": tool, "arguments": {"target": target, **arguments}})


def detail_first(q=None, data=None, *, scope="scope", sequence=1):
    q = q or query(sections=["economy", "citizens"])
    data = data or {"entity": {"handle": "city1", "development": {"tiles": ["facts " * 500]}, "yields": {"gold": 2}}}
    key = detail_scope(DetailQuery.model_validate(q))
    full = detail_response(None, {"query": q, "data": data}, scope, sequence, key)
    cache = {key: apply_detail_response({}, full)}
    return full, cache


def test_overlapping_details_reconstruct_destination_without_changing_source():
    first, cache = detail_first()
    source = copy.deepcopy(cache)
    destination = query(sections=["citizens"])
    key = detail_scope(DetailQuery.model_validate(destination))
    data = copy.deepcopy(first["data"])
    data["entity"].pop("yields")
    result = {"query": destination, "data": data, "evidence": "fresh-evidence", "available": True}
    compact = detail_response(None, result, "scope", 2, key, baselines=cache)
    assert compact["detailUpdate"]["baseQuery"] == first["query"]
    state = apply_detail_response(cache, compact)
    assert state["value"] == data and state["query"] == destination
    assert compact["evidence"] == "fresh-evidence" and cache == source
    cache[key] = state
    assert apply_detail_response(cache, compact) == state
    assert len(encoded(compact)) < len(encoded(result)) / 3
    with pytest.raises(ValueError, match="FULL_PAYLOAD_REQUIRED"):
        apply_detail_response({}, compact)
    full = detail_response(state, result, "scope", 3, key, baselines=cache, full=True)
    assert full["data"] == data and "baseQuery" not in full["detailUpdate"]


@pytest.mark.parametrize("change", ["target", "tool", "horizon", "scope"])
def test_detail_cross_query_reuse_stays_in_its_supported_semantic_scope(change):
    q = query("construction_details", names=["Library", "Temple"], horizonTurns=20)
    data = {"constructions": [{"name": "Library", "facts": "long facts " * 500}]}
    _, cache = detail_first(q, data)
    other = query("construction_details", names=["Library"], horizonTurns=20)
    scope = "scope"
    if change == "target":
        other["arguments"]["target"] = "city2"
    elif change == "tool":
        other = query("city_details", sections=["production"])
    elif change == "horizon":
        other["arguments"]["horizonTurns"] = 30
    else:
        scope = "other"
    key = detail_scope(DetailQuery.model_validate(other))
    reply = detail_response(None, {"query": other, "data": data}, scope, 2, key, baselines=cache)
    assert reply["detailUpdate"]["kind"] == "full"


def test_advanced_source_cannot_be_assigned_to_an_older_destination_query():
    first, cache = detail_first()
    source_key = next(iter(cache))
    destination = query(sections=["citizens"])
    key = detail_scope(DetailQuery.model_validate(destination))
    reply = detail_response(None, {"query": destination, "data": first["data"]}, "scope", 2, key, baselines=cache)
    original_cache = copy.deepcopy(cache)
    advanced = detail_response(
        cache[source_key], {**first, "data": {**first["data"], "new": True}}, "scope", 3, source_key
    )
    cache[source_key] = apply_detail_response(cache, advanced)
    with pytest.raises(ValueError, match="FULL_PAYLOAD_REQUIRED"):
        apply_detail_response(cache, reply)
    # A previously acknowledged destination makes this a harmless duplicate,
    # regardless of the source's later state.
    cache[key] = apply_detail_response(original_cache, reply)
    assert apply_detail_response(cache, reply) == cache[key]


def test_detail_query_defaults_and_section_order_preserve_identity_and_full_fallback():
    q = query(sections=["economy", "citizens", "economy"])
    assert q == query(sections=["citizens", "economy"])
    first, cache = detail_first(q, {"entity": {"handle": "city1"}})
    key = next(iter(cache))
    same = detail_response(cache[key], first, "scope", 2, key, baselines=cache)
    assert same["detailUpdate"]["kind"] == "full"
    assert apply_detail_response(cache, same)["value"] == first["data"]
    # Missing horizon and explicit null have the same effective semantics.
    with_horizon = query("construction_details", names=["Library", "Temple"], horizonTurns=None)
    data = {"constructions": [{"name": "Library", "facts": "long facts " * 500}]}
    _, cache = detail_first(with_horizon, data)
    without_horizon = query("construction_details", names=["Library"])
    key = detail_scope(DetailQuery.model_validate(without_horizon))
    reply = detail_response(None, {"query": without_horizon, "data": data}, "scope", 2, key, baselines=cache)
    assert reply["detailUpdate"]["kind"] == "unchanged"
    assert apply_detail_response(cache, reply)["value"] == data


def test_nested_overview_patches_are_opt_in_and_preserve_full_typed_state():
    before = stamp(
        {
            "units": [{"handle": "unit1", "health": 100, "facts": "known facts " * 400}, {"handle": "unit2"}],
            "settlement": {"candidates": ["unchanged " * 400], "newCity/~happiness": None, "allowed": True},
        },
        "j",
        "e",
        "r1",
    )
    after = copy.deepcopy(before)
    after["units"][0]["health"] = 80
    after["units"].reverse()
    after["settlement"]["newCity/~happiness"] = -2
    after["settlement"]["allowed"] = 1
    after = stamp(after, "j", "e", "r2")
    ordinary = overview_update(before, after, [])
    compact = overview_update(before, after, [], compact_nested=True)
    assert "patch" not in ordinary
    assert compact["patch"]
    assert encoded(apply_overview_update(before, compact)) == encoded(after)
    assert len(encoded(compact)) < len(encoded(ordinary))
    assert apply_overview_update(after, compact) == after
    assert overview_update(None, after, [], compact_nested=True) == overview_update(None, after, [])
    with pytest.raises(ValueError, match="BASE_STATE_MISMATCH"):
        apply_overview_update(stamp({}, "j", "e", "wrong"), compact)


def construction_reply():
    return {
        "query": query("construction_details", names=["Library", "Temple"]),
        "data": {
            "estimateBasis": ESTIMATE_BASIS,
            "constructions": [
                {"name": name, "investmentComparison": {"basis": INVESTMENT_BASIS, "available": True}}
                for name in ("Library", "Temple")
            ],
        },
    }


def test_construction_explanations_share_exact_text_and_keep_authored_content():
    original = construction_reply()
    original["notes"] = {"basis": INVESTMENT_BASIS, "explanationRefs": {"authored": "Keep literal"}}
    saved = copy.deepcopy(original)
    compact = compact_explanations(original)
    assert compact["constructionExplanations"] == {"investment": INVESTMENT_BASIS}
    assert compact["notes"] == original["notes"]
    assert expand_explanations(compact) == original == saved
    assert compact_explanations(compact) == compact
    assert compact_explanations(compact, full=True) == original
    assert len(encoded(compact)) < len(encoded(original))


def test_startup_explanations_share_across_reads_and_preloads_with_local_references():
    original = {
        "reads": {"player_turn": {"cities": [], "constructionEstimateBasis": ESTIMATE_BASIS}},
        "decisionDetails": [construction_reply()],
        "memoryContext": ESTIMATE_BASIS,
    }
    compact = compact_explanations(original, startup=True)
    assert set(compact["constructionExplanations"]) == {"estimate", "investment"}
    assert compact["memoryContext"] == original["memoryContext"]
    assert expand_explanations(compact, startup=True) == original
    assert compact_explanations({"cities": [], "constructionEstimateBasis": ESTIMATE_BASIS}) == {
        "cities": [],
        "constructionEstimateBasis": ESTIMATE_BASIS,
    }


def test_explanation_recovery_rejects_missing_registry_and_conflicting_fields():
    compact = compact_explanations(construction_reply())
    missing = copy.deepcopy(compact)
    missing.pop("constructionExplanations")
    with pytest.raises(ValueError, match="FULL_EXPLANATIONS_REQUIRED"):
        expand_explanations(missing)
    conflict = copy.deepcopy(compact)
    conflict["data"]["constructions"][0]["investmentComparison"]["basis"] = "conflict"
    with pytest.raises(ValueError, match="FULL_EXPLANATIONS_REQUIRED"):
        expand_explanations(conflict)
    untouched = {
        "stateUpdate": {"kind": "delta", "patch": [{"op": "replace", "path": "/basis", "value": INVESTMENT_BASIS}]}
    }
    assert compact_explanations(untouched) == untouched
