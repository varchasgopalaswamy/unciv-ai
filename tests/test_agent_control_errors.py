"""Control responses distinguish malformed requests from internal state failures."""

import asyncio
import json
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web
from multidict import CIMultiDict

from civ_agent.agent_service import AgentService
from civ_agent.civilization_state import StateConflict


def control(error=None, *, parsing=False):
    service = Mock(spec=AgentService)
    service.token = "control-test"
    service.gateway = SimpleNamespace(handle=AsyncMock())
    service.start = AsyncMock(side_effect=None if parsing else error, return_value={"started": True})
    application = AgentService.application(service)
    handler = next(iter(application.router.routes())).handler
    request = SimpleNamespace(
        secure=True,
        headers=CIMultiDict({"Authorization": "Bearer control-test"}),
        json=AsyncMock(side_effect=error if parsing else None, return_value={"op": "start"}),
    )

    async def dispatch():
        return cast(web.Response, await handler(cast(web.Request, request)))

    return dispatch, service


def test_request_json_parse_failure_is_reported_as_invalid_json():
    dispatch, service = control(ValueError("malformed JSON"), parsing=True)
    response = asyncio.run(dispatch())
    assert response.status == 400 and json.loads(cast(bytes, response.body)) == {"error": "INVALID_JSON"}
    service.start.assert_not_called()


def test_receipt_conflict_keeps_its_real_error_and_conflict_status():
    dispatch, _ = control(StateConflict("RECEIPT_CONFLICT"))
    response = asyncio.run(dispatch())
    assert response.status == 409 and json.loads(cast(bytes, response.body)) == {"error": "RECEIPT_CONFLICT"}


def test_internal_value_error_is_not_misreported_as_malformed_request():
    dispatch, _ = control(ValueError("internal invariant failed"))
    with pytest.raises(ValueError, match="internal invariant failed"):
        asyncio.run(dispatch())
