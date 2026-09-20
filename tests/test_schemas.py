import asyncio

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from civ_agent.civilization_state import CivilizationState
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.schemas import StartRequest, start_error, validate
from civ_agent.service_io import ServiceIO
from civ_agent.tool_schemas import TOOL_MODELS
from tests.test_proposal_gateway import Host


@pytest.mark.parametrize(
    "tool,arguments",
    [
        (
            "trade_offer",
            {
                "partner": "Greece",
                "give": [{"type": "Gold", "name": "Gold", "amount": True, "duration": 0}],
                "receive": [],
            },
        ),
        ("trade_offer", {"partner": "Greece", "give": [], "receive": [], "proposal": None}),
        ("trade_decide", {"proposal": "d1", "decision": "accept", "version": 3}),
        ("chat_send", {"text": "hello", "op": "trade_decide"}),
        ("history_read", {"turn": "1"}),
        ("memory_update", {"changes": [{"key": "SOUL", "category": "observation", "body": {"text": "invented"}}]}),
        ("memory_update", {"changes": [{"key": "SOUL", "category": "policy", "body": {}, "hidden": "data"}]}),
    ],
)
def test_advertised_schema_and_runtime_reject_invalid_input_before_mutation(tmp_path, tool, arguments):
    if tool in {"trade_offer", "trade_decide"}:
        arguments = {**arguments, "intent": {"reason": "Validate input"}}

    async def run():
        io = ServiceIO()
        memory = CivilizationState(tmp_path / "private", "Rome")
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = Host()
        try:
            job = await gateway.open("test", "gameplay", "g1", host, {})
            schema = next(item["inputSchema"] for item in await gateway.tools(job) if item["name"] == tool)
            assert not Draft202012Validator(schema).is_valid(arguments)
            if tool == "memory_update":
                result = await gateway.call(job, tool, arguments, "one")
                assert result["error"] == "INVALID_ARGUMENT" and not result["applied"]
            else:
                with pytest.raises(ValueError, match="^INVALID_ARGUMENT$"):
                    await gateway.call(job, tool, arguments, "one")
            assert host.executed == []
            assert memory.pending() == []
        finally:
            await io.close()

    asyncio.run(run())


def start_payload():
    return {
        "activationId": "a1",
        "epoch": "g1",
        "civilization": "Rome",
        "gameTurn": 0,
        "handoff": 0,
        "threadId": None,
        "mcpUrl": "https://localhost:8765/mcp",
        "playerToken": "P" * 43,
        "prompt": "Private player strategy",
        "model": None,
        "timeoutSeconds": 30,
        "leaseSeconds": 10,
    }


@pytest.mark.parametrize(
    "change,code",
    [
        ({"gameTurn": True}, "INVALID_GAME_TURN"),
        ({"handoff": 1.5}, "INVALID_GAME_TURN"),
        ({"timeoutSeconds": "30"}, "INVALID_TIMEOUT"),
        ({"model": "a private invalid setting"}, "INVALID_MODEL"),
        ({"playerToken": "PRIVATE-CREDENTIAL"}, "INVALID_PLAYER_TOKEN"),
        ({"PRIVATE-UNKNOWN-FIELD": "SECRET"}, "INVALID_ARGUMENT"),
    ],
)
def test_service_validation_keeps_useful_codes_without_inputs(change, code):
    with pytest.raises(ValidationError) as caught:
        StartRequest.model_validate({**start_payload(), **change})
    assert start_error(caught.value) == code


def test_models_do_not_echo_private_inputs_and_optional_tool_fields_stay_omitted():
    payload = start_payload()
    model = validate(StartRequest, payload)
    assert payload["playerToken"] not in repr(model)
    assert payload["prompt"] not in repr(model)
    with pytest.raises(ValidationError) as caught:
        StartRequest.model_validate({**payload, "model": "PRIVATE INVALID SETTING"})
    assert "PRIVATE INVALID SETTING" not in str(caught.value)
    for name, arguments in (
        ("chat_history", {"with": "Greece"}),
        ("trade_offer", {"partner": "Greece", "give": [], "receive": [], "intent": {"reason": "Offer terms"}}),
    ):
        assert validate(TOOL_MODELS[name], arguments).model_dump(by_alias=True, exclude_unset=True) == arguments


def test_war_and_mediated_peace_terms_use_trade_but_introductions_remain_excluded():
    terms = {
        "partner": "Greece",
        "give": [],
        "receive": [{"type": "WarDeclaration", "name": "France", "amount": 1, "duration": -1}],
        "intent": {"reason": "Recruit Greece into the war"},
    }
    schema = TOOL_MODELS["trade_offer"].model_json_schema()
    assert Draft202012Validator(schema).is_valid(terms)
    assert validate(TOOL_MODELS["trade_offer"], terms).model_dump(exclude_unset=True) == terms
    terms["receive"][0]["type"] = "PeaceProposal"
    assert Draft202012Validator(schema).is_valid(terms)
    for kind in ("Introduction",):
        terms["receive"][0]["type"] = kind
        assert not Draft202012Validator(schema).is_valid(terms)
        with pytest.raises(ValueError, match="INVALID_ARGUMENT"):
            validate(TOOL_MODELS["trade_offer"], terms)
