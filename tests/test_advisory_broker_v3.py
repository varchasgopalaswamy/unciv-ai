"""Budgeted raw diagnostics and free receipt recovery against a local fake provider."""

import asyncio
import hashlib
import json
from unittest.mock import AsyncMock

import aiohttp

from civ_agent.advisory_answers import failed_answers
from civ_agent.decisions_broker import DecisionsBroker, RoutingClient
from civ_agent.decisions_ledger import DecisionsLedger
from civ_agent.decisions_policy import DecisionsSettings
from civ_agent.decisions_tasks import request_body, request_id, request_metadata, validate_result
from civ_agent.tls import write_private_json
from tests.test_decisions_broker import server


def evidence():
    return {"player": "Rome", "preservation": [{"key": "event:1", "version": 1}, {"key": "event:2", "version": 1}]}


def registered(tmp_path):
    settings = DecisionsSettings(decisions_enabled=True)
    ledger = DecisionsLedger(tmp_path)
    ledger.register("seat", "game", "Rome", settings.model_dump())
    ledger.register("other", "game", "Greece", settings.model_dump())
    write_private_json(
        tmp_path / "decisions-clients.json",
        {
            "seat": hashlib.sha256(b"secret").hexdigest(),
            "other": hashlib.sha256(b"other-secret").hexdigest(),
        },
    )
    write_private_json(tmp_path / "token.json", {"token": "secret"})
    return settings, ledger


def test_broker_persists_all_answers_and_private_inputs_and_lookup_never_dispatches(tmp_path):
    async def run():
        settings, ledger = registered(tmp_path)
        upstream = AsyncMock()
        upstream.evaluate.return_value = {
            "answers": [
                {"name": "preservation:0", "type": "choice", "choice": "consider", "confidence": 0.99},
                {"name": "preservation:1", "type": "choice", "choice": "skip", "confidence": 0.7},
            ],
            "usage": {"input_tokens": 100},
        }
        broker = DecisionsBroker(tmp_path, upstream)
        socket = tmp_path / "broker.sock"
        async with server(broker.application(), socket=socket):
            client = RoutingClient(socket, tmp_path / "token.json")
            supplied = evidence()
            result = await client.batch(supplied)
            assert result["fallback"] == "low_confidence" and len(result["answers"]) == 1
            assert result["rawAnswers"][1]["confidence"] == 0.7
            assert "replayEvidence" not in result
            body = request_body(supplied, settings)
            assert validate_result(body, supplied, result, 0.9)["fallback"] == "low_confidence"
            durable = ledger.lookup("seat", request_id(supplied))
            assert durable["replayEvidence"] == json.loads(body["input"])
            before = ledger.status("seat")
            assert await client.lookup(request_id(supplied)) == result
            assert await client.batch(supplied) == result
            assert ledger.status("seat") == before
            upstream.evaluate.assert_awaited_once()
            async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(socket))) as session:
                async with session.get(
                    "http://localhost/batch/" + request_id(supplied), headers={"Authorization": "Bearer other-secret"}
                ) as response:
                    assert (await response.json())["fallback"] == "not_submitted"
                wrong_player = {**supplied, "player": "Greece"}
                async with session.post(
                    "http://localhost/batch",
                    headers={"Authorization": "Bearer secret"},
                    json={"requestId": request_id(wrong_player), "evidence": wrong_player},
                ) as response:
                    assert response.status == 400

    asyncio.run(run())


def test_pending_receipt_survives_restart_and_cannot_dispatch_again(tmp_path):
    async def run():
        settings, ledger = registered(tmp_path)
        supplied = evidence()
        body = request_body(supplied, settings)
        metadata = request_metadata(body, supplied)
        pending = {
            **metadata,
            **failed_answers(body, 0.9, "completion_unknown"),
            "replayEvidence": json.loads(body["input"]),
        }
        assert (
            ledger.reserve("seat", metadata["requestId"], metadata["inputHash"], body, pending_result=pending) is None
        )
        before = ledger.status("seat")
        upstream = AsyncMock()
        socket = tmp_path / "broker.sock"
        broker = DecisionsBroker(tmp_path, upstream)
        async with server(broker.application(), socket=socket):
            client = RoutingClient(socket, tmp_path / "token.json")
            for result in (await client.lookup(request_id(supplied)), await client.batch(supplied)):
                assert result["fallback"] == "completion_unknown"
                assert validate_result(body, supplied, result, 0.9)["fallback"] == "completion_unknown"
            assert ledger.status("seat") == before
            upstream.evaluate.assert_not_awaited()

    asyncio.run(run())


def test_incompatible_receipt_is_not_reconstructed_or_dispatched_and_keeps_reservation(tmp_path):
    async def run():
        settings, ledger = registered(tmp_path)
        supplied = evidence()
        body = request_body(supplied, settings)
        metadata = request_metadata(body, supplied)
        assert ledger.reserve("seat", metadata["requestId"], metadata["inputHash"], body) is None
        before = ledger.status("seat")
        upstream = AsyncMock()
        broker = DecisionsBroker(tmp_path, upstream)
        async with server(broker.application()) as url, aiohttp.ClientSession() as session:
            headers = {"Authorization": "Bearer secret"}
            async with session.post(
                url + "/batch", headers=headers, json={"requestId": request_id(supplied), "evidence": supplied}
            ) as response:
                assert response.status == 409
            async with session.get(url + "/batch/" + request_id(supplied), headers=headers) as response:
                assert response.status == 409
        assert ledger.status("seat") == before
        assert ledger.lookup("seat", metadata["requestId"]) == {
            "answers": [],
            "fallback": "completion_unknown",
            "requestId": metadata["requestId"],
        }
        upstream.evaluate.assert_not_awaited()

    asyncio.run(run())
