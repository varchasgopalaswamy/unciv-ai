"""Deterministic delivery, scoped inspections, and recovery after committed commands."""

import asyncio
import copy
import json
from contextlib import asynccontextmanager

import pytest

from civ_agent.civilization_state import CivilizationState, StateConflict
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.service_io import ServiceIO
from civ_agent.state_updates import apply_overview_update, overview_update, stamp
from tests.memory_helpers import stored_records
from tests.test_proposal_gateway import Host
from tests.test_structured_memory import create, strategy


def stamped(value, revision="r1", epoch="g1"):
    return stamp(value, "job", epoch, revision)


def test_delta_instructions_and_reducer_remove_fields_inside_replaced_rows():
    tools = ProposalGateway.tool_catalogue([{"name": "player_turn", "inputSchema": {}}], "gameplay")
    description = next(tool["description"] for tool in tools if tool["name"] == "player_turn")
    assert "Omitted values stay unchanged" not in description
    assert "fields absent from that replacement are removed" in description
    before = stamped({"units": [{"handle": "a", "canGiftUnit": True}, {"handle": "b", "health": 70}]})
    after = stamped({"units": [{"handle": "a"}, {"handle": "b", "health": 70}]}, "r2")
    update = overview_update(before, after, [])
    assert update["collections"]["units"]["upsert"] == [{"handle": "a"}]
    restored = apply_overview_update(before, update)
    assert "canGiftUnit" not in restored["units"][0]
    assert restored["units"][1] == before["units"][1]


def test_delta_round_trip_changes_only_changed_sections_and_rows():
    before = stamped(
        {
            "units": [{"handle": "unit1", "health": 100}, {"handle": "unit2", "health": 80}],
            "cities": [{"handle": "city1", "population": 1}],
            "alerts": [{"handle": "alert1", "content": {"paragraphs": ["Full native warning"]}}],
            "notifications": [],
            "economy": {"gold": 50, "income": 4},
            "pending": [],
            "rules": {"description": "Stable rules " * 100},
            "scenario": {"mission": "Earlier assessment"},
        }
    )
    after = stamped(
        {
            **{key: value for key, value in before.items() if key not in {"stateId", "scenario"}},
            "units": [{"handle": "unit3", "health": 100}, {"handle": "unit2", "health": 70}],
            "alerts": [],
            "economy": {"gold": 40},
            "pending": [{"kind": "PRODUCTION"}],
        },
        "r2",
    )
    saved = copy.deepcopy((before, after))
    delta = overview_update(before, after, [])
    assert delta == overview_update(before, after, [])
    assert set(delta["set"]) == {"economy", "pending"}
    assert delta["removed"] == ["scenario"]
    assert delta["collections"]["units"] == {
        "key": "handle",
        "upsert": after["units"],
        "removed": ["unit1"],
        "order": ["unit3", "unit2"],
    }
    assert delta["collections"]["alerts"]["removed"] == ["alert1"]
    assert apply_overview_update(before, delta) == after
    assert apply_overview_update(after, delta) == after  # Idempotent delivery.
    assert (before, after) == saved
    assert len(json.dumps(delta)) < len(json.dumps(after)) / 2
    later = stamped({"units": []}, "r3")
    with pytest.raises(ValueError, match="BASE_STATE_MISMATCH"):
        apply_overview_update(later, delta)
    with pytest.raises(ValueError, match="FULL_OVERVIEW_REQUIRED"):
        apply_overview_update(None, delta)
    assert apply_overview_update(None, overview_update(None, after, [])) == after
    assert stamped({"units": []}, "r1", "new-epoch")["stateId"] != stamped({"units": []})["stateId"]


def test_empty_reordered_and_unkeyed_sections_reconstruct_exactly():
    before = stamped({"cities": [{"handle": "a"}, {"handle": "b"}], "alerts": [{"legacy": "No stable ID"}]})
    after = stamped({"cities": [{"handle": "b"}, {"handle": "a"}], "alerts": []}, "r2")
    delta = overview_update(before, after, [])
    assert not delta["collections"]["cities"]["upsert"]
    assert delta["collections"]["cities"]["order"] == ["b", "a"]
    assert delta["set"]["alerts"] == []
    assert apply_overview_update(before, delta) == after
    unchanged = overview_update(after, after, [])
    assert not unchanged["set"] and not unchanged["collections"] and not unchanged["removed"]
    typed = overview_update(stamped({"value": True}), stamped({"value": 1}, "r2"), [])
    assert type(typed["set"]["value"]) is int


def test_wonder_updates_remove_completed_and_send_only_changed_or_unlocked_entries():
    oracle = {"name": "The Oracle", "benefits": "Free Social Policy", "cities": [{"city": "city1", "turns": 8}]}
    college = {"name": "National College", "benefits": "+50% Science", "cities": [{"city": "city1", "turns": 5}]}
    before = stamped({"wonderOpportunities": [college, oracle]})
    sistine = {"name": "Sistine Chapel", "benefits": "+25% Culture", "cities": [{"city": "city1", "turns": 20}]}
    after = stamped({"wonderOpportunities": [college, sistine]}, "r2")
    delta = overview_update(before, after, [])
    assert "wonderOpportunities" not in delta["set"]
    assert delta["collections"]["wonderOpportunities"] == {
        "key": "name",
        "upsert": [sistine],
        "removed": ["The Oracle"],
        "order": ["National College", "Sistine Chapel"],
    }
    assert apply_overview_update(before, delta) == after
    updated = stamped({"wonderOpportunities": [college, {**sistine, "cities": [{"city": "city1", "turns": 19}]}]}, "r3")
    changed = overview_update(after, updated, [])
    assert [w["name"] for w in changed["collections"]["wonderOpportunities"]["upsert"]] == ["Sistine Chapel"]
    assert apply_overview_update(after, changed) == updated


class UpdatingHost(Host):
    def __init__(self):
        super().__init__()
        self.reads = 0
        self.fail_refresh = False
        self.after_command = lambda: None
        self.view["map"]["units"].append({**copy.deepcopy(self.view["map"]["units"][0]), "handle": "unit2"})
        self.view["map"]["tiles"] = [{"position": {"x": 0, "y": 0}, "visible": True, "terrain": "Grassland"}]
        self.view["map"]["frontier"] = [{"x": 1, "y": 0}]

    async def tool(self, name, args):
        if name == "player_turn":
            self.reads += 1
            if self.fail_refresh:
                raise TimeoutError("Post-command refresh failed")
        result = await super().tool(name, args)
        if name == "player_command":
            if args["kind"] == "MOVE_UNIT":
                unit = next(u for u in self.view["map"]["units"] if u["handle"] == args["target"])
                unit["position"] = copy.deepcopy(args["destination"])
            if args["kind"] == "ACKNOWLEDGE_ALERT":
                self.view["alerts"] = [a for a in self.view.get("alerts", []) if a["handle"] != args["target"]]
            self.after_command()
            return {**result, "isYourTurn": args["kind"] != "END_TURN", "feedback": {"paragraphs": ["Native outcome"]}}
        return result


@asynccontextmanager
async def session(tmp_path):
    memory, io, host = CivilizationState(tmp_path / "private", "Rome"), ServiceIO(), UpdatingHost()
    gateway = ProposalGateway(memory, io, tmp_path / "workspace")
    job = await gateway.open("job", "gameplay", "g1", host, {}, 0)
    try:
        yield gateway, job, host, memory
    finally:
        await gateway.close(job, True)
        await io.close()


MOVE = {"kind": "MOVE_UNIT", "target": "unit1", "destination": {"x": 1, "y": 0}}


async def inspect(gateway, job, unit="unit1"):
    return await gateway.call(job, "unit_details", {"target": unit, "sections": ["movement"]}, "inspect-" + unit)


def test_movement_reports_zero_or_actual_discovery_and_replays_without_recounting(tmp_path):
    async def run():
        async with session(tmp_path) as (gateway, job, host, _):
            await gateway.call(job, "player_turn", {}, "start")
            await inspect(gateway, job)
            no_discovery = await gateway.call(job, "player_command", MOVE, "known-tile")
            assert no_discovery["exploration"] == {
                "exploredTilesBefore": 1,
                "exploredTilesAfter": 1,
                "newlyExploredTiles": 0,
            }
            await gateway.call(job, "unit_details", {"target": "unit1", "sections": ["movement"]}, "refresh")
            host.after_command = lambda: host.view["map"]["tiles"].append(
                {"position": {"x": 2, "y": 0}, "visible": True, "terrain": "Grassland"}
            )
            discovered = await gateway.call(job, "player_command", MOVE, "frontier")
            assert discovered["exploration"]["newlyExploredTiles"] == 1
            reads = host.reads
            assert await gateway.call(job, "player_command", MOVE, "frontier") == discovered
            assert host.reads == reads and len(host.executed) == 2

    asyncio.run(run())


def test_scout_identifies_settler_destination_blocker_in_action_update_without_repeated_reads(tmp_path):
    async def run():
        async with session(tmp_path) as (gateway, job, host, _):
            settler = host.view["map"]["units"][1]
            unidentified = {
                "siteStatus": "BLOCKED",
                "siteBlockers": [{"code": "CITY_TOO_CLOSE", "city": None}],
                "uncertainty": None,
            }
            settler.update(
                name="Settler",
                settlement={"siteStatus": "BLOCKED", "siteBlockers": [{"code": "CITY_TOO_CLOSE", "city": "Rome"}]},
                movementOrder={"destination": {"x": 4, "y": 0}, "canCancel": True, "settlement": unidentified},
            )
            base = await gateway.call(job, "player_turn", {}, "start")
            shown = base["units"][1]
            assert shown["settlementStatus"] == "BLOCKED"  # Current tile, not the destination.
            assert shown["movementOrder"]["settlement"] == unidentified
            detail = await gateway.call(job, "unit_details", shown["inspections"]["FOUND_CITY"]["arguments"], "site")
            assert detail["data"]["entity"]["movementOrder"] == {
                "destination": settler["movementOrder"]["destination"],
                "settlement": unidentified,
            }
            await inspect(gateway, job)
            blocked = {
                "siteStatus": "BLOCKED",
                "siteBlockers": [
                    {
                        "code": "CITY_TOO_CLOSE",
                        "city": "Paris",
                        "position": {"x": 6, "y": 0},
                        "distance": 2,
                        "minimumDistance": 4,
                    }
                ],
                "uncertainty": None,
            }
            host.after_command = lambda: settler["movementOrder"].update(settlement=blocked)
            result = await gateway.call(job, "player_command", MOVE, "scout")
            update = result["stateUpdate"]
            changed = next(row for row in update["collections"]["units"]["upsert"] if row["handle"] == "unit2")
            assert changed["movementOrder"]["settlement"] == blocked
            assert changed["position"] == shown["position"] and changed["settlementStatus"] == "BLOCKED"
            assert detail["query"] in update["invalidatedDetails"]
            assert apply_overview_update(base, update) == job.delivered_overview
            assert host.reads == 2 and len(host.executed) == 1

            host.after_command = lambda: None
            await gateway.call(
                job, "unit_details", {"target": "unit1", "sections": ["movement"]}, "inspect-scout-again"
            )
            unchanged = await gateway.call(job, "player_command", MOVE, "scout-again")
            assert "unit2" not in json.dumps(unchanged["stateUpdate"])  # Unchanged destination not repeated.

            host.after_command = lambda: settler.update(movementOrder=None)
            arrived = await gateway.call(job, "player_command", MOVE, "arrival")
            row = arrived["stateUpdate"]["collections"]["units"]["upsert"][0]
            assert row["handle"] == "unit2" and "movementOrder" not in row
            assert job.delivered_overview is not None
            assert "movementOrder" not in job.delivered_overview["units"][1]
            assert len(host.executed) == 3  # No automatic cancellation or redirection.

    asyncio.run(run())


@pytest.mark.parametrize("queue", [["Calendar"], ["Mining", "Calendar"]])
def test_research_result_reports_actual_active_technology_and_retained_progress_without_extra_reads(tmp_path, queue):
    async def run():
        async with session(tmp_path) as (gateway, job, host, memory):
            host.view.update(
                researchQueue=["Mining"],
                research={
                    "current": "Mining",
                    "sciencePerTurn": "5",
                    "overflowScience": 0,
                    "technologies": [{"name": "Mining", "researched": False, "scienceStored": 20}],
                },
            )
            before = await gateway.call(job, "player_turn", {}, "start")

            def changed():
                host.view["researchQueue"] = queue
                host.view["research"]["current"] = queue[0]

            host.after_command = changed
            arguments = {
                "kind": "SET_RESEARCH_QUEUE",
                "queue": queue,
                "intent": {"reason": "Choose the research order"},
            }
            result = await gateway.call(job, "player_command", arguments, "research")
            assert result["researchChange"]["current"] == queue[0]
            if queue[0] == "Calendar":
                assert result["researchChange"]["paused"] == {"technology": "Mining", "scienceStored": 20}
            else:
                assert "paused" not in result["researchChange"]
            assert apply_overview_update(before, result["stateUpdate"])["researchQueue"] == queue
            assert await gateway.call(job, "player_command", arguments, "research") == result
            assert host.reads == 2 and len(host.executed) == 1

    asyncio.run(run())


def test_successive_actions_use_updates_and_only_changed_inspections_expire(tmp_path):
    async def run():
        async with session(tmp_path) as (gateway, job, host, memory):
            base = await gateway.call(job, "player_turn", {}, "start")
            detail = await inspect(gateway, job)
            other = await inspect(gateway, job, "unit2")
            receipt = await gateway.call(job, "player_command", MOVE, "move")
            delta = receipt["stateUpdate"]
            assert receipt["feedback"] == {"paragraphs": ["Native outcome"]}
            assert delta["invalidatedDetails"] == [detail["query"]]
            assert delta["collections"]["units"]["upsert"][0]["position"] == MOVE["destination"]
            assert [q.model_dump() for q in job.details_read] == [other["query"]]
            assert apply_overview_update(base, delta) == job.delivered_overview
            assert host.reads == 2  # One explicit read, one internal read.
            create(memory, strategy(sources=["observation:overview"]), job=job.id)
            # Earlier delivered detail remains citeable as historical evidence,
            # but cannot be pinned as a current assumption without a new read.
            stale = memory.patch(
                job.id,
                [
                    {
                        "op": "create",
                        "content": {
                            "type": "belief",
                            "claim": "The old path is still current",
                            "uncertainty": "medium",
                            "sources": [detail["evidence"]],
                        },
                    }
                ],
            )
            assert stale["changes"][0]["accepted"]
            for source in ("observation:game", "observation:unit/unit1"):
                assessment = memory.patch(
                    job.id,
                    [
                        {
                            "op": "create",
                            "content": {
                                "type": "belief",
                                "claim": "Historical assessment",
                                "uncertainty": "medium",
                                "sources": [source],
                            },
                        }
                    ],
                )["changes"][0]
                assert assessment["accepted"] and assessment["warnings"]
            next_move = await gateway.call(job, "player_command", MOVE, "unread-move")
            assert next_move["done"]
            second = await gateway.call(job, "player_command", {**MOVE, "target": "unit2"}, "other-move")
            assert second["done"] and len(host.executed) == 3
            latest = copy.deepcopy(job.delivered_overview)
            reads = host.reads
            assert await gateway.call(job, "player_command", MOVE, "move") == receipt
            assert job.delivered_overview == latest and host.reads == reads and len(host.executed) == 3

    asyncio.run(run())


@pytest.mark.parametrize("source_kind", ["overview", "detail"])
def test_actions_deliver_flagged_plans_without_delivering_unread_evidence(tmp_path, source_kind):
    async def run():
        async with session(tmp_path) as (gateway, job, _, memory):
            base = await gateway.call(job, "player_turn", {}, "start")
            detail = await inspect(gateway, job)
            source = "observation:overview" if source_kind == "overview" else detail["evidence"]
            key = create(
                memory, strategy(assumptions=[{"text": "The position is unchanged", "source": source}]), job=job.id
            )
            result = await gateway.call(job, "player_command", MOVE, "move")
            assert "memoryUpdates" not in result
            updated = stored_records(memory)[key]
            assert updated["version"] == 1
            assert job.delivered_overview is not None
            if source_kind == "detail":
                assert job.delivered_overview["strategyReview"]["assessments"][0]["id"] == key
                assert job.delivered_overview["strategyReview"]["assessments"][0]["needsReview"]
            else:
                assert job.delivered_overview["strategyReview"]["assessments"] == []
            assert apply_overview_update(base, result["stateUpdate"]) == job.delivered_overview
            assert job.delivered_overview is not None and "memoryUpdates" not in job.delivered_overview
            args = {
                "changes": [
                    {
                        "op": "replace",
                        "id": key,
                        "content": strategy(reasons=["Hold the new position"]),
                    }
                ]
            }
            revision = await gateway.call(job, "memory_update", args, "review")
            # The revised strategy explicitly drops the old positional assumption;
            # it need not inspect an abandoned dependency to do so.
            assert revision["changes"][0]["accepted"]
            assert not job.memory["records"][key]["body"]["needsReview"]
            newest = copy.deepcopy(job.memory["records"][key])
            assert newest["version"] > updated["version"]
            assert await gateway.call(job, "player_command", MOVE, "move") == result
            assert job.memory["records"][key] == newest  # Old receipt never rewinds delivery.
            assert "memoryUpdates" not in await gateway.call(job, "player_turn", {}, "unchanged")

    asyncio.run(run())


def test_communication_cannot_retire_notebook_entries_or_deliver_later_contributions(tmp_path):
    async def run():
        async with session(tmp_path) as (gateway, job, _, memory):
            base = await gateway.call(job, "player_turn", {}, "start")
            key = create(memory, strategy(), job=job.id)
            chat = await gateway.open("chat", "communication", "g1", job.connection, {}, 0)
            unseen = create(memory, strategy(assessment="Undelivered contribution"), job=chat.id)
            assert not memory.patch(chat.id, [{"op": "retire", "id": key, "reason": "abandoned"}])["changes"][0][
                "accepted"
            ]
            await inspect(gateway, job)
            result = await gateway.call(job, "player_command", MOVE, "move")
            assert apply_overview_update(base, result["stateUpdate"]) == job.delivered_overview
            assert "memoryUpdates" not in result
            assessment = memory.patch(
                job.id,
                [
                    {
                        "op": "create",
                        "content": {
                            "type": "belief",
                            "claim": "Communication assessment",
                            "uncertainty": "medium",
                            "sources": [unseen],
                        },
                    }
                ],
            )["changes"][0]
            assert assessment["accepted"] and assessment["warnings"][0]["sources"] == [unseen]
            assert not memory.refresh_memory(job.id, [unseen])

    asyncio.run(run())


def test_new_alerts_and_decisions_arrive_without_repeating_old_alert_content(tmp_path):
    async def run():
        async with session(tmp_path) as (gateway, job, host, _):
            old = {"handle": "alert-old", "kind": "WonderBuilt", "content": {"paragraphs": ["Old long text " * 300]}}
            new = {
                "handle": "alert-new",
                "kind": "FirstContact",
                "canAcknowledge": True,
                "content": {"paragraphs": ["Full greeting and decisions"]},
            }
            host.view["alerts"] = [old]
            await gateway.call(job, "player_turn", {}, "start")
            await inspect(gateway, job)
            host.after_command = lambda: host.view.update(alerts=[old, new], pending=[{"kind": "ChooseTechnology"}])
            receipt = await gateway.call(job, "player_command", MOVE, "move")
            update = receipt["stateUpdate"]
            assert update["collections"]["alerts"]["upsert"] == [
                {
                    **new,
                    "content": {"kind": "FirstContact", "paragraphs": []},
                    "command": {"kind": "ACKNOWLEDGE_ALERT", "target": "alert-new"},
                }
            ]
            assert update["set"]["pending"] == [{"kind": "ChooseTechnology"}]
            assert "Old long text" not in json.dumps(update)
            host.after_command = lambda: None
            acknowledged = await gateway.call(
                job, "player_command", {"kind": "ACKNOWLEDGE_ALERT", "target": "alert-new"}, "ack"
            )
            assert acknowledged["done"] and host.executed[-1]["revision"] == "rev2"
            assert acknowledged["stateUpdate"]["collections"]["alerts"]["removed"] == ["alert-new"]

    asyncio.run(run())


def test_visibility_changes_expire_regions_without_inventing_destruction_or_hidden_movement(tmp_path):
    async def run():
        async with session(tmp_path) as (gateway, job, host, _):
            await gateway.call(job, "player_turn", {}, "start")
            await inspect(gateway, job)
            query = {"center": {"x": 0, "y": 0}, "radius": 2}
            region = await gateway.call(job, "map_region", query, "region")

            def reveal():
                host.view["map"]["tiles"][0]["visible"] = False
                host.view["map"]["tiles"].append({"position": {"x": 1, "y": 0}, "visible": True, "terrain": "Hill"})
                host.view["map"]["units"] = host.view["map"]["units"][:1]

            host.after_command = reveal
            result = await gateway.call(job, "player_command", MOVE, "move")
            update = result["stateUpdate"]
            assert region["query"] in update["invalidatedDetails"]
            assert update["set"]["mapSummary"]["exploredTiles"] == 2
            assert update["collections"]["units"]["removed"] == ["unit2"]
            assert "destroyed" not in json.dumps(update)
            fresh = await gateway.call(job, "map_region", query, "new-region")
            assert [tile["visible"] for tile in fresh["data"]["tiles"]] == [False, True]
            assert not (await gateway.call(job, "map_region", {"center": {"x": 99, "y": 99}}, "unknown"))["available"]

    asyncio.run(run())


def test_refresh_failure_keeps_receipt_and_does_not_deliver_partial_evidence(tmp_path):
    async def run():
        async with session(tmp_path) as (gateway, job, host, memory):
            await gateway.call(job, "player_turn", {}, "start")
            await inspect(gateway, job)
            before = stored_records(memory)["observation:overview"]
            host.after_command = lambda: setattr(host, "fail_refresh", True)
            receipt = await gateway.call(job, "player_command", MOVE, "move")
            assert receipt["done"] and receipt["stateUpdate"]["kind"] == "unavailable"
            assert stored_records(memory)["observation:overview"] == before
            assert job.observation is None and not memory.pending()
            assert await gateway.call(job, "player_command", MOVE, "move") == receipt
            assert len(host.executed) == 1
            rejected = await gateway.call(job, "player_command", MOVE, "new-id")
            assert rejected["error"] == "OBSERVATION_REQUIRED" and len(host.executed) == 1
            host.fail_refresh = False
            host.view["turn"] = 0
            host.after_command = lambda: None
            fresh = await gateway.call(job, "player_turn", {}, "recover")
            assert fresh["units"][0]["position"] == MOVE["destination"]

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["invalid_view", "save_response"])
def test_invalid_state_update_stops_for_inspection_and_keeps_committed_receipt(tmp_path, failure):
    async def run():
        async with session(tmp_path) as (gateway, job, host, memory):
            await gateway.call(job, "player_turn", {}, "start")
            await inspect(gateway, job)
            before = stored_records(memory)["observation:overview"]
            key = create(
                memory,
                strategy(assumptions=[{"text": "The overview is current", "source": "observation:overview"}]),
                job=job.id,
            )
            plan_before = stored_records(memory)[key]
            if failure == "invalid_view":
                host.after_command = lambda: host.view.update(turn="malformed")
            else:
                original = memory._call_record
                fail_once = True

                def fail_commit(db, job, id, name, arguments, result=None):
                    nonlocal fail_once
                    if fail_once and result and result.get("stateUpdate", {}).get("kind") == "delta":
                        assert "memoryUpdates" not in result
                        assert "strategyReview" not in result["stateUpdate"]["set"]
                        fail_once = False
                        raise OSError("Interrupted response write")
                    return original(db, job, id, name, arguments, result)

                memory._call_record = fail_commit
            result = await gateway.call(job, "player_command", MOVE, "move")
            assert result["error"] == "STATE_UPDATE_FAILED" and result["applied"] is True
            assert result["done"] and result["recoverable"] is False
            assert gateway.failed.is_set() and len(host.executed) == 1
            receipt = memory.call_receipt(job.id, "move")
            assert receipt is not None and receipt["done"] and not memory.pending()
            assert stored_records(memory)["observation:overview"] == before
            assert stored_records(memory)[key] == plan_before
            with memory.transaction() as db:
                snapshot = json.loads(
                    db.exec_driver_sql("SELECT snapshot FROM jobs WHERE id=?", (job.id,)).scalar_one()
                )
                assert snapshot["delivered"][key] == plan_before["version"]
            assert memory.run_failure()["exceptionType"] == (
                "ValidationError" if failure == "invalid_view" else "OSError"
            )
            with pytest.raises(StateConflict, match="STATE_UPDATE_FAILED"):
                await gateway.call(job, "player_command", MOVE, "retry")

    asyncio.run(run())


def test_end_turn_never_queries_or_delivers_the_next_players_state(tmp_path):
    async def run():
        async with session(tmp_path) as (gateway, job, host, _):
            await gateway.call(job, "player_turn", {}, "start")
            host.after_command = lambda: setattr(host, "fail_refresh", True)
            result = await gateway.call(job, "player_command", {"kind": "END_TURN"}, "end")
            assert result["stateUpdate"]["kind"] == "turn_ended" and host.reads == 1
            assert job.observation is None and job.delivered_overview is None
            assert await gateway.call(job, "player_command", {"kind": "END_TURN"}, "end") == result
            assert len(host.executed) == 1

    asyncio.run(run())


def test_interrupted_refresh_replays_the_committed_action_as_a_full_resync(tmp_path):
    async def run():
        async with session(tmp_path) as (gateway, job, host, memory):
            await gateway.call(job, "player_turn", {}, "start")
            await inspect(gateway, job)
            native = host.tool
            waiting, release = asyncio.Event(), asyncio.Event()

            async def block_refresh(name, args):
                if name == "player_turn" and host.executed and not release.is_set():
                    waiting.set()
                    await release.wait()
                return await native(name, args)

            host.tool = block_refresh
            pending = asyncio.create_task(gateway.call(job, "player_command", MOVE, "move"))
            await asyncio.wait_for(waiting.wait(), 1)
            assert not gateway.dispatch.locked() and len(host.executed) == 1
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert job.observation is None and memory.call_receipt(job.id, "move") is not None
            release.set()
            replay = await gateway.call(job, "player_command", MOVE, "move")
            assert replay["stateUpdate"]["kind"] == "full" and len(host.executed) == 1

    asyncio.run(run())


def test_terminal_result_and_notifications_are_delivered_even_when_no_further_action_is_possible(tmp_path):
    async def run():
        async with session(tmp_path) as (gateway, job, host, _):
            await gateway.call(job, "player_turn", {}, "start")
            await inspect(gateway, job)

            def victory():
                host.view["result"] = {"winner": "Rome", "victoryType": "Science"}
                host.view["canEndTurn"] = False
                host.view["notifications"] = [
                    {
                        "handle": "notice1",
                        "text": "Victory",
                        "category": "General",
                        "icons": [],
                        "locations": [],
                        "canDismiss": False,
                    }
                ]

            host.after_command = victory
            result = await gateway.call(job, "player_command", MOVE, "victory")
            update = result["stateUpdate"]
            assert update["set"]["result"] == host.view["result"] and update["set"]["canEndTurn"] is False
            assert update["collections"]["notifications"]["upsert"] == host.view["notifications"]

    asyncio.run(run())


def test_receipt_without_response_or_baseline_recovers_as_full_without_reexecution(tmp_path):
    async def run():
        async with session(tmp_path) as (gateway, job, host, memory):
            await gateway.call(job, "player_turn", {}, "start")
            memory.call(job.id, "move", "player_command", MOVE)
            queued = memory.queue(job.id, "player_command", {**MOVE, "epoch": "g1", "revision": "rev1"}, "move")
            memory.receipt(queued["id"], await host.tool("player_command", json.loads(queued["payload"])))
            gateway.forget_observation(job)
            result = await gateway.call(job, "player_command", MOVE, "move")
            assert result["done"] and result["stateUpdate"]["kind"] == "full"
            assert result["stateUpdate"]["overview"]["units"][0]["position"] == MOVE["destination"]
            assert len(host.executed) == 1 and not job.details_read
            assert await gateway.call(job, "player_command", MOVE, "move") == result

    asyncio.run(run())


def test_restart_and_rollback_require_a_fresh_baseline_and_do_not_resume_old_updates(tmp_path):
    async def run():
        async with session(tmp_path) as (gateway, job, host, memory):
            initial = await gateway.call(job, "player_turn", {}, "start")
            checkpoint = tmp_path / "checkpoint.json"
            memory.pause(True)
            checksum = memory.backup(checkpoint)
            memory.pause(False)
            await inspect(gateway, job)
            result = await gateway.call(job, "player_command", MOVE, "move")
            latest = result["stateUpdate"]["stateId"]
            memory.pause(True)
            memory.restore(checkpoint, checksum, "restored-epoch")
            memory.pause(False)
            with pytest.raises(StateConflict):
                await gateway.call(job, "player_command", MOVE, "move")
            restarted = CivilizationState(tmp_path / "private", "Rome")
            assert not restarted.pending()
            assert initial["stateId"] != latest
            # A new process/job never trusts a baseline reconstructed from old private history.
            next_gateway = ProposalGateway(restarted, gateway.io, tmp_path / "workspace")
            new_job = await next_gateway.open("new", "gameplay", "restored-epoch", host, {}, 0)
            assert new_job.observation is None and new_job.delivered_overview is None and not new_job.details_read
            await next_gateway.close(new_job, True)

    asyncio.run(run())
