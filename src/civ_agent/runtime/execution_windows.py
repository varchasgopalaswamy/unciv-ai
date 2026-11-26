"""Code Mode hook for an optional, privately bound live execution window."""

from contextlib import asynccontextmanager

from pydantic_ai import ModelRetry

from civ_agent.execution_windows import WindowBinding, detached_focus, release_focus
from civ_agent.orchestration.contracts import RoleRequest

__all__ = ["detached_focus", "focused_execution", "release_focus"]


@asynccontextmanager
async def focused_execution(
    scheduler,
    request,
    *,
    run_id: str,
    parent_call_id: str,
    code: str,
    enabled: bool,
    seconds: float = 10.0,
    max_calls: int = 6,
    queue_timeout: float = 30.0,
):
    if not enabled:
        yield None
        return
    if scheduler is None or not isinstance(request, RoleRequest) or request.purpose == "communication":
        raise ModelRetry("FOCUSED_WINDOW_UNAVAILABLE. Focus is only available to live player roles.")
    binding = WindowBinding.create(request, run_id=run_id, parent_call_id=parent_call_id, code=code)
    async with scheduler.focused(binding, seconds=seconds, max_calls=max_calls, queue_timeout=queue_timeout) as window:
        yield window
