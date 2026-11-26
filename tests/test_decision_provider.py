"""Native decision transport retains independent outcomes and accounting evidence."""

import asyncio
import copy
import json

import pytest
from aiohttp import web
from pydantic_ai.models.openai_decisions import OpenAIDecisionsModel
from pydantic_ai.models.system_one import SystemOneModel

from civ_agent.advisory_answers import parse_answers
from civ_agent.container_supervisor import authorize_agent
from civ_agent.decisions_backends import BACKENDS
from civ_agent.decisions_broker import DecisionsBroker, RoutingClient
from civ_agent.decisions_ledger import DecisionsLedger
from civ_agent.decisions_policy import DecisionsSettings
from civ_agent.decisions_tasks import request_id
from civ_agent.diplomacy_decisions import DecisionsClient
from civ_agent.protocol import JsonObject
from tests.test_decisions_broker import server
from tests.test_decisions_ledger import evidence


def body(backend) -> JsonObject:
    return {
        "model": BACKENDS[backend].model,
        "input": '{"private": "evidence"}',
        "questions": [
            {
                "name": f"question:{index}",
                "type": "choice",
                "instructions": "Is this useful?",
                "choices": [
                    {"value": "consider", "description": "Worth considering."},
                    {"value": "skip", "description": "Nothing useful."},
                ],
            }
            for index in range(2)
        ],
    }


def answer(index, *, selected=0.985, confidence=0.4) -> JsonObject:
    return {
        "name": f"question:{index}",
        "type": "choice",
        "choice": "consider",
        "confidence": confidence,
        "probabilities": [
            {"value": "consider", "probability": selected},
            {"value": "skip", "probability": 1 - selected},
        ],
    }


def response_body(backend, rows=None):
    rows = copy.deepcopy(rows if rows is not None else [answer(0), answer(1)])
    if backend == "jev":
        answers = {}
        for row in rows:
            name = row.pop("name")
            if isinstance(row.get("probabilities"), list):
                row["probabilities"] = {p["value"]: p["probability"] for p in row["probabilities"]}
            answers[name] = row
    else:
        answers = rows
    return {
        "model": BACKENDS[backend].model,
        "answers": answers,
        "usage": {
            "input_tokens": 125,
            "output_tokens": 0,
            "total_tokens": 125,
            "input_tokens_details": {"cache_write_tokens": 0, "cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }


def client(backend, url):
    return DecisionsClient(
        "synthetic-provider-key" if backend == "luna" else None,
        backend=backend,
        endpoint=url + ("/v1/decisions" if backend == "luna" else "/v1/systemone"),
    )


@pytest.mark.parametrize("backend", ["luna", "jev"])
def test_native_model_decides_once_with_existing_questions_and_probability_policy(backend, monkeypatch):
    native = OpenAIDecisionsModel if backend == "luna" else SystemOneModel
    original = native.decide
    native_calls, native_returns, wire = [], [], []

    async def decide(self, request, model_settings):
        native_calls.append((request, model_settings))
        result = await original(self, request, model_settings)
        native_returns.append(result)
        return result

    monkeypatch.setattr(native, "decide", decide)
    monkeypatch.setenv("SYSTEM_ONE_API_KEY", "unrelated-environment-key")

    async def run():
        supplied = response_body(backend)

        async def provider(request):
            wire.append((dict(request.headers), await request.json()))
            return web.json_response(supplied)

        app = web.Application()
        app.router.add_post("/v1/decisions" if backend == "luna" else "/v1/systemone", provider)
        async with server(app) as url:
            result = await client(backend, url).evaluate(body(backend))
        assert result["fallback"] is None and len(native_calls) == len(native_returns) == len(wire) == 1
        request, settings = native_calls[0]
        assert request.state == body(backend)["input"]
        assert list(request.questions) == ["question:0", "question:1"]
        assert request.questions["question:0"].criteria == {
            "consider": "Worth considering.",
            "skip": "Nothing useful.",
        }
        assert all(a["confidence"] == a["probability"] == 0.985 for a in result["answers"])
        assert all(a["providerConfidence"] == 0.4 for a in result["answers"])
        assert result["usage"] == supplied["usage"]
        headers, sent = wire[0]
        assert sent["model"] == BACKENDS[backend].model
        if backend == "luna":
            assert headers["Authorization"] == "Bearer synthetic-provider-key"
            assert sent["questions"] == body(backend)["questions"]
            assert settings["max_retries"] == 0
        else:
            assert "Authorization" not in headers
            assert set(sent) == {"model", "state", "questions"}
            assert sent["questions"]["question:0"]["criteria"] == request.questions["question:0"].criteria

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["luna", "jev"])
@pytest.mark.parametrize("failure", ["refusal", "missing", "invalid_choice", "invalid_probability", "no_probability"])
def test_one_invalid_question_keeps_the_independent_valid_answer_without_another_call(backend, failure):
    async def run():
        rows = [answer(0), answer(1)]
        if failure == "refusal":
            rows[1] = {"name": "question:1", "type": "refusal", "reason": "private provider prose"}
        elif failure == "missing":
            rows.pop()
        elif failure == "invalid_choice":
            rows[1]["choice"] = "invented"
        elif failure == "invalid_probability":
            rows[1]["probabilities"][0]["probability"] = True
        else:
            del rows[1]["probabilities"]
        supplied, calls = response_body(backend, rows), []

        async def provider(request):
            calls.append(await request.json())
            return web.json_response(supplied)

        app = web.Application()
        app.router.add_post("/v1/decisions" if backend == "luna" else "/v1/systemone", provider)
        async with server(app) as url:
            raw = await client(backend, url).evaluate(body(backend))
        assert len(calls) == 1 and raw["fallback"] is None
        parsed = parse_answers(body(backend), raw, 0.98)
        assert [row["name"] for row in parsed["answers"]] == ["question:0"]
        assert parsed["questionResults"][1]["status"] == {"refusal": "refusal", "missing": "missing"}.get(
            failure, "malformed"
        )
        assert raw["usage"] == supplied["usage"]
        assert "private provider prose" not in json.dumps(raw)

    asyncio.run(run())


def test_duplicate_question_does_not_discard_its_independent_valid_sibling():
    async def run():
        calls = []

        async def provider(request):
            calls.append(await request.json())
            return web.json_response(response_body("luna", [answer(0), answer(1), answer(1)]))

        app = web.Application()
        app.router.add_post("/v1/decisions", provider)
        async with server(app) as url:
            raw = await client("luna", url).evaluate(body("luna"))
        parsed = parse_answers(body("luna"), raw, 0.98)
        assert len(calls) == 1
        assert [row["name"] for row in parsed["answers"]] == ["question:0"]
        assert parsed["questionResults"][1]["status"] == "malformed"

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["luna", "jev"])
@pytest.mark.parametrize(
    "failure", ["boolean_confidence", "string_confidence", "string_probability", "boolean_vector", "not_argmax"]
)
def test_native_numeric_coercion_cannot_relax_the_broker_probability_contract(backend, failure):
    async def run():
        rows = [answer(0), answer(1)]
        if failure == "boolean_confidence":
            rows[1]["confidence"] = True
        elif failure == "string_confidence":
            rows[1]["confidence"] = "0.99"
        elif failure == "string_probability":
            rows[1]["probabilities"][0]["probability"] = "0.985"
        elif failure == "boolean_vector":
            rows[1]["probabilities"] = [
                {"value": "consider", "probability": True},
                {"value": "skip", "probability": False},
            ]
        else:
            rows[1]["choice"] = "skip"

        async def provider(_):
            return web.json_response(response_body(backend, rows))

        app = web.Application()
        app.router.add_post("/v1/decisions" if backend == "luna" else "/v1/systemone", provider)
        async with server(app) as url:
            raw = await client(backend, url).evaluate(body(backend))
        parsed = parse_answers(body(backend), raw, 0.98)
        assert [row["name"] for row in parsed["answers"]] == ["question:0"]
        assert parsed["questionResults"][1]["status"] == "malformed"

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["luna", "jev"])
def test_response_model_mismatch_is_rejected_without_echoing_the_model(backend):
    async def run():
        async def provider(_):
            return web.json_response({**response_body(backend), "model": "private-wrong-model"})

        app = web.Application()
        app.router.add_post("/v1/decisions" if backend == "luna" else "/v1/systemone", provider)
        async with server(app) as url:
            decisions = client(backend, url)
            raw = await decisions.evaluate(body(backend))
        assert raw["fallback"] == "service_failure" and raw["answers"] == []
        assert raw["providerError"] == {"kind": "response_invalid"}
        assert "private-wrong-model" not in json.dumps(raw)
        assert "private-wrong-model" not in json.dumps(decisions.health_snapshot())

    asyncio.run(run())


@pytest.mark.parametrize(
    "usage", [None, {}, {"output_tokens": 0}, {"input_tokens": True}, {"input_tokens": "0"}, {"input_tokens": 0}]
)
def test_missing_usage_never_becomes_zero_or_releases_a_paid_reservation(tmp_path, usage):
    async def run():
        request = body("luna")
        ledger = DecisionsLedger(tmp_path)
        ledger.register(
            "seat", "game", "Rome", DecisionsSettings(decisions_enabled=True, decisions_backend="luna").model_dump()
        )
        assert ledger.reserve("seat", "request", "evidence", request) is None
        reserved = ledger.status("seat")["reservedNanodollars"]
        assert reserved > 0
        supplied = response_body("luna")
        if usage is None:
            del supplied["usage"]
        else:
            supplied["usage"] = usage

        async def provider(_):
            return web.json_response(supplied)

        app = web.Application()
        app.router.add_post("/v1/decisions", provider)
        async with server(app) as url:
            raw = await client("luna", url).evaluate(request)
        assert raw["usage"] == (usage or {})
        ledger.complete("seat", "request", raw)
        status = ledger.status("seat")
        assert status["reservedNanodollars"] == (0 if usage == {"input_tokens": 0} else reserved)
        assert status["spentNanodollars"] == 0

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["luna", "jev"])
@pytest.mark.parametrize(
    "status,kind",
    [(401, "authentication"), (403, "access_denied"), (429, "rate_limited"), (503, "upstream_unavailable")],
)
def test_native_http_error_is_safe_and_never_retried(backend, status, kind):
    async def run():
        calls = []

        async def provider(request):
            calls.append(await request.json())
            return web.Response(status=status, text="private provider body synthetic-provider-key")

        app = web.Application()
        app.router.add_post("/v1/decisions" if backend == "luna" else "/v1/systemone", provider)
        async with server(app) as url:
            decisions = client(backend, url)
            result = await decisions.evaluate(body(backend))
        assert len(calls) == 1
        assert result["providerError"] == {"kind": kind, "httpStatus": status}
        assert result["fallback"] == "service_failure" and result["usage"] == {}
        assert "private" not in json.dumps(result)
        assert "synthetic-provider-key" not in json.dumps(decisions.health_snapshot())

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["luna", "jev"])
def test_redirect_cannot_send_player_evidence_or_credentials_to_another_endpoint(backend):
    async def run():
        redirected = []

        async def provider(_):
            raise web.HTTPTemporaryRedirect(location="/unexpected")

        async def unexpected(request):
            redirected.append((dict(request.headers), await request.read()))
            return web.json_response(response_body(backend))

        app = web.Application()
        app.router.add_post("/v1/decisions" if backend == "luna" else "/v1/systemone", provider)
        app.router.add_post("/unexpected", unexpected)
        async with server(app) as url:
            result = await client(backend, url).evaluate(body(backend))
        assert redirected == [] and result["fallback"] == "service_failure"

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["luna", "jev"])
@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_and_external_cancellation_do_not_restart_provider_work(backend, cancel):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def provider(request):
            calls.append(await request.json())
            entered.set()
            await release.wait()
            return web.json_response(response_body(backend))

        app = web.Application()
        app.router.add_post("/v1/decisions" if backend == "luna" else "/v1/systemone", provider)
        async with server(app) as url:
            decisions = client(backend, url)
            task = asyncio.create_task(decisions.evaluate(body(backend), timeout=5 if cancel else 0.1))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                if cancel:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                    assert decisions.health_snapshot()["circuit"]["consecutiveFailures"] == 0
                else:
                    raw = await task
                    assert raw["fallback"] == "timeout"
                    assert raw["providerError"] == {"kind": "timeout"}
                assert len(calls) == 1
            finally:
                release.set()

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["luna", "jev"])
def test_native_partial_reply_lost_after_commit_is_recovered_once_after_broker_restart(tmp_path, backend):
    class LostReplyBroker(DecisionsBroker):
        def _reply(self, principal, result):
            # The provider response and usage have committed. Simulate the
            # broker failing before the caller receives its acknowledgement.
            raise web.HTTPServiceUnavailable()

    async def run():
        settings = DecisionsSettings(decisions_enabled=True, decisions_backend=backend)
        auth, seat = tmp_path / "auth", tmp_path / "seat"
        seat.mkdir()
        authorize_agent(auth, "game", 0, seat, decisions=True, settings=settings.model_dump(), player="Rome")
        frozen = evidence()
        frozen["preservation"].append(
            {"key": "event:2", "version": 1, "record": {"body": {"text": "Another private event."}}}
        )
        identifier = request_id(frozen, model=BACKENDS[backend].model)
        socket = tmp_path / "broker.sock"
        remote = RoutingClient(socket, seat / "decisions-client.json", backend=backend)
        calls = []
        valid = {**answer(0), "name": "preservation:0"}
        refused = {"name": "preservation:1", "type": "refusal", "reason": "private refusal prose"}

        async def provider(request):
            calls.append(await request.json())
            return web.json_response(response_body(backend, [valid, refused]))

        app = web.Application()
        app.router.add_post("/v1/decisions" if backend == "luna" else "/v1/systemone", provider)
        async with server(app) as url:
            initial = LostReplyBroker(auth, client(backend, url))
            async with server(initial.application(), socket=socket):
                lost = await remote.batch(frozen)
                assert lost["fallback"] == "broker_unavailable" and lost["requestId"] == identifier
                committed = initial.ledger.status("game-0")
                assert committed["requests"] == 1 and committed["inputTokens"] == 125
                assert committed["reservedNanodollars"] == committed["unknownCompletions"] == 0
            initial.ledger.engine.dispose()

            # A fresh broker/client has no in-memory response or prior circuit
            # state. Free lookup and duplicate submission use the durable reply.
            restarted = DecisionsBroker(auth, client(backend, url))
            async with server(restarted.application(), socket=socket):
                recovered = await remote.lookup(identifier)
                duplicate = await remote.batch(frozen)
                assert recovered == duplicate
                assert recovered["requestId"] == identifier
                assert [row["name"] for row in recovered["answers"]] == ["preservation:0"]
                assert [row["status"] for row in recovered["questionResults"]] == ["accepted", "refusal"]
                assert recovered["rawAnswers"][0]["probability"] == 0.985
                assert recovered["rawAnswers"][0]["providerConfidence"] == 0.4
                assert recovered["accounting"] == committed
                assert committed["spentNanodollars"] == 125 * BACKENDS[backend].nanodollars_per_input_token
                assert "private refusal prose" not in json.dumps(recovered)
            restarted.ledger.engine.dispose()
        assert len(calls) == 1

    asyncio.run(run())
