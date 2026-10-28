"""Delivered notebook and construction baselines preserve gateway evidence rules."""

import asyncio
import copy
import json

import pytest

from civ_agent.construction_estimates import ESTIMATE_BASIS, INVESTMENT_BASIS
from civ_agent.construction_explanations import expand_explanations
from civ_agent.memory_delivery import apply_memory_delivery
from civ_agent.observations import DetailQuery, detail_scope
from civ_agent.payload_updates import apply_detail_response
from civ_agent.startup_context import prepare_startup, startup_prompt
from tests.test_construction_estimates import estimate
from tests.test_economy_optimization import managed

ASSESSMENT = "Develop the cities while retaining production flexibility. " * 25


async def replace_strategy(m, assessment, call_id):
    result = await m.gateway.call(
        m.main,
        "memory_update",
        {"changes": [{"op": "replace", "id": m.strategy, "content": {"type": "strategy", "assessment": assessment}}]},
        call_id,
    )
    assert result["changes"][0]["accepted"], result
    return result


def test_startup_metadata_seeds_both_caches_and_first_redundant_read_is_unchanged(tmp_path):
    async def run():
        async with managed(tmp_path) as m:
            await replace_strategy(m, ASSESSMENT, "long-strategy")
            prepared = await prepare_startup(m.gateway, m.economy)
            context = json.loads(prepared["memoryContext"].split("\n", 1)[1])
            metadata = context["memoryDelivery"]
            assert metadata["scopeId"] == m.economy.coordination_scope
            assert all(item["kind"] == "full" for item in metadata["items"])
            _, expected = apply_memory_delivery({}, context, scope=m.economy.coordination_scope)
            assert m.strategy in expected
            assert m.economy.memory_read_pending == expected
            assert m.economy.memory_pending == {key: row["value"] for key, row in expected.items()}
            assert not m.economy.memory_read_seen and not m.economy.memory_seen
            m.gateway.commit_startup_coordination(m.economy)
            assert m.economy.memory_read_seen == expected
            assert m.economy.memory_seen == {key: row["value"] for key, row in expected.items()}
            assert not m.economy.memory_read_pending and not m.economy.memory_pending
            result = await m.gateway.call(m.economy, "memory_read", {"ids": [m.strategy]}, "repeat-startup")
            assert result["records"] == []
            assert result["memoryDelivery"]["items"][0]["kind"] == "unchanged"
            expanded, _ = apply_memory_delivery(expected, result)
            assert expanded["records"] == [expected[m.strategy]["value"]]

    asyncio.run(run())


def test_current_search_compacts_but_historical_search_never_seeds_current_caches(tmp_path):
    async def run():
        async with managed(tmp_path) as m:
            await replace_strategy(m, ASSESSMENT, "long-strategy")
            m.gateway.reset_coordination(m.main)
            args = {"id": m.strategy, "type": "strategy"}
            first = await m.gateway.call(m.main, "memory_search", args, "search")
            assert first["memoryDelivery"]["items"][0]["kind"] == "full"
            baseline = copy.deepcopy(m.main.memory_read_seen)
            second = await m.gateway.call(m.main, "memory_search", args, "repeat-search")
            assert second["records"] == []
            assert second["memoryDelivery"]["items"][0]["kind"] == "unchanged"
            assert apply_memory_delivery(baseline, second)[0]["records"] == first["records"]
            before = copy.deepcopy((m.main.memory_read_seen, m.main.memory_seen, m.main.memory_versions))
            history = await m.gateway.call(m.main, "memory_search", {**args, "history": True}, "history")
            assert history["records"] and all(row["historical"] for row in history["records"])
            assert "memoryDelivery" not in history
            assert (m.main.memory_read_seen, m.main.memory_seen, m.main.memory_versions) == before
            m.gateway.reset_coordination(m.main)
            assert await m.gateway.call(m.main, "memory_search", {**args, "history": True}, "history") == history
            assert not m.main.memory_read_seen and not m.main.memory_seen
            await m.gateway.call(m.main, "memory_search", {**args, "history": True}, "new-history")
            assert not m.main.memory_read_seen and not m.main.memory_seen
            current = await m.gateway.call(m.main, "memory_search", args, "current-after-history")
            assert current["memoryDelivery"]["items"][0]["kind"] == "full"

    asyncio.run(run())


def test_own_writes_invalidate_reads_and_old_full_or_unchanged_replays_cannot_regress(tmp_path):
    async def run():
        async with managed(tmp_path) as m:
            await replace_strategy(m, ASSESSMENT, "long-strategy")
            args = {"ids": [m.strategy]}
            first = await m.gateway.call(m.main, "memory_read", args, "read")
            repeated = await m.gateway.call(m.main, "memory_read", args, "repeat")
            assert repeated["memoryDelivery"]["items"][0]["kind"] == "unchanged"
            edited = await replace_strategy(m, ASSESSMENT + " Preserve the new choice.", "edit")
            version = edited["changes"][0]["version"]
            newest = copy.deepcopy(m.main.memory_seen[m.strategy])
            assert m.strategy not in m.main.memory_read_seen
            for call_id, reply in (("read", first), ("repeat", repeated)):
                assert await m.gateway.call(m.main, "memory_read", args, call_id) == reply
                assert m.strategy not in m.main.memory_read_seen
                assert m.main.memory_seen[m.strategy] == newest
                assert m.main.memory_versions[m.strategy] == version
            fresh = await m.gateway.call(m.main, "memory_read", args, "after-edit")
            assert fresh["memoryDelivery"]["items"][0]["kind"] == "full"
            assert fresh["records"] == [newest]
            cached = copy.deepcopy(m.main.memory_read_seen)
            assert await m.gateway.call(m.main, "memory_read", args, "read") == first
            assert m.main.memory_read_seen == cached
            assert m.main.memory_seen[m.strategy] == newest

    asyncio.run(run())


def test_sibling_updates_delivered_by_overview_invalidate_read_baseline(tmp_path):
    async def run():
        async with managed(tmp_path) as m:
            await m.gateway.call(m.economy, "player_turn", {}, "economy-start")
            key = m.plans[1]
            await m.gateway.call(m.economy, "memory_read", {"ids": [key]}, "inspect-plan")
            reason = "Retain flexible production while the city grows. " * 6

            async def amend(text, call_id):
                result = await m.gateway.call(
                    m.economy,
                    "memory_update",
                    {"changes": [{"op": "amend", "id": key, "fields": {"reasons": {"construction": text}}}]},
                    call_id,
                )
                assert result["changes"][0]["accepted"], result
                return result

            await amend(reason, "long-plan")
            old = await m.gateway.call(m.main, "memory_read", {"ids": [key]}, "old-plan")
            assert key in m.main.memory_read_seen
            changed = await amend(reason + " Prepare the next build.", "new-plan")
            overview = await m.gateway.call(m.main, "player_turn", {}, "updated-overview")
            update = next(row for row in overview["memoryUpdates"] if row["record"]["id"] == key)
            assert update["version"] == changed["changes"][0]["version"]
            assert key not in m.main.memory_read_seen
            assert m.main.memory_seen[key] == update["record"]
            assert await m.gateway.call(m.main, "memory_read", {"ids": [key]}, "old-plan") == old
            assert key not in m.main.memory_read_seen and m.main.memory_seen[key] == update["record"]
            full = await m.gateway.call(m.main, "memory_read", {"ids": [key]}, "current-plan")
            assert full["memoryDelivery"]["items"][0]["kind"] == "full"
            assert full["records"] == [update["record"]]

    asyncio.run(run())


def test_scope_reset_and_full_reads_recover_without_accepting_old_scope_replays(tmp_path):
    async def run():
        async with managed(tmp_path) as m:
            await replace_strategy(m, ASSESSMENT, "long-strategy")
            args = {"ids": [m.strategy]}
            first = await m.gateway.call(m.main, "memory_read", args, "first")
            await m.gateway.call(m.main, "memory_read", args, "repeat")
            full = await m.gateway.call(m.main, "memory_read", {**args, "full": True}, "full")
            assert full["memoryDelivery"]["items"][0]["kind"] == "full"
            assert full["records"] == first["records"]
            old_scope = m.main.coordination_scope
            m.gateway.reset_coordination(m.main)
            assert m.main.coordination_scope != old_scope
            assert await m.gateway.call(m.main, "memory_read", args, "first") == first
            assert not m.main.memory_read_seen and not m.main.memory_seen
            recovered = await m.gateway.call(m.main, "memory_read", args, "new-scope")
            assert recovered["memoryDelivery"]["scopeId"] == m.main.coordination_scope
            assert recovered["memoryDelivery"]["items"][0]["kind"] == "full"
            assert recovered["records"] == first["records"]
            m.main.memory_read_seen.clear()
            recovery = await m.gateway.call(m.main, "memory_read", {**args, "full": True}, "lost-baseline")
            assert recovery["records"] == first["records"]
            assert m.strategy in m.main.memory_read_seen

    asyncio.run(run())


@pytest.mark.parametrize("tool", ["memory_read", "memory_search"])
def test_observation_hashes_match_wire_projection_and_repeat_reads(tool, tmp_path):
    async def run():
        async with managed(tmp_path) as m:
            body = {
                "epoch": "g1",
                "revision": "raw-revision",
                "requestId": "host-receipt",
                "facts": "Observed player-visible fact. " * 50,
                "nested": {"epoch": "g1", "revision": "raw-nested", "requestId": "nested-receipt"},
            }
            await m.gateway.io.call(m.memory.observe, "delivery-check", body, m.main.id)
            key = "observation:delivery-check"
            stored = await m.gateway.io.call(m.memory.delivered_records, m.main.id, [key])
            assert stored[key]["body"] == body
            args = {"ids": [key]} if tool == "memory_read" else {"id": key, "type": "observation"}
            first = await m.gateway.call(m.main, tool, args, "first-projection")
            expanded, cache = apply_memory_delivery({}, first, scope=m.main.coordination_scope)
            assert expanded["records"][0]["body"] == {"facts": body["facts"], "nested": {}}
            assert m.main.memory_read_seen[key] == cache[key]
            repeated = await m.gateway.call(m.main, tool, args, "repeat-projection")
            assert repeated["memoryDelivery"]["items"][0]["kind"] == "unchanged"
            assert apply_memory_delivery(cache, repeated)[0]["records"] == expanded["records"]

    asyncio.run(run())


def test_construction_startup_and_replies_share_exact_guidance_with_full_recovery(tmp_path):
    async def run():
        async with managed(tmp_path) as m:
            city = m.host.view["cities"][1]
            city.update(
                constructionEstimates={"Granary": estimate(), "Market": estimate("6")},
                constructionChoice={"availableSinceChoice": ["Market"]},
            )
            prepared = await prepare_startup(m.gateway, m.economy)
            prompt = startup_prompt(prepared, "economy", "")
            assert prompt.user.count(ESTIMATE_BASIS) == 1
            assert "Construction estimate explanations:" in prompt.user
            assert "explanationRefs" in prompt.user
            m.gateway.commit_startup_coordination(m.economy)
            args = {"target": "city2", "names": ["Granary", "Market"], "horizonTurns": 20}
            result = await m.gateway.call(m.economy, "construction_details", args, "compare")
            assert result["constructionExplanations"] == {"investment": INVESTMENT_BASIS}
            expanded = expand_explanations(result)
            assert expanded["data"]["estimateBasis"] == ESTIMATE_BASIS
            assert all(
                row["investmentComparison"]["basis"] == INVESTMENT_BASIS for row in expanded["data"]["constructions"]
            )
            key = detail_scope(DetailQuery.model_validate(expanded["query"]))
            assert m.economy.detail_seen[key]["value"] == expanded["data"]
            full = await m.gateway.call(m.economy, "construction_details", {**args, "full": True}, "recover")
            assert "constructionExplanations" not in full
            assert full["detailUpdate"]["kind"] == "full" and full["data"] == expanded["data"]
            assert await m.gateway.call(m.economy, "construction_details", args, "compare") == result
            assert m.economy.detail_seen[key]["value"] == expanded["data"]

    asyncio.run(run())


def test_crossquery_detail_reuse_requires_fresh_inspection_and_keeps_native_preview_guard(tmp_path):
    async def run():
        async with managed(tmp_path) as m:
            m.host.view["map"]["units"][0].update(name="Worker", military=False)
            m.host.view["cities"][0]["development"].update(
                tiles=[{"position": {"x": 1, "y": 0}, "description": "Known tile detail " * 300}],
                yields=[{"stat": "Gold", "amount": "5"}],
            )
            await m.gateway.call(m.main, "player_turn", {}, "fresh-overview")
            broad = await m.gateway.call(
                m.main, "city_details", {"target": "city1", "sections": ["citizens", "economy"]}, "broad"
            )
            cache = copy.deepcopy(m.main.detail_seen)
            narrow_query = DetailQuery(tool="city_details", arguments={"target": "city1", "sections": ["citizens"]})
            assert narrow_query not in m.main.details_read
            m.main.details_read.clear()
            narrow = await m.gateway.call(m.main, "city_details", narrow_query.arguments, "narrow")
            assert narrow["detailUpdate"]["baseQuery"] == broad["query"]
            assert narrow_query in m.main.details_read
            reconstructed = apply_detail_response(cache, narrow)
            assert (
                reconstructed["value"]["entity"]["development"]["tiles"]
                == broad["data"]["entity"]["development"]["tiles"]
            )
            assert "yields" not in reconstructed["value"]["entity"]["development"]
            blocked = await m.gateway.call(
                m.main,
                "player_command",
                {"kind": "SET_UNIT_DESTINATION", "target": "unit1", "destination": {"x": 1, "y": 0}},
                "uninspected-movement",
            )
            assert blocked["error"] == "DETAIL_REQUIRED" and not blocked["applied"]
            assert blocked["recovery"]["reads"][0]["tool"] == "movement_preview"
            assert not m.host.executed

    asyncio.run(run())
