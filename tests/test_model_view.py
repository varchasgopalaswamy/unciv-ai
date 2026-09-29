import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from civ_agent.civilization_state import CivilizationState
from civ_agent.memory_records import public_record
from civ_agent.model_view import model_view
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.protocol import JsonObject
from civ_agent.service_io import ServiceIO
from civ_agent.startup_context import prepare_startup, startup_prompt
from civ_agent.state_updates import apply_overview_update
from civ_agent.turn_archive import TurnArchive
from tests.test_agent_service import StartupHost


def content(kind, paragraphs, gameplay=None) -> JsonObject:
    return {
        "kind": kind,
        "title": "Pottery" if kind == "TechResearched" else "Leader",
        "paragraphs": paragraphs,
        "gameplayParagraphs": gameplay,
        "quote": "FLAVOR_QUOTATION",
        "acknowledgement": "FLAVOR_BUTTON",
        "additionalAcknowledgements": ["FLAVOR_SECOND_BUTTON"],
    }


def alert(kind="TechResearched") -> JsonObject:
    return {
        "handle": "alert1",
        "kind": kind,
        "subject": "Pottery",
        "canAcknowledge": True,
        "content": content(kind, ["FLAVOR_PEDIA\nGranary enabled"], ["Granary enabled"]),
    }


def relation() -> JsonObject:
    return {
        "civilizationId": "Egypt",
        "civilizationName": "Egypt",
        "status": "Peace",
        "native": {
            "builtInAI": True,
            "supportsChat": False,
            "demands": [
                {
                    "name": "DoNotSettleNearUs",
                    "text": "FLAVOR_DEMAND: Please don't settle new cities near us.",
                    "action": {"available": True, "unavailableReasons": []},
                }
            ],
            "promises": [],
            "opinionModifiers": [{"name": "YearsOfPeace", "text": "Years of peace", "amount": 3}],
        },
    }


def test_controller_metadata_is_omitted_but_trade_terms_and_player_words_survive():
    terms = [
        {
            "from": "Rome",
            "to": "Greece",
            "item": {"type": "Luxury_Resource", "name": "Silver", "amount": 1, "duration": 25},
        }
    ]
    trade = {
        "proposal": "d1",
        "version": 2,
        "author": "Rome",
        "partner": "Greece",
        "status": "PENDING",
        "clauses": terms,
        "mode": "SHARED",
        "deadlineMillis": 99999,
        "counterofferLimit": 3,
        "counteroffersRemaining": 2,
        "nativeRequestId": None,
        "pausesGameplay": True,
        "awaitingNativeTurn": False,
        "acceptAvailable": True,
        "counterofferAvailable": True,
    }
    raw = {
        "trade": trade,
        "instruction": "The service waits while communication jobs handle replies.",
        "options": {
            "partner": "Greece",
            "give": [terms[0]["item"]],
            "receive": [],
            "canInitiate": True,
            "mode": "SHARED",
            "initiationRule": "Publishing pauses all gameplay",
            "negotiation": {"active": False},
        },
        "opportunity": {
            "civilizationId": "Greece",
            "missingLuxuriesOffered": [],
            "spareLuxuriesToOffer": [],
            "mode": "SHARED",
        },
        "overview": {"turn": 5, "canEndTurn": True, "negotiation": {"active": False}},
        "chat": {"offers": [], "consents": [], "trades": [trade], "negotiation": {"active": True}},
        "event": {"id": 1, "kind": "trade", "body": trade, "wake": True},
        "capabilities": {
            "queries": ["player_turn"],
            "commands": ["END_TURN"],
            "durableRecovery": True,
            "protocolVersion": 1,
            "completeGameplay": False,
        },
        "message": {"author": "Greece", "text": "The deadline is turn 25; please pause expansion."},
        "promise": {"terms": "Wait for my reply", "expiresTurn": 25, "conditions": "While at peace"},
        "unknown": {"mode": "peaceful", "deadlineMillis": 10, "wake": "at dawn"},
    }
    before = copy.deepcopy(raw)
    view = model_view(raw)
    assert raw == before and model_view(view) == view
    expected: JsonObject = {
        k: trade[k]
        for k in (
            "proposal",
            "version",
            "author",
            "partner",
            "status",
            "clauses",
            "acceptAvailable",
            "counterofferAvailable",
        )
    }
    expected["inspection"] = {"tool": "trade_read", "arguments": {"proposal": "d1"}}
    expected["responses"] = [{"tool": "trade_decide", "arguments": {"proposal": "d1", "decision": "accept"}}]
    assert view["trade"] == view["chat"]["trades"][0] == view["event"]["body"] == expected
    assert "instruction" not in view and "wake" not in view["event"]
    assert "mode" not in view["options"] and "initiationRule" not in view["options"]
    assert (
        "negotiation" not in view["options"]
        and "negotiation" not in view["overview"]
        and "negotiation" not in view["chat"]
    )
    assert "mode" not in view["opportunity"]
    assert view["capabilities"] == {"queries": ["player_turn"], "commands": ["END_TURN"]}
    assert view["options"]["canInitiate"] and view["options"]["give"][0]["duration"] == 25
    for key in ("message", "promise", "unknown"):
        assert view[key] == raw[key]


def test_trade_templates_require_pending_terms_and_explicit_availability():
    summary = {"proposal": "dabc12", "author": "Egypt", "partner": "Rome", "status": "PENDING"}
    assert model_view(summary)["inspection"] == {"tool": "trade_read", "arguments": {"proposal": "dabc12"}}
    assert "responses" not in model_view(summary)
    terms = {**summary, "acceptAvailable": False, "declineAvailable": True, "withdrawAvailable": False}
    projected = model_view(terms)
    assert projected["responses"] == [
        {"tool": "trade_decide", "arguments": {"proposal": "dabc12", "decision": "decline"}}
    ]
    assert model_view(projected) == projected
    # Re-projecting a closed or restricted response removes stale templates.
    assert "responses" not in model_view({**projected, "declineAvailable": False})
    closed = model_view({**projected, "status": "DECLINED"})
    assert "inspection" not in closed and "responses" not in closed
    own_offer = model_view({**summary, "withdrawAvailable": True})
    assert own_offer["responses"] == [
        {"tool": "trade_decide", "arguments": {"proposal": "dabc12", "decision": "withdraw"}}
    ]


def test_saved_trade_evidence_and_memory_keep_game_dates_without_job_metadata():
    raw = {
        "category": "note",
        "body": {
            "text": "Negotiate Sugar for Silver",
            "updatedTurn": 62,
            "basis": {"job": "job-private", "purpose": "gameplay", "turn": 62},
        },
    }
    record = public_record("mem1", raw)
    assert record == {"id": "mem1", "type": "note", "text": "Negotiate Sugar for Silver", "updatedTurn": 62}
    assert raw["body"]["basis"]["job"] == "job-private"
    assert model_view({**record, "basis": raw["body"]["basis"]}) == record
    contribution = {"id": "mem2", "type": "contribution", "body": raw["body"]}
    assert "basis" not in model_view(contribution)["body"]
    assert contribution["body"]["basis"]["job"] == "job-private"
    expired = {
        "proposal": "d1",
        "author": "Rome",
        "partner": "Greece",
        "status": "EXPIRED",
        "deadlineMillis": 123,
        "failure": "The negotiation deadline elapsed.",
        "unavailableReason": "The negotiation deadline elapsed.",
        "acceptAvailable": False,
    }
    envelope = {"structuredContent": expired, "content": [{"type": "text", "text": json.dumps(expired)}]}
    view = model_view(envelope)
    assert view["structuredContent"]["status"] == "EXPIRED"
    assert not view["structuredContent"]["acceptAvailable"]
    assert view["structuredContent"]["failure"] == "This proposal expired."
    assert json.loads(view["content"][0]["text"]) == view["structuredContent"]
    assert "deadlineMillis" not in json.dumps(view)


def test_projection_preserves_gameplay_and_player_wording_without_mutating_audit_data():
    raw = {
        "alerts": [alert()],
        "diplomacy": {"knownCivilizations": [relation()]},
        "notifications": [{"text": "A unit was destroyed"}],
        "capture": {"paragraphs": ["Razing destroys the city"], "options": [{"name": "Raze"}]},
        "quest": {"description": "Build a road to us", "remainingTurns": 15},
        "construction": {"description": "Maintenance 2; yields +3 science", "confirmation": "Spend 200 gold"},
        "message": {"author": "Greece", "text": "Please don't settle near us. By near, I mean the river valley."},
        "socialAdvisories": [{"terms": "Stay away from the river", "conditions": "While we are friends"}],
        "unknown": {"text": "Keep this", "description": "Keep this too", "quote": "Player quotation"},
    }
    before = copy.deepcopy(raw)
    view = model_view(raw)
    assert raw == before
    assert model_view(view) == view
    assert "FLAVOR_" not in json.dumps(view)
    assert view["alerts"][0]["content"]["paragraphs"] == ["Granary enabled"]
    native = view["diplomacy"]["knownCivilizations"][0]["native"]
    assert native["availableDemandsToSend"] == [
        {"type": "DoNotSettleNearUs", "recipient": "Egypt", "available": True, "unavailableReasons": []}
    ]
    assert "demands" not in native and native["promises"] == []
    for key in raw.keys() - {"alerts", "diplomacy"}:
        assert view[key] == raw[key]
    assert native["opinionModifiers"] == raw["diplomacy"]["knownCivilizations"][0]["native"]["opinionModifiers"]


@pytest.mark.parametrize(
    "kind,demand,choice,war,incident",
    [
        ("DemandToStopSettlingCitiesNear", "DoNotSettleNearUs", "Refuse", False, None),
        ("DemandToNotAttackUs", "DoNotAttackUs", "Refuse", True, None),
        ("Denounced", None, "DeclareWar", True, None),
        (
            "AttackedProtectedMinor",
            None,
            "WithdrawProtection",
            False,
            {"name": "Kabul", "civilizationId": "Kabul", "unmet": False},
        ),
        (
            "BulliedProtectedMinor",
            None,
            "SideWithCityState",
            False,
            {"name": "Unknown city-state", "civilizationId": None, "unmet": True},
        ),
    ],
)
def test_received_native_messages_keep_identity_choices_and_actual_consequences(kind, demand, choice, war, incident):
    raw = alert(kind)
    raw["content"] = None
    raw["nativeDiplomacy"] = {
        "civilizationId": "Egypt",
        "sender": "Egypt",
        "recipient": "Rome",
        "event": kind,
        "demand": demand,
        "incidentCityState": incident,
        "active": True,
        "content": content(kind, ["FLAVOR_SPEECH"]),
        "options": [
            {
                "name": choice,
                "text": "FLAVOR_RESPONSE",
                "available": True,
                "declaresWar": war,
                "consequences": ["Native consequences"],
                "socialAdvisories": [{"terms": "We promised peace", "conditions": "Unless provoked"}],
            }
        ],
    }
    view = model_view(raw)
    assert "FLAVOR_" not in json.dumps(view)
    native = view["nativeDiplomacy"]
    assert native["sender"] == "Egypt" and native["recipient"] == "Rome"
    assert native["event"] == kind and native["demand"] == demand and native["incidentCityState"] == incident
    assert native["options"][0] == {k: v for k, v in raw["nativeDiplomacy"]["options"][0].items() if k != "text"}


def test_all_audited_dialogs_and_research_details_omit_flavor_but_keep_facts():
    first = alert("FirstContact")
    first["content"] = content("FirstContact", ["FLAVOR_GREETING"], [])
    first["firstContact"] = {
        "civilizationId": "Egypt",
        "leaderName": "Ramesses",
        "message": "FLAVOR_GREETING",
        "acknowledgement": "FLAVOR_BUTTON",
    }
    friendship = alert("DeclarationOfFriendship")
    friendship["content"] = None
    friendship["friendship"] = {
        "civilizationId": "Egypt",
        "content": content(None, ["FLAVOR_FRIENDSHIP"]),
        "duration": 30,
        "accept": {"available": True},
        "canDecline": True,
    }
    native = relation()
    native["native"]["promises"] = [
        {
            "kind": "DoNotSettleNearUs",
            "promiser": "Rome",
            "promisee": "Egypt",
            "turnsRemaining": 27,
            "text": "FLAVOR_PROMISE",
        }
    ]
    trade = {
        "civilizationId": "Egypt",
        "leaderName": "Ramesses",
        "message": "FLAVOR_INVITATION",
        "give": [],
        "receive": [{"type": "Gold", "amount": 10}],
        "proposal": "p1",
    }
    raw = {
        "alerts": [first, friendship],
        "research": {
            "name": "Future Tech",
            "researchNow": {"available": True},
            "freeChoice": {},
            "description": "FLAVOR_PEDIA\n+Score",
            "gameplayDescription": "+Score",
        },
        "result": {
            "outcome": "Victory",
            "winningCivilizationId": "Rome",
            "victoryType": "Science",
            "victoryTurn": 300,
            "paragraphs": ["FLAVOR_VICTORY"],
        },
        "messages": [
            content(kind, ["FLAVOR_NARRATIVE"], [])
            for kind in ["StartIntro", "GoldenAge", "WarDeclaration", "Defeated", "GameHasBeenWon"]
        ],
        "diplomacy": {"knownCivilizations": [native], "incomingRequests": [trade], "outgoingRequests": [trade]},
    }
    shown = model_view(raw)
    assert "FLAVOR_" not in json.dumps(shown)
    assert shown["research"]["description"] == "+Score"
    assert shown["alerts"][1]["friendship"]["duration"] == 30
    assert shown["diplomacy"]["knownCivilizations"][0]["native"]["promises"][0]["promiser"] == "Rome"
    assert shown["diplomacy"]["incomingRequests"][0]["receive"] == trade["receive"]
    assert shown["result"]["victoryTurn"] == 300
    assert model_view(shown) == shown


def test_unknown_popup_effects_are_not_silently_lost():
    unknown = alert("NewEvent")
    unknown["content"] = content(None, ["Important unclassified effect"])
    assert model_view(unknown)["content"]["paragraphs"] == ["Important unclassified effect"]
    assert model_view(unknown)["content"]["acknowledgement"] == "FLAVOR_BUTTON"


def test_quest_narrative_is_replaced_only_when_structured_objectives_are_present():
    raw = {
        "name": "Give Gold",
        "description": "We will collapse without gold!",
        "influenceReward": 20,
        "remainingTurns": 10,
        "targetCivilization": {"civilizationId": None, "name": "Unknown civilization", "unmet": True},
        "objective": {"kind": "Give Gold", "issuer": "Geneva", "targetName": None},
    }
    shown = model_view(raw)
    assert "description" not in shown and "targetCivilization" not in shown
    assert shown["recentBully"] == raw["targetCivilization"]
    assert shown["objective"]["issuer"] == "Geneva" and shown["remainingTurns"] == 10
    assert shown["influenceReward"] == 20
    invest = {
        **raw,
        "name": "Invest",
        "objective": {"kind": "Invest", "issuer": "Geneva", "goldGiftInfluenceBonusPercent": 50},
    }
    assert model_view(invest)["objective"]["goldGiftInfluenceBonusPercent"] == 50
    assert model_view({**raw, "objective": None})["description"] == raw["description"]
    assert model_view(shown) == shown


def test_startup_deltas_details_receipts_replays_and_historical_evidence_share_projection(tmp_path):
    async def run():
        io = ServiceIO()
        memory = CivilizationState(tmp_path / "memory", "Rome")
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = StartupHost(
            SimpleNamespace(service=SimpleNamespace(active_id="job", records={"job": {"epoch": "g1", "gameTurn": 0}}))
        )
        host.view["alerts"] = [alert()]
        host.view["diplomacy"] = {"knownCivilizations": [relation()]}
        original = host.tool

        async def tool(name, args):
            result = await original(name, args)
            if name == "player_command":
                result["acknowledgedAlert"] = copy.deepcopy(host.view["alerts"][0])
                host.view["alerts"] = []
                host.view["diplomacy"]["knownCivilizations"][0]["native"]["demands"][0]["action"] = {
                    "available": False,
                    "unavailableReasons": ["Already pending"],
                }
            return result

        host.tool = tool
        try:
            job = await gateway.open("job", "gameplay", "g1", host, {}, 0)
            prepared = await prepare_startup(gateway, job)
            view = prepared["reads"]["player_turn"]
            assert "FLAVOR_" not in startup_prompt(prepared, "gameplay", memory.context(job.memory)).user
            detail = await gateway.call(job, "player_details", {"section": "diplomacy"}, "details")
            assert "demands" not in detail["data"]["diplomacy"]["knownCivilizations"][0]["native"]
            # Raw evidence is retained, while explicit reads and future preload project it.
            source = "observation:turn"
            evidence = await gateway.call(job, "memory_read", {"ids": [source]}, "evidence")
            assert "FLAVOR_" not in json.dumps(evidence)
            record = job.memory["records"][source]
            assert "FLAVOR_QUOTATION" in json.dumps(record)
            context: JsonObject = {"records": {source: record}, "contextIds": [source], "omitted": 0, "events": []}
            assert "FLAVOR_" not in memory.context(context)
            assert (
                public_record("mem1", {"category": "note", "body": {"text": "FLAVOR_QUOTATION is what Egypt said"}})[
                    "text"
                ]
                == "FLAVOR_QUOTATION is what Egypt said"
            )
            command = {"kind": "ACKNOWLEDGE_ALERT", "target": "alert1"}
            receipt = await gateway.call(job, "player_command", command, "ack")
            assert "FLAVOR_" not in json.dumps(receipt)
            current = apply_overview_update(view, receipt["stateUpdate"])
            assert current == job.delivered_overview and current["alerts"] == []
            assert not current["diplomacy"][0]["native"]["availableDemandsToSend"][0]["available"]
            assert await gateway.call(job, "player_command", command, "ack") == receipt
            history = await gateway.call(job, "history_read", {"turn": 0}, "history")
            assert "FLAVOR_" not in json.dumps(history)
            assert "FLAVOR_QUOTATION" in json.dumps(memory.history(0))
            memory.export(tmp_path / "workspace")
            assert "FLAVOR_" not in (tmp_path / "workspace/turn_0/summary.json").read_text()
            assert "FLAVOR_" not in (tmp_path / "workspace/turn_0/receipts.jsonl").read_text()
            # Cached audit responses also pass through the model boundary.
            memory.call(job.id, "cached", "player_turn", {}, {"alerts": [alert()]})
            assert "FLAVOR_" not in json.dumps(await gateway.call(job, "player_turn", {}, "cached"))
            assert len(host.executed) == 1
        finally:
            await io.close()

    asyncio.run(run())


def test_workspace_archives_project_native_data_and_keep_the_original_audit(tmp_path):
    raw = {"alerts": [alert()], "diplomacy": [relation()]}
    reply = {"structuredContent": raw, "content": [{"type": "text", "text": json.dumps(raw)}]}
    before = copy.deepcopy(reply)
    event = {
        "sequence": 0,
        "time": "2026-09-21T20:00:00Z",
        "kind": "tool_result",
        "data": {"itemId": "read", "type": "mcpToolCall", "status": "completed", "result": reply},
    }
    archive = TurnArchive(tmp_path / "archive", tmp_path / "workspace", "Rome", "g1")
    archive.update(0, "job", [event], phase="COMPLETED", handoff=1)
    assert reply == before
    for name in ("commands.jsonl", "attempts/attempt_1/events.jsonl"):
        assert "FLAVOR_" in (tmp_path / "archive/turn_0" / name).read_text()
        assert "FLAVOR_" not in (tmp_path / "workspace/turn_0" / name).read_text()
    projected = model_view(reply)
    assert json.loads(projected["content"][0]["text"]) == projected["structuredContent"]
    assert model_view(projected) == projected
    unmatched = {**reply, "content": [{"type": "text", "text": "A player's own quotation: FLAVOR_QUOTATION"}]}
    assert model_view(unmatched)["content"] == unmatched["content"]
