"""Private audiences stay exact across validation, dispatch, and concurrent jobs."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from civ_agent import cli
from civ_agent.commands import chat
from civ_agent.tool_schemas import tool_model
from tests.test_diplomacy_contracts import opened


@pytest.mark.parametrize("purpose", ["communication", "gameplay"])
@pytest.mark.parametrize(
    "fields",
    [
        {"recipients": []},
        {"recipients": None},
        {"recipients": "Greece"},
        {"recipients": ["Greece", "Greece"]},
        {"recipients": [""]},
        {"recipients": [" "]},
        {"recipients": [4]},
        {"recipients": ["Greece"], "to": "France"},
        {"recipients": ["Greece"], "nativeAction": {"kind": "DENOUNCE", "target": "Greece"}},
        {"recipients": ["Greece"], "nativeAction": None},
    ],
)
def test_group_schema_rejects_invalid_addressing(purpose, fields):
    with pytest.raises(ValidationError):
        tool_model("chat_send", purpose).model_validate({"text": "Hello", **fields})


@pytest.mark.parametrize(
    "fields",
    [
        {"recipients": ["Rome"]},
        {"recipients": ["Greece", "Egypt"]},
        {"recipients": ["Unknown"]},
        {"recipients": ["Greece"], "mentions": ["France"]},
        {"recipients": []},
        {"recipients": ["Greece", "Greece"]},
        {"recipients": ["Greece"], "to": "France"},
        {"recipients": ["Greece"], "nativeAction": {"kind": "DENOUNCE", "target": "Greece"}},
    ],
)
def test_invalid_group_is_never_dispatched(tmp_path, fields):
    async def run():
        async with opened(tmp_path, "gameplay") as (gateway, job, host):
            result = await gateway.call(job, "chat_send", {"text": "Hello", **fields}, "invalid")
            assert result["error"] == "INVALID_TOOL_ARGUMENTS" and result["applied"] is False
            assert not host.writes

    asyncio.run(run())


@pytest.mark.parametrize(
    "address", [{}, {"to": "Greece"}, {"recipients": ["Greece"]}, {"recipients": ["France", "Greece"]}]
)
def test_group_and_legacy_requests_forward_exactly_and_replay(tmp_path, address):
    async def run():
        async with opened(tmp_path) as (gateway, job, host):
            fields = {"text": "Hello", "mentions": ["Greece"], **address}
            result = await gateway.call(job, "chat_send", fields, "send")
            assert "error" not in result
            assert {k: v for k, v in host.writes[-1].items() if k not in {"op", "epoch", "requestId"}} == fields
            assert result["message"]["public"] is (not address)
            assert await gateway.call(job, "chat_send", fields, "send") == result
            assert len(host.writes) == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    ("first_address", "second_address", "same"),
    [
        ({"recipients": ["Greece", "France"]}, {"recipients": ["France", "Greece"]}, True),
        ({"to": "Greece"}, {"recipients": ["Greece"]}, True),
        ({"recipients": ["Greece"]}, {"to": "Greece"}, True),
        ({"recipients": ["Greece", "France"]}, {"to": "Greece"}, False),
        ({"to": "Greece"}, {"recipients": ["France", "Greece"]}, False),
        ({"recipients": ["Greece", "France"]}, {}, False),
        ({}, {"recipients": ["Greece", "France"]}, False),
        ({"recipients": ["Greece", "France"]}, {"recipients": ["Greece", "China"]}, False),
    ],
)
def test_concurrent_send_matches_exact_participants(tmp_path, first_address, second_address, same):
    async def run():
        async with opened(tmp_path) as (gateway, first, host):
            second = await gateway.open("second", "gameplay", "g1", host, {}, 0)
            # Include another eligible player for overlapping groups.
            for job in (first, second):
                await gateway.call(job, "chat_read", {}, "read")
                assert job.social is not None
                job.social["chatRecipients"].append("China")
            sent = await gateway.call(first, "chat_send", {"text": "First", **first_address}, "send")
            result = await gateway.call(second, "chat_send", {"text": "Second", **second_address}, "send")
            if same:
                assert result["error"] == "CONVERSATION_UPDATED"
                assert result["messages"] == [sent["message"]] and len(host.writes) == 1
            else:
                assert "error" not in result and len(host.writes) == 2

    asyncio.run(run())


@pytest.mark.parametrize("targets", [[], ["Greece"], ["Greece", "France"]])
def test_cli_repeated_recipients_forward_to_host(tmp_path, monkeypatch, targets):
    host = AsyncMock()
    host.tool.side_effect = [{"epoch": "g1"}, {"message": {}}]
    host.__aenter__.return_value = host
    monkeypatch.setattr(chat, "HostConnection", lambda *args: host)
    monkeypatch.setattr(chat, "player_credentials", lambda *args: ("Rome", "credential"))
    monkeypatch.setattr(chat, "endpoint_configuration", lambda *args: ("https://localhost", "ca"))
    flags = [value for target in targets for value in ("--to", target)]
    assert cli.main(["chat", "send", "Rome", "--state-dir", str(tmp_path), "--text", "Hello", *flags]) == 0
    sent = host.tool.call_args.args[1]
    assert sent["op"] == "send" and sent["text"] == "Hello"
    assert {k: sent[k] for k in ("to", "recipients") if k in sent} == (
        {"recipients": targets} if len(targets) > 1 else {"to": targets[0]} if targets else {}
    )


def test_cli_group_native_action_fails_before_prompt_or_send(tmp_path, monkeypatch):
    host = AsyncMock()
    host.tool.return_value = {"epoch": "g1"}
    host.__aenter__.return_value = host
    monkeypatch.setattr(chat, "HostConnection", lambda *args: host)
    monkeypatch.setattr(chat, "player_credentials", lambda *args: ("Rome", "credential"))
    monkeypatch.setattr(chat, "endpoint_configuration", lambda *args: ("https://localhost", "ca"))
    assert (
        cli.main(
            [
                "chat",
                "send",
                "Rome",
                "--state-dir",
                str(tmp_path),
                "--text",
                "Hello",
                "--to",
                "Greece",
                "--to",
                "France",
                "--native-action",
                "DENOUNCE",
                "--target",
                "Greece",
            ]
        )
        == 2
    )
    host.tool.assert_awaited_once_with("player_turn", {})
