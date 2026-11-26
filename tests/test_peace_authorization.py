import asyncio
from types import SimpleNamespace

from civ_agent.orchestration.peace_authorization import NativePeace


def test_non_peace_action_does_not_start_consultation():
    peace = NativePeace(SimpleNamespace(), SimpleNamespace())
    assert asyncio.run(peace.authorize(None, "trade_decide", {"decision": "reject"})) is None
