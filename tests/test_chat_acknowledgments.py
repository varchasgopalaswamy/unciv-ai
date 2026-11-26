"""A bad message reference rejects one receipt; it cannot become a disconnect."""

from unittest.mock import AsyncMock

from sqlalchemy import select

from civ_agent import memory_schema as tables
from civ_agent.diplomacy_routing import DiplomacyRouting
from civ_agent.memory_snapshots import load_snapshot
from tests.domain_fixtures import run_async
from tests.test_diplomacy_contracts import opened


def handled(memory, job):
    with memory.transaction() as db:
        saved = db.scalar(select(tables.jobs.c.snapshot).where(tables.jobs.c.id == job.id))
        return load_snapshot(db, saved).get("handledRoutingEventIds", [])


def read(messages=()):
    return {"tool": "chat_read", "arguments": {"handledMessages": list(messages)}}


@run_async
async def test_gateway_rejects_wrong_ack_without_a_structured_loop(tmp_path):
    async with opened(tmp_path, purpose="gameplay") as (gateway, job, _):
        gateway.memory.routing_enabled = True
        gateway.routing = DiplomacyRouting(gateway, AsyncMock())
        response = await gateway.call(job, "chat_read", {"handledMessages": ["7"]}, "ack")
        assert response["error"] == "MESSAGES_NOT_DELIVERED_TO_JOB"
        assert response["applied"] is False
        assert await gateway.call(job, "chat_read", {"handledMessages": ["7"]}, "ack") == response
