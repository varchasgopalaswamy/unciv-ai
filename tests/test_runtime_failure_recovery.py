"""Transient model failures retain their cause and never replay native actions."""

import json

import pytest

from civ_agent.decision_contracts import DecisionContract
from civ_agent.economy_roles import RequestDisposition
from tests.domain_fixtures import managed, run_async


@pytest.mark.parametrize("date", [None, 76])
@run_async
async def test_request_review_date_has_a_direct_nullable_wire_value(tmp_path, date):
    async with managed(tmp_path) as m:
        contract = DecisionContract("economy", await m.gateway.tools(m.economy))
        disposition = {
            "id": "request1",
            "status": "deferred",
            "reason": "Reassess after the worker finishes.",
            "reviewTurn": date,
        }
        wire = json.loads(contract.encode({"kind": "finish", "arguments": {"requests": [disposition]}}))
        assert wire["decision"]["arguments"]["requests"][0]["reviewTurn"] == date
        assert contract.parse(json.dumps(wire))["arguments"]["requests"][0]["reviewTurn"] == date
        # Existing direct callers may still omit the date.
        assert (
            RequestDisposition.model_validate({k: v for k, v in disposition.items() if k != "reviewTurn"}).reviewTurn
            is None
        )
