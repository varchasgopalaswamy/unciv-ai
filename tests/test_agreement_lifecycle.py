import asyncio
import copy
import json

import pytest
from jsonschema import Draft202012Validator

from civ_agent.civilization_state import CivilizationState, StateConflict
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.service_io import ServiceIO
from tests.test_proposal_gateway import Host
from tests.test_structured_memory import create, current, plan, setup


def agreement():
    return {
        "agreement": "a1",
        "version": 1,
        "partner": "Greece",
        "kind": "TRADE",
        "status": "ACTIVE",
        "remainingTurns": 5,
        "give": [],
        "receive": [{"type": "Gold_Per_Turn", "name": "Gold per turn", "amount": 4, "duration": 5}],
        "initialGive": [],
        "initialReceive": [{"type": "Gold_Per_Turn", "name": "Gold per turn", "amount": 4, "duration": 10}],
        "observedTurn": 0,
        "endedTurn": None,
        "history": [],
        "cancellation": {"available": False, "unavailableReason": "No native cancellation control."},
    }


def ended_event(id=1, **changes):
    value = agreement()
    return {
        "id": id,
        "kind": "agreement",
        "wake": True,
        "body": {
            "agreement": value["agreement"],
            "version": 2,
            "partner": value["partner"],
            "kind": value["kind"],
            "status": "ENDED",
            "change": "ENDED",
            "reason": "EXPIRED",
            "turn": 5,
            "before": {"give": value["give"], "receive": value["receive"]},
            "after": {"give": [], "receive": []},
            "effects": {"goldPerTurnFromTrades": 0},
            **changes,
        },
    }


class AgreementHost(Host):
    def __init__(self):
        super().__init__()
        self.agreement = agreement()
        self.reads = []

    def summaries(self):
        return [
            {
                key: self.agreement[key]
                for key in ("agreement", "version", "partner", "kind", "status", "remainingTurns", "cancellation")
            }
        ]

    async def tool(self, name, args):
        if name == "player_communication":
            if args["op"] == "state":
                return {**await super().tool(name, args), "agreements": self.summaries()}
            if args["op"] in {"agreement_list", "agreement_read"}:
                self.reads.append(copy.deepcopy(args))
                if args["op"] == "agreement_list":
                    return {"agreements": self.summaries()}
                if args["agreement"] != self.agreement["agreement"]:
                    return {"error": "AGREEMENT_UNAVAILABLE"}
                return {"agreement": copy.deepcopy(self.agreement)}
        return await super().tool(name, args)


@pytest.mark.parametrize("purpose", ["gameplay", "communication"])
def test_agreement_tools_route_reads_with_private_sources_and_exact_replay(tmp_path, purpose):
    async def run():
        io = ServiceIO()
        memory = CivilizationState(tmp_path / "private", "Rome")
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = AgreementHost()
        try:
            job = await gateway.open("job", purpose, "g1", host, {})
            tools = {tool["name"]: tool for tool in await gateway.tools(job)}
            assert "agreement_cancel" not in tools
            for name in ("agreement_list", "agreement_read"):
                assert tools[name]["annotations"]["readOnlyHint"] is True
            listed_schema = Draft202012Validator(tools["agreement_list"]["inputSchema"])
            read_schema = Draft202012Validator(tools["agreement_read"]["inputSchema"])
            assert listed_schema.is_valid({})
            assert not listed_schema.is_valid({"partner": "France"})
            assert read_schema.is_valid({"agreement": "a1"})
            assert not read_schema.is_valid({"agreement": "a1", "civilization": "Greece"})
            listed = await gateway.call(job, "agreement_list", {}, "list")
            assert listed["evidence"] == "observation:agreements"
            assert listed["agreements"] == host.summaries()
            # Seeing an ID/summary is not permission to cite unseen detailed terms.
            unavailable = await gateway.call(
                job,
                "memory_update",
                {"changes": [{"op": "create", "content": plan(sources=["observation:agreement/a1"])}]},
                "unread-source",
            )
            assert unavailable["changes"][0]["accepted"]
            assert unavailable["changes"][0]["warnings"][0]["code"] == "UNVERIFIED_REFERENCES"
            shown = await gateway.call(job, "agreement_read", {"agreement": "a1"}, "read")
            assert shown["agreement"] == host.agreement
            assert shown["evidence"] == "observation:agreement/a1"
            assert "epoch" not in shown
            remembered = await gateway.call(
                job,
                "memory_update",
                {
                    "changes": [
                        {
                            "op": "create",
                            "content": plan(
                                assumptions=[{"text": "Income funds construction", "source": shown["evidence"]}]
                            ),
                        }
                    ]
                },
                "remember",
            )
            assert remembered["changes"][0]["accepted"]
            host.agreement["status"] = "ENDED"
            assert await gateway.call(job, "agreement_read", {"agreement": "a1"}, "read") == shown
            assert host.reads == [
                {"op": "agreement_list", "epoch": "g1"},
                {"op": "agreement_read", "epoch": "g1", "agreement": "a1"},
            ]
            unknown = await gateway.call(job, "agreement_read", {"agreement": "foreign-id"}, "foreign")
            assert unknown == {"error": "AGREEMENT_UNAVAILABLE"}
            assert "observation:agreement/foreign-id" not in memory.snapshot(purpose)["records"]
            assert not memory.pending() and not host.executed
        finally:
            await io.close()

    asyncio.run(run())


def test_agreement_ending_flags_dependencies_and_relevant_plans_without_promoting_chat(tmp_path):
    memory = setup(tmp_path)
    memory.observe("agreement/a1", {"agreement": agreement()}, "game")
    dependent = create(memory, plan(assumptions=[{"text": "Greece pays us", "source": "observation:agreement/a1"}]))
    partner = create(memory, plan(subjects=["Greece"]))
    economy = create(memory, plan(subjects=["economy"]))
    unrelated = create(memory, plan(subjects=["France"]))
    historical = create(memory, plan(sources=["observation:agreement/a1"]))
    event = ended_event()
    memory.ingest("g1", [{**event, "kind": "message"}], 1)
    assert all(not current(memory, key)["needsReview"] for key in (dependent, partner, economy))
    memory.ingest("g1", [ended_event(2)], 2)
    for key in (dependent, partner, economy):
        assert current(memory, key)["needsReview"] == ["Agreement event 2 needs review"]
        assert current(memory, key)["_review"] == [{"key": "event:2", "version": 1}]
    assert not current(memory, unrelated)["needsReview"]
    assert not current(memory, historical)["needsReview"]
    assert memory.snapshot("gameplay")["records"]["event:1"]["category"] == "statement"
    assert memory.snapshot("gameplay")["records"]["event:2"]["category"] == "observation"
    assert memory.has_wake()
    assert memory.queue("game", "player_command", {"kind": "BUY_CONSTRUCTION"}, {}, "old-plan", {"plan": dependent})[
        "id"
    ]


@pytest.mark.parametrize(
    ("kind", "term", "subjects"),
    [
        ("FRIENDSHIP", None, {"Greece"}),
        ("EMBASSY", None, {"Greece"}),
        ("RESEARCH_AGREEMENT", None, {"Greece", "economy"}),
        ("DEFENSIVE_PACT", None, {"Greece", "defense"}),
        ("TRADE", {"type": "Agreement", "name": "Open Borders"}, {"Greece", "defense", "expansion"}),
        ("TRADE", {"type": "Strategic_Resource", "name": "Iron"}, {"Greece", "economy"}),
    ],
)
def test_relationship_endings_select_relevant_plan_subjects(tmp_path, kind, term, subjects):
    memory = setup(tmp_path)
    plans = {
        subject: create(memory, plan(subjects=[subject]))
        for subject in ("Greece", "economy", "defense", "expansion", "France")
    }
    event = ended_event(kind=kind, before={"give": [], "receive": [term] if term else []})
    memory.ingest("g1", [event], 1)
    assert {subject for subject, key in plans.items() if current(memory, key)["needsReview"]} == subjects


def test_reviewed_lifecycle_event_stays_reviewed_on_reconnect_and_restart(tmp_path):
    memory = setup(tmp_path)
    key = create(memory, plan(subjects=["Greece"]))
    event = ended_event()
    memory.ingest("g1", [event], 1)
    memory.refresh_memory("game", [key, "event:1"])
    assert memory.patch(
        "game",
        [{"op": "amend", "id": key, "fields": {k: v for k, v in (plan(subjects=["Greece"])).items() if k != "type"}}],
    )["changes"][0]["accepted"]
    memory.ingest("g1", [event], 1)
    assert not current(memory, key)["needsReview"]
    restarted = CivilizationState(tmp_path / "private", "Rome")
    restarted.ingest("g1", [event], 1)
    assert not current(restarted, key)["needsReview"]
    # An already committed identity cannot replace evidence or rerun side effects.
    restarted.ingest("g1", [ended_event(reason="FORGED_REPLAY", turn=99)], 1)
    snapshot = restarted.snapshot("communication")
    assert snapshot["records"]["event:1"]["body"] == event
    assert snapshot["events"] == [event]
    assert restarted.cursor() == 1


def test_late_event_does_not_invalidate_a_plan_based_on_current_agreement(tmp_path):
    memory = setup(tmp_path)
    value = {**agreement(), "version": 2, "status": "ENDED", "receive": [], "remainingTurns": 0}
    memory.observe("agreement/a1", {"agreement": value}, "game")
    key = create(
        memory, plan(subjects=["Greece"], assumptions=[{"text": "Income ended", "source": "observation:agreement/a1"}])
    )
    memory.ingest("g1", [ended_event()], 1)
    assert not current(memory, key)["needsReview"]
    assert not current(memory, "observation:agreement/a1").get("requiresRefresh")


def test_countdown_reads_do_not_flag_subject_plans_but_native_expiry_does(tmp_path):
    memory = setup(tmp_path)
    value = agreement()
    value["receive"][0]["displayText"] = "Gold per turn 4 (5 turns)"
    memory.observe("agreement/a1", {"agreement": copy.deepcopy(value)}, "game")
    key = create(memory, plan(subjects=["economy"]))
    value["version"] += 1
    value["remainingTurns"] = 4
    value["receive"][0].update(duration=4, displayText="Gold per turn 4 (4 turns)")
    memory.observe("agreement/a1", {"agreement": copy.deepcopy(value)}, "game")
    assert not current(memory, key)["needsReview"]
    value["version"] += 1
    value["receive"][0].update(duration=0, displayText="Gold per turn 4")
    memory.observe("agreement/a1", {"agreement": copy.deepcopy(value)}, "game")
    assert current(memory, key)["needsReview"]


def test_older_read_response_cannot_erase_a_newer_lifecycle_event(tmp_path):
    memory = setup(tmp_path)
    memory.observe("agreement/a1", {"agreement": agreement()}, "game")
    memory.ingest("g1", [ended_event(version=3)], 1)
    with pytest.raises(StateConflict, match="AGREEMENT_CHANGED"):
        memory.observe("agreement/a1", {"agreement": {**agreement(), "version": 2}}, "game")
    assert current(memory, "observation:agreement/a1")["latestVersion"] == 3
    memory.ingest("g1", [ended_event(id=2, version=2)], 2)
    assert current(memory, "observation:agreement/a1")["latestVersion"] == 3


def test_lifecycle_event_invalidates_older_list_even_if_full_read_is_current(tmp_path):
    memory = setup(tmp_path)
    memory.observe("agreements", {"agreements": [agreement()]}, "game")
    dependent = create(memory, plan(assumptions=[{"text": "Income continues", "source": "observation:agreements"}]))
    ended = {**agreement(), "version": 2, "status": "ENDED", "remainingTurns": 0, "receive": []}
    memory.observe("agreement/a1", {"agreement": ended}, "game")
    memory.ingest("g1", [ended_event()], 1)
    assert current(memory, dependent)["needsReview"]
    assert current(memory, "observation:agreements")["requiresRefresh"]
    assert not current(memory, "observation:agreement/a1").get("requiresRefresh")
    assert memory.queue(
        "game",
        "player_command",
        {"kind": "BUY_CONSTRUCTION"},
        {},
        "historical-intent",
        {
            "reason": "Spend expected income",
            "assumptions": [{"text": "Income continues", "source": "observation:agreements"}],
        },
    )["id"]
    with pytest.raises(StateConflict, match="agreement_list"):
        memory.observe("agreements", {"agreements": [agreement()]}, "game")
    memory.observe("agreements", {"agreements": [ended]}, "game")
    assert not current(memory, "observation:agreements").get("requiresRefresh")


def test_historical_intent_does_not_block_friendship_and_live_agreement_reads_still_refresh(tmp_path):
    async def run():
        io = ServiceIO()
        memory = CivilizationState(tmp_path / "private", "Rome")
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = AgreementHost()
        try:
            job = await gateway.open("chat", "communication", "g1", host, {})
            await gateway.call(job, "agreement_read", {"agreement": "a1"}, "read")
            memory.ingest("g1", [ended_event()], 1)
            result = await gateway.call(
                job,
                "friendship_offer",
                {
                    "partner": "Greece",
                    "intent": {
                        "reason": "Protect ongoing trade",
                        "assumptions": [{"text": "Agreement remains active", "source": "observation:agreement/a1"}],
                    },
                },
                "stale-action",
            )
            assert "error" not in result
            assert not memory.pending()
            await gateway.call(job, "memory_read", {"ids": ["observation:agreement/a1"]}, "stale-cache")
            stale_source = await gateway.call(
                job,
                "memory_update",
                {
                    "changes": [
                        {
                            "op": "create",
                            "content": plan(
                                assumptions=[{"text": "Income remains available", "source": "observation:agreement/a1"}]
                            ),
                        }
                    ]
                },
                "unrefreshed-source",
            )
            assert stale_source["changes"][0]["accepted"]
            host.agreement.update(version=2, status="ENDED", receive=[], remainingTurns=0)
            refreshed = await gateway.call(job, "agreement_read", {"agreement": "a1"}, "refresh")
            assert refreshed["agreement"]["status"] == "ENDED"
            assert not current(memory, "observation:agreement/a1").get("requiresRefresh")
        finally:
            await io.close()

    asyncio.run(run())


def test_agreement_event_and_plan_flags_follow_checkpoint_rollback(tmp_path):
    memory = setup(tmp_path)
    key = create(memory, plan(subjects=["Greece"]))
    saved = tmp_path / "before-ending.json"
    checksum = memory.backup(saved)
    memory.ingest("g1", [ended_event(reason="DISCARDED_FUTURE")], 1)
    assert current(memory, key)["needsReview"]
    memory.restore(saved, checksum, "g2")
    memory.pause(False)
    memory.start_job("restored", "gameplay", {}, game_turn=4)
    assert not current(memory, key)["needsReview"]
    assert not memory.snapshot("communication")["events"]
    assert not memory.search_memory("restored", {"history": True, "state": "all", "query": "DISCARDED_FUTURE"})[
        "records"
    ]
    memory.ingest("g2", [ended_event()], 1)
    assert current(memory, key)["needsReview"]
    assert memory.snapshot("communication")["events"] == [ended_event()]


def test_agreement_state_export_is_player_private(tmp_path):
    rome = setup(tmp_path / "rome")
    france = CivilizationState(tmp_path / "france", "France")
    france.bind("g1")
    france.start_job("game", "gameplay", {})
    rome.observe("communication", {"agreements": [agreement()]}, "game")
    rome.ingest("g1", [ended_event()], 1)
    rome.export(tmp_path / "rome-workspace")
    france.export(tmp_path / "france-workspace")
    ledger = json.loads((tmp_path / "rome-workspace/communication/ledger.json").read_text())
    assert ledger["agreements"] == [agreement()]
    assert "commitment ledger" in (tmp_path / "rome-workspace/relationships/Greece.md").read_text()
    assert "a1" not in json.dumps(france.snapshot("communication"))
    assert not (tmp_path / "france-workspace/communication/ledger.json").exists()
