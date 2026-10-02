"""Codex control errors identify the stalled operation without exposing inputs."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from civ_agent.agent_service import CodexProcess


def test_request_timeout_identifies_method_and_clears_pending_state(tmp_path, monkeypatch):
    async def run():
        process = CodexProcess("codex", tmp_path, "", None, AsyncMock())
        process.socket = Mock()
        process.socket.send_json = AsyncMock()

        async def timeout(future, seconds):
            assert seconds == 30 and list(process.pending.values()) == [future]
            future.cancel()
            raise TimeoutError

        monkeypatch.setattr(asyncio, "wait_for", timeout)
        with pytest.raises(TimeoutError, match="^Codex thread/start did not respond within 30 seconds$"):
            await process.request("thread/start", {"developerInstructions": "PRIVATE_PROMPT"})
        assert not process.pending and not process.requests

    asyncio.run(run())
