"""Operator-controlled interactive sessions use the same proposal boundary as jobs."""

from civ_agent import service_recovery
from civ_agent.civilization_state import StateConflict, job_id
from civ_agent.host_connection import HostConnection
from civ_agent.schemas import InteractiveRequest, validate
from civ_agent.startup_context import prepare_startup, startup_text


async def open_session(service, payload):
    validate(InteractiveRequest, payload)
    if payload["hostUrl"] != service.mcp_url:
        raise ValueError("INVALID_INTERACTIVE_BINDING")
    if service.chat.running or (service.active_task is not None and not service.active_task.done()):
        raise StateConflict("SCHEDULED_JOB_ACTIVE")
    connection = HostConnection(
        service.mcp_url,
        payload["playerToken"],
        service.ca_file,
        diagnostics=service.diagnostics,
        context={"purpose": "interactive"},
    )
    job = None
    try:
        view = await connection.tool("player_turn", {})
        identity = await connection.tool("player_communication", {"op": "state", "epoch": view.get("epoch")})
        if identity.get("civilization") != service.civilization:
            raise StateConflict("PLAYER_CREDENTIAL_MISMATCH")
        if view.get("phase") not in {"ACTIVE", "WAITING"}:
            raise StateConflict("Return the host to manual control before opening an interactive session.")
        await service.io.call(service.memory.bind, view["epoch"])
        for prior in list(service.gateway.jobs.values()):
            await service.gateway.close(prior, False)
        await service_recovery.barrier(service, False)
        job = await service.gateway.open(
            "interactive-" + job_id(),
            "gameplay",
            view["epoch"],
            connection,
            {key: payload[key] for key in ("model", "reasoningEffort")},
            view["turn"],
            owns_connection=True,
        )
        return {
            "jobId": job.id,
            "token": job.token,
            "url": service.gateway_url,
            "caFile": str(service.gateway_ca_file) if service.gateway_ca_file else None,
            "context": service.memory.context(job.memory)
            + "\n\n"
            + startup_text(await prepare_startup(service.gateway, job)),
        }
    except BaseException:
        if job is not None:
            await service.gateway.close(job, False)
        else:
            await connection.close()
        raise


async def close_session(service, payload):
    if set(payload) != {"jobId"}:
        raise ValueError("INVALID_ARGUMENT")
    for job in list(service.gateway.jobs.values()):
        if job.id == payload["jobId"] and job.id.startswith("interactive-"):
            await service.gateway.close(job, True)
    return {"closed": True}
