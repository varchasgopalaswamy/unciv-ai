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
from tests.domain_fixtures import StartupHost


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


def captured_settler() -> JsonObject:
    return {
        "handle": "alert9",
        "kind": "RecapturedCivilian",
        "canAcknowledge": False,
        "capture": {
            "title": "Return Settler to France?",
            "paragraphs": [
                "The Settler we liberated originally belonged to France. They will be grateful if we return it to them."
            ],
            "options": [
                {
                    "name": choice,
                    "label": label,
                    "paragraphs": [effect],
                    "available": True,
                    "unavailableReasons": [],
                    "civilianOutcome": {"recipient": recipient, "unitType": unit_type},
                    "command": {"kind": "RESOLVE_CAPTURED_CITY", "target": "alert9", "selection": choice},
                }
                for choice, label, recipient, unit_type, effect in [
                    ("ReturnCivilian", "Yes", "France", "Settler", "France receives a Settler."),
                    ("KeepCivilian", "No", "Greece", "Worker", "The captured Settler becomes a Worker."),
                ]
            ],
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
            "missingLuxuriesAvailableForTrade": [{"name": "Silk", "amount": 1}],
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
    expected["proposalId"] = expected.pop("proposal")
    expected["inspection"] = {"tool": "trade_read", "arguments": {"proposalId": "d1"}}
    expected["responses"] = [{"tool": "trade_decide", "arguments": {"proposalId": "d1", "decision": "accept"}}]
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
    assert view["opportunity"]["missingLuxuriesAvailableForTrade"] == [{"name": "Silk", "amount": 1}]
    assert "responses" not in view["opportunity"] and "missingLuxuriesOffered" not in view["opportunity"]
    for key in ("message", "promise", "unknown"):
        assert view[key] == raw[key]


def test_trade_templates_require_pending_terms_and_explicit_availability():
    summary = {"proposal": "dabc12", "author": "Egypt", "partner": "Rome", "status": "PENDING"}
    assert model_view(summary)["inspection"] == {"tool": "trade_read", "arguments": {"proposalId": "dabc12"}}
    assert "responses" not in model_view(summary)
    terms = {**summary, "acceptAvailable": False, "declineAvailable": True, "withdrawAvailable": False}
    projected = model_view(terms)
    assert projected["responses"] == [
        {"tool": "trade_decide", "arguments": {"proposalId": "dabc12", "decision": "decline"}}
    ]
    assert model_view(projected) == projected
    # Re-projecting a closed or restricted response removes stale templates.
    assert "responses" not in model_view({**projected, "declineAvailable": False})
    closed = model_view({**projected, "status": "DECLINED"})
    assert "inspection" not in closed and "responses" not in closed
    own_offer = model_view({**summary, "withdrawAvailable": True})
    assert own_offer["responses"] == [
        {"tool": "trade_decide", "arguments": {"proposalId": "dabc12", "decision": "withdraw"}}
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
    assert native["opinionModifiers"] == [{"name": "YearsOfPeace", "amount": 3}]


def test_capture_choice_keeps_consequences_without_promising_another_players_gratitude():
    raw = captured_settler()
    before = copy.deepcopy(raw)
    view = model_view(raw)
    assert raw == before and model_view(view) == view
    assert "grateful" not in json.dumps(view)
    assert view["capture"]["title"] == "Return Settler to France?"
    assert view["capture"]["options"] == raw["capture"]["options"]
    player_words = {
        "author": "France",
        "text": "We will be grateful if you return it. I offer 50 gold.",
        "terms": "Return the Settler for 50 gold",
    }
    assert model_view(player_words) == player_words


@pytest.mark.parametrize("kind", ["CityConquered", "CityTraded"])
def test_city_liberation_drops_evaluative_wording_without_losing_control_costs(kind):
    options = [
        {
            "name": "Liberate",
            "label": "Liberate (city returns to France)",
            "paragraphs": [
                "Liberating a city returns it to its original owner, giving you a massive relationship boost with them!"
            ],
            "available": False,
            "unavailableReasons": ["At war with the original owner"],
        },
        {
            "name": "Puppet",
            "paragraphs": ["You cannot control production. -25% Science and Culture."],
            "available": True,
        },
        {"name": "Raze", "paragraphs": ["Population decreases until the city is destroyed."], "available": True},
    ]
    raw = {
        "handle": "alert2",
        "kind": kind,
        "canAcknowledge": False,
        "capture": {"title": "Choose a disposition for Paris", "paragraphs": [], "options": options},
    }
    shown = model_view(raw)["capture"]["options"]
    assert "massive" not in json.dumps(shown)
    assert "native diplomatic opinion" in shown[0]["paragraphs"][0]
    for key in ("label", "available", "unavailableReasons"):
        assert shown[0][key] == options[0][key]
    assert shown[1:] == options[1:]
    assert model_view({"handle": "city1", "captureOptions": options})["captureOptions"] == shown


def test_ai_opinion_keeps_the_modifier_and_amount_without_roleplay():
    raw = relation()
    raw["native"]["opinionModifiers"] = [
        {"name": "ReturnedCapturedUnits", "text": "You returned captured units to us", "amount": 20},
        {
            "name": "BetrayedDeclarationOfFriendship",
            "text": "Your so-called 'friendship' is worth nothing.",
            "amount": -40,
        },
    ]
    view = model_view(raw)
    assert view["native"]["opinionModifiers"] == [
        {"name": "ReturnedCapturedUnits", "amount": 20},
        {"name": "BetrayedDeclarationOfFriendship", "amount": -40},
    ]


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
            "title": "FLAVOR_RESULT_TITLE",
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


@pytest.mark.parametrize(
    "name,requirement",
    [
        ("Route", "Connect your capital to the issuer's capital with a city connection (normally a road)."),
        ("Clear Barbarian Camp", "Clear the camp at location yourself."),
        ("Connect Resource", "Acquire targetName in your resource supply, by improvement or trade."),
        ("Construct Wonder", "Have targetName built in your cities before a rival builds it."),
        ("Acquire Great Person", "Acquire targetName or its civilization-specific replacement."),
        ("Conquer City State", "Conquer and eliminate targetCivilization."),
        ("Find Player", "Discover targetCivilization's territory; contact alone is insufficient."),
        ("Find Natural Wonder", "Discover targetName."),
        ("Give Gold", "Give a gold gift to issuer."),
        ("Pledge to Protect", "Pledge protection to issuer."),
        ("Contest Culture", "Earn most culture before expiry; tied positive scores qualify."),
        ("Contest Faith", "Earn most faith before expiry; tied positive scores qualify."),
        ("Contest Technologies", "Research most technologies before expiry; tied positive scores qualify."),
        ("Invest", "Gifts earn extra influence until expiry; no completion action."),
        ("Bully City State", "Demand tribute successfully from targetCivilization."),
        ("Denounce Civilization", "Have a native denunciation of targetCivilization active."),
        ("Spread Religion", "Make targetName the majority religion in issuer's capital."),
    ],
)
def test_quest_mechanics_survive_redaction_for_every_kind(name, requirement):
    objective = {"kind": name, "issuer": "Geneva", "requirement": requirement}
    raw = {"name": name, "influenceReward": 20, "description": "FLAVOR_APPEAL", "objective": objective}
    shown = model_view(raw)
    assert shown["objective"] == objective and "description" not in shown


def test_return_civilian_keeps_native_diplomatic_effect_without_gratitude_narrative():
    raw = captured_settler()
    effect = {
        "civilizationId": "France",
        "stat": "NativeOpinion.ReturnedCapturedUnits",
        "operation": "SET",
        "value": 20,
        "before": 20,
        "after": 20,
    }
    raw["capture"]["options"][0]["diplomaticEffect"] = effect
    shown = model_view(raw)
    assert shown["capture"]["options"][0]["diplomaticEffect"] == effect
    assert "grateful" not in json.dumps(shown)


def test_startup_deltas_details_receipts_replays_and_historical_evidence_share_projection(tmp_path):
    async def run():
        io = ServiceIO()
        memory = CivilizationState(tmp_path / "memory", "Rome")
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = StartupHost(
            SimpleNamespace(service=SimpleNamespace(active_id="job", records={"job": {"epoch": "g1", "gameTurn": 0}}))
        )
        host.view["alerts"] = [alert(), captured_settler()]
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
            assert "grateful" not in json.dumps(view)
            detail = await gateway.call(job, "player_details", {"section": "diplomacy"}, "details")
            assert "demands" not in detail["data"]["diplomacy"]["knownCivilizations"][0]["native"]
            # Raw evidence is retained, while explicit reads and future preload project it.
            source = "observation:turn"
            evidence = await gateway.call(job, "memory_read", {"ids": [source]}, "evidence")
            assert "FLAVOR_" not in json.dumps(evidence)
            assert "grateful" not in json.dumps(evidence)
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
            assert "grateful" not in json.dumps(history)
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


def test_native_history_exports_only_delivered_projection_and_keeps_original_reply(tmp_path):
    from pathlib import Path

    from pydantic_ai import Agent
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai_harness import StepPersistence
    from pydantic_ai_harness.step_persistence import SqliteStepStore

    from civ_agent.orchestration.contracts import Scope, canonical
    from civ_agent.orchestration.history_exports import export_native_history
    from civ_agent.orchestration.store import WorkflowStore
    from civ_agent.tls import write_private
    from tests.test_export_agent_history import layout, source_bytes

    async def run():
        raw = {"alerts": [alert(), captured_settler()], "diplomacy": [relation()]}
        reply = {"structuredContent": raw, "content": [{"type": "text", "text": json.dumps(raw)}]}
        before = copy.deepcopy(reply)
        projected = model_view(reply)
        assert json.loads(projected["content"][0]["text"]) == projected["structuredContent"]
        assert model_view(projected) == projected
        unmatched = {**reply, "content": [{"type": "text", "text": "A player's own quotation: FLAVOR_QUOTATION"}]}
        assert model_view(unmatched)["content"] == unmatched["content"]

        root, _, paths = layout(tmp_path)
        store = WorkflowStore(paths.store, player=paths.player)
        try:
            store.bind_epoch("game", "g1")
        finally:
            store.close()
        database = paths.store / "agent-steps.sqlite3"
        write_private(database, b"")
        steps = SqliteStepStore(database=database)
        scope = Scope(game="game", player="p0", epoch="g1", activation="source-role")
        calls = []

        def model(messages, info):
            calls.append(messages)
            return ModelResponse(
                parts=[ToolCallPart("read_delivered_view", {}, "read")]
                if len(calls) == 1
                else [TextPart("Observed delivered view")]
            )

        agent = Agent(
            FunctionModel(model),
            name="player_role_v1",
            capabilities=[StepPersistence(store=steps, metadata={"scope": canonical(scope.model_dump(mode="json"))})],
        )

        @agent.tool_plain
        def read_delivered_view() -> dict:
            return model_view(reply)

        await agent.run("Read the delivered view")
        original_files = source_bytes(root)
        entries = await export_native_history(root, tmp_path / "export", "Rome")
        assert len(entries) == 1 and len(calls) == 2
        exported = Path(entries[0]["history"]).read_text()
        assert "FLAVOR_" in json.dumps(reply) and reply == before
        assert "FLAVOR_" not in exported and "grateful" not in exported
        assert "Granary enabled" in exported
        assert source_bytes(root) == original_files

    asyncio.run(run())
