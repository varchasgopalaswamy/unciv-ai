import asyncio
import json
import sqlite3

import pytest
from pydantic import ValidationError

from civ_agent.civilization_state import CivilizationState, StateConflict
from civ_agent.memory_history import summary_markdown
from civ_agent.memory_records import MemoryReadResult, MemorySearchResult, MemoryUpdate, MemoryUpdateResult, TurnSummary
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.service_io import ServiceIO
from civ_agent.turn_archive import TurnArchive
from tests.test_proposal_gateway import Host


def plan(**changes):
    return {
        "type": "plan",
        "objective": "Defend Rome",
        "approach": "Hold the border",
        "successCriteria": "Rome survives",
        **changes,
    }


def setup(tmp_path, limit=12000):
    memory = CivilizationState(tmp_path / "private", "Rome", limit)
    memory.bind("g1")
    memory.start_job("game", "gameplay", {}, game_turn=4)
    memory.observe_player({"turn": 4, "cities": [{"handle": "c1"}], "map": {"units": [{"handle": "u1"}]}}, "game")
    return memory


def create(memory, content, job="game"):
    result = memory.patch(job, [{"op": "create", "content": content}])
    MemoryUpdateResult.model_validate(result)
    assert result["changes"][0]["accepted"], result
    return result["changes"][0]["id"]


def current(memory, id):
    return memory.snapshot("gameplay")["records"][id]["body"]


@pytest.mark.parametrize("reason", ["completed", "abandoned", "superseded", "retracted", "irrelevant"])
def test_retirement_keeps_history_and_does_not_resurrect(tmp_path, reason):
    memory = setup(tmp_path)
    key = create(memory, {"type": "note", "text": "Old border report", "subjects": ["Greece"]})
    memory.export(tmp_path / "workspace")
    assert (tmp_path / f"workspace/notes/{key}.md").exists()
    retired = {"op": "retire", "id": key, "reason": reason, "assessment": "No longer useful"}
    assert memory.patch("game", [retired])["changes"][0]["accepted"]
    memory.export(tmp_path / "workspace")
    assert not (tmp_path / f"workspace/notes/{key}.md").exists()
    assert not (tmp_path / "workspace/relationships/Greece.md").exists()
    assert key not in memory.context(memory.snapshot("gameplay"))
    assert not memory.refresh_memory("game", [key])
    assert not memory.search_memory("game", {"type": "note"})["records"]
    history = memory.search_memory("game", {"id": key, "history": True, "state": "all"})
    MemorySearchResult.model_validate(history)
    assert [r["state"] for r in history["records"]] == ["retired", "active"]
    assert current(memory, key)["retirementReason"] == reason
    assert not memory.patch("game", [{"op": "amend", "id": key, "fields": {"text": "Resurrect"}}])["changes"][0][
        "accepted"
    ]
    result = memory.patch("game", [{"op": "create", "content": {"type": "note", "text": "New report"}}])
    successor = result["changes"][0]["id"]
    assert successor != key and current(memory, key)["state"] == "retired"


def test_expiry_uses_trusted_turn_and_completion_is_an_assessment(tmp_path):
    memory = setup(tmp_path)
    key = create(memory, plan(expiresTurn=6))
    memory.start_job("chat", "communication", {})
    memory.observe("communication", {"context": {"turn": 5}}, "chat")
    assert current(memory, key)["state"] == "active"
    memory.observe("communication", {"context": {"turn": 6}}, "chat")
    assert current(memory, key)["state"] == "active"
    memory.finish_job("game", True)
    memory.start_job("next", "gameplay", {}, game_turn=6)
    assert current(memory, key)["retirementReason"] == "expired"
    assert current(memory, key)["updatedTurn"] == 6
    assert not memory.summary(4)["actions"]


def test_context_limit_whole_records_and_only_delivered_sources(tmp_path):
    memory = setup(tmp_path, 1000)
    ids = [
        create(memory, {"type": "note", "text": str(n) + "x" * 450, "subjects": ["economy"], "priority": 5 - n})
        for n in range(3)
    ]
    memory.finish_job("game", True)
    snapshot = memory.start_job("next", "gameplay", {})
    context = json.loads(memory.context(snapshot).split("\n", 1)[1])
    assert context["omittedRecords"] == 2
    assert context["records"][0]["id"] == ids[0]
    assert len(json.dumps(context["records"])) <= 1000
    claim = {
        "op": "create",
        "content": {"type": "belief", "claim": "Note supports planning", "uncertainty": "high", "sources": [ids[-1]]},
    }
    result = memory.patch("next", [claim])["changes"][0]
    assert result["accepted"] and result["warnings"][0]["code"] == "UNVERIFIED_REFERENCES"
    memory.refresh_memory("next", [ids[-1]])
    assert memory.patch("next", [claim])["changes"][0]["accepted"]


def test_belief_retraction_flags_plan_but_old_sighting_is_historical(tmp_path):
    memory = setup(tmp_path)
    memory.observe("border", {"turn": 4, "units": ["Greek warrior"]}, "game")
    belief = create(
        memory,
        {"type": "belief", "claim": "Greece may attack", "uncertainty": "medium", "sources": ["observation:border"]},
    )
    campaign = create(memory, plan(assumptions=[{"text": "Greece may attack", "source": belief}]))
    historical_belief = create(memory, plan(sources=[belief]))
    history = create(memory, plan(objective="Remember the sighting", sources=["observation:border"]))
    dependent = create(memory, plan(assumptions=[{"text": "Warrior remains nearby", "source": "observation:border"}]))
    memory.observe("border", {"turn": 5, "units": ["Greek warrior"]}, "game")
    assert not current(memory, dependent)["needsReview"]
    memory.observe("border", {"turn": 5, "units": []}, "game")
    assert current(memory, dependent)["needsReview"]
    assert not current(memory, history)["needsReview"]
    memory.patch("game", [{"op": "retire", "id": belief, "reason": "retracted"}])
    assert current(memory, campaign)["needsReview"]
    assert not current(memory, historical_belief)["needsReview"]


def test_missing_references_are_unverified_assessments_without_new_reads(tmp_path):
    memory = setup(tmp_path)
    memory.observe("border", {"turn": 4, "units": []}, "game")
    memory.finish_job("game", True)
    memory.start_job("next", "gameplay", {})
    args = [
        {
            "op": "create",
            "content": plan(
                sources=["observation:game"],
                assumptions=[{"text": "The border is clear", "source": "observation:border"}],
            ),
        }
    ]
    result = memory.patch("next", args)
    MemoryUpdateResult.model_validate(result)
    saved = result["changes"][0]
    assert saved["accepted"]
    assert set(saved["warnings"][0]["sources"]) == {"observation:border", "observation:game"}
    assert current(memory, saved["id"])["_evidence"] == []
    assert current(memory, saved["id"])["_assumptions"] == []


@pytest.mark.parametrize("subject", ["u1", "c1", "Greece"])
def test_subject_changes_flag_only_related_plans(tmp_path, subject):
    memory = setup(tmp_path)
    key = create(memory, plan(subjects=[subject]))
    unrelated = create(memory, plan(subjects=["economy"]))
    memory.ingest(
        "g1", [{"id": 1, "kind": "message", "body": {"participants": ["Greece", "Rome"], "text": "Hello"}}], 1
    )
    assert not current(memory, key)["needsReview"]
    if subject == "Greece":
        memory.ingest(
            "g1",
            [
                {
                    "id": 2,
                    "kind": "engine",
                    "body": {"participants": ["Greece", "Rome"], "action": "DECLARE_WAR", "turn": 4},
                }
            ],
            2,
        )
    else:
        memory.observe_player({"turn": 4, "cities": [], "map": {"units": []}}, "game")
    assert current(memory, key)["needsReview"]
    assert not current(memory, unrelated)["needsReview"]


def test_old_store_rejected_without_migration(tmp_path):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    with sqlite3.connect(directory / "memory.sqlite3") as db:
        db.execute("CREATE TABLE meta(key TEXT, value TEXT)")
    with pytest.raises(StateConflict, match="fresh match"):
        CivilizationState(directory, "Rome")


def test_sponsored_war_flags_plans_about_the_target_without_treating_chat_as_fact(tmp_path):
    memory = setup(tmp_path)
    target = create(memory, plan(subjects=["France"]))
    unrelated = create(memory, plan(subjects=["Egypt"]))
    body = {
        "participants": ["Rome", "Greece"],
        "action": "ENACT_TRADE",
        "wars": [{"declaringCivilization": "Greece", "targetCivilization": "France", "atWar": True}],
        "turn": 4,
    }
    memory.ingest("g1", [{"id": 1, "kind": "message", "body": body}], 1)
    assert not current(memory, target)["needsReview"]
    memory.ingest("g1", [{"id": 2, "kind": "engine", "body": body}], 2)
    assert current(memory, target)["needsReview"]
    assert not current(memory, unrelated)["needsReview"]


def test_native_diplomatic_receipts_refresh_related_plans_but_plain_chat_does_not(tmp_path):
    memory = setup(tmp_path)
    target = create(memory, plan(subjects=["Greece"]))
    unrelated = create(memory, plan(subjects=["Egypt"]))
    body = {"author": "Rome", "target": "Greece", "kind": "DENOUNCE", "status": "ENACTED", "turn": 4}
    memory.ingest("g1", [{"id": 1, "kind": "message", "body": body}], 1)
    assert not current(memory, target)["needsReview"]
    memory.ingest("g1", [{"id": 2, "kind": "diplomacy", "body": body}], 2)
    restored = CivilizationState(tmp_path / "private", "Rome")
    records = restored.snapshot("gameplay")["records"]
    assert records["event:1"]["category"] == "statement"
    assert records["event:2"]["category"] == "observation"
    assert current(restored, target)["needsReview"] == ["Diplomatic event 2 needs review"]
    assert not current(restored, unrelated)["needsReview"]


def test_search_pages_and_short_memory_ids_do_not_collide_with_messages(tmp_path):
    memory = setup(tmp_path)
    memory.ingest("g1", [{"id": 1, "kind": "message", "body": {"message": "m1", "text": "Hello"}}], 1)
    memory.refresh_memory("game", ["event:1"])
    ids = [create(memory, {"type": "note", "text": f"Market {i}"}) for i in range(23)]
    first = memory.search_memory("game", {"type": "note", "query": "Market"})
    second = memory.search_memory("game", {"type": "note", "query": "Market", "offset": first["nextOffset"]})
    assert len(first["records"]) == 20 and first["hasMore"]
    assert len(second["records"]) == 3 and not second["hasMore"]
    assert {r["id"] for r in first["records"] + second["records"]} == set(ids)
    belief = create(
        memory, {"type": "belief", "claim": "Greeting was cordial", "uncertainty": "high", "sources": ["m1", ids[0]]}
    )
    assert [ref["key"] for ref in current(memory, belief)["_evidence"]] == ["event:1", "mem1"]


def test_snapshot_restores_lifecycle_hides_future_revisions_and_keeps_ids_monotonic(tmp_path):
    memory = setup(tmp_path)
    key = create(memory, plan())
    memory.patch("game", [{"op": "retire", "id": key, "reason": "abandoned"}])
    saved = tmp_path / "round.json"
    checksum = memory.backup(saved)
    replacement = memory.patch("game", [{"op": "create", "content": plan(objective="ABANDONED_FUTURE")}])["changes"][0][
        "id"
    ]
    memory.export(tmp_path / "workspace")
    memory.restore(saved, checksum, "g2")
    memory.pause(False)
    memory.start_job("restored", "gameplay", {}, game_turn=4)
    assert current(memory, key)["retirementReason"] == "abandoned"
    assert not memory.search_memory("restored", {"history": True, "state": "all", "query": "ABANDONED_FUTURE"})[
        "records"
    ]
    assert not memory.refresh_memory("restored", [replacement], include_retired=True)
    assert "ABANDONED_FUTURE" not in json.dumps(memory.summary(4))
    new = create(memory, {"type": "note", "text": "Current timeline"}, "restored")
    assert int(new[3:]) > int(replacement[3:])
    memory.export(tmp_path / "workspace")
    assert "ABANDONED_FUTURE" not in (tmp_path / "workspace/memory-index.md").read_text()
    with pytest.raises(StateConflict):
        memory.patch("game", [])


def test_summaries_survive_interruptions_preserve_reason_and_receipt_truth(tmp_path):
    memory = setup(tmp_path)
    campaign = create(memory, plan(objective="Grow Rome"))
    queued = memory.queue(
        "game",
        "player_command",
        {"kind": "QUEUE_CONSTRUCTION", "target": "c1", "selection": "Granary"},
        {},
        "build",
        {"plan": campaign},
    )
    memory.receipt(queued["id"], {"kind": "QUEUE_CONSTRUCTION", "queue": ["Granary"]})
    move = memory.queue(
        "game",
        "player_command",
        {"kind": "MOVE_UNIT", "target": "u1", "destination": "far"},
        {},
        "move",
        {"reason": "Explore the coast"},
    )
    memory.uncertain(move["id"])
    memory.finish_job("game", False)
    first = memory.summary(4)
    assert first["actions"][0]["reason"] == "Grow Rome"
    assert first["actions"][1]["outcome"] == "uncertain"
    memory.receipt(move["id"], {"kind": "MOVE_UNIT", "position": "near", "remainingMovement": 0})
    summary = memory.summary(4)
    TurnSummary.model_validate(summary)
    assert summary["actions"][1]["receipt"]["position"] == "near"
    markdown = summary_markdown(summary)
    assert "Queued Granary" in markdown and "Grow Rome" in markdown and "Explore the coast" in markdown
    assert "built" not in markdown.lower() and "far" not in markdown and "attempt" not in markdown
    assert len(markdown.split()) <= 200
    memory.export(tmp_path / "workspace")
    assert json.loads((tmp_path / "workspace/turn_4/summary.json").read_text()) == summary


@pytest.mark.parametrize("uncertain", [False, True])
def test_archive_can_render_during_dispatch_before_receipt_arrives(tmp_path, uncertain):
    memory = setup(tmp_path)
    request = memory.queue(
        "game", "player_command", {"kind": "FOUND_CITY", "target": "u1"}, {}, "found", {"reason": "Found the capital"}
    )
    if uncertain:
        memory.uncertain(request["id"])
    summary = memory.summary(4)
    TurnSummary.model_validate(summary)
    archive = TurnArchive(tmp_path / "archive", tmp_path / "workspace", "Rome", "g1")
    artifacts = archive.update(4, "game", [], structured_summary=summary)
    assert artifacts["summaryData"]["actions"][0]["outcome"] == ("uncertain" if uncertain else "pending")
    memory.receipt(request["id"], {"kind": "FOUND_CITY", "city": "Rome"})
    artifacts = archive.update(4, "game", [], structured_summary=memory.summary(4))
    assert artifacts["summaryData"]["actions"][0]["outcome"] == "confirmed"
    assert "Found the capital" in artifacts["summary"]


def test_input_and_response_models_reject_invented_types_and_metadata():
    with pytest.raises(ValidationError):
        MemoryUpdate.model_validate(
            {"changes": [{"op": "create", "content": {"type": "observation", "text": "Invented"}}]}
        )
    with pytest.raises(ValidationError):
        MemoryUpdate.model_validate(
            {"changes": [{"op": "create", "content": {"type": "note", "text": "x", "createdTurn": 99}}]}
        )
    with pytest.raises(ValidationError):
        MemoryReadResult.model_validate({"records": [{"id": "mem1", "type": "plan", "text": "Not a typed plan"}]})


def test_summary_word_limit_includes_heading_and_list_markers():
    data = TurnSummary(turn=7, unresolved=["plan " * 500]).model_dump()
    markdown = summary_markdown(data)
    assert len(markdown.split()) == 200 and markdown.endswith("…\n")


def test_review_requires_reading_changed_evidence_and_retirement_preserves_past_summary(tmp_path):
    memory = setup(tmp_path)
    key = create(memory, plan(subjects=["Greece"]))
    memory.ingest(
        "g1",
        [{"id": 1, "kind": "engine", "body": {"participants": ["Greece", "Rome"], "action": "DECLARE_WAR", "turn": 4}}],
        1,
    )
    memory.refresh_memory("game", [key])
    change = {"op": "amend", "id": key, "fields": {k: v for k, v in (plan(subjects=["Greece"])).items() if k != "type"}}
    assert memory.patch("game", [change])["changes"][0]["accepted"]
    memory.refresh_memory("game", ["event:1"])
    assert memory.patch("game", [change])["changes"][0]["accepted"]
    memory.finish_job("game", True)
    before = memory.summary(4)
    memory.start_job("later", "gameplay", {}, game_turn=5)
    memory.patch("later", [{"op": "retire", "id": key, "reason": "completed"}])
    assert memory.summary(4) == before


def test_historical_evidence_is_pinned_without_replacing_current_job_version(tmp_path):
    memory = setup(tmp_path)
    memory.observe("border", {"turn": 4, "unit": "Greek warrior"}, "game")
    memory.observe("border", {"turn": 5, "unit": None}, "game")
    page = memory.search_memory("game", {"id": "observation:border", "history": True})
    old = page["records"][-1]["evidence"]
    key = create(
        memory,
        {"type": "belief", "claim": "Greece previously scouted our border", "uncertainty": "low", "sources": [old]},
    )
    assert current(memory, key)["_evidence"] == [
        {"key": "observation:border", "version": 1, "category": "observation", "historical": True}
    ]
    with memory.transaction() as db:
        snapshot = json.loads(db.exec_driver_sql("SELECT snapshot FROM jobs WHERE id='game'").scalar_one())
        assert snapshot["delivered"]["observation:border"] == 2


def test_intent_is_recorded_and_retirement_does_not_gate_dispatch(tmp_path):
    async def run():
        memory = CivilizationState(tmp_path / "private", "Rome")
        io = ServiceIO()
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = Host()
        try:
            game = await gateway.open("game", "gameplay", "g1", host, {}, 0)
            await gateway.call(game, "player_turn", {}, "observe")
            await gateway.call(game, "construction_details", {"target": "city1", "names": ["Granary"]}, "inspect")
            action = {"kind": "QUEUE_CONSTRUCTION", "target": "city1", "selection": "Granary"}
            missing = await gateway.call(game, "player_command", action, "missing")
            assert missing["error"] == "INTENT_REQUIRED" and not host.executed
            created = await gateway.call(
                game, "memory_update", {"changes": [{"op": "create", "content": plan()}]}, "plan"
            )
            key = created["changes"][0]["id"]
            assert (
                await gateway.call(game, "memory_update", {"changes": [{"op": "create", "content": plan()}]}, "plan")
                == created
            )
            await gateway.call(
                game, "memory_update", {"changes": [{"op": "retire", "id": key, "reason": "abandoned"}]}, "retire"
            )
            result = await gateway.call(game, "player_command", {**action, "intent": {"plan": key}}, "act")
            assert result["done"] and len(host.executed) == 1
            assert "intent" not in host.executed[0]
            assert memory.summary(0)["actions"][0]["reason"] == "Defend Rome"
            assert await gateway.call(game, "player_command", action, "missing") == missing
        finally:
            await io.close()

    asyncio.run(run())


def test_retirement_after_approval_cannot_undo_an_inflight_command(tmp_path):
    memory = setup(tmp_path)
    key = create(memory, plan())
    request = memory.queue("game", "player_command", {"kind": "ATTACK", "target": "u1"}, {}, "attack", {"plan": key})
    memory.patch("game", [{"op": "retire", "id": key, "reason": "abandoned"}])
    memory.receipt(request["id"], {"kind": "ATTACK", "damage": 10})
    assert memory.summary(4)["actions"][0]["outcome"] == "confirmed"
    assert memory.queue("game", "player_command", {"kind": "ATTACK", "target": "u1"}, {}, "another", {"plan": key})[
        "id"
    ]
