"""Native lifecycle adapter; the host owns permits and cumulative job accounting."""

from collections.abc import Awaitable, Callable

from pydantic import ValidationError

from civ_agent.host_connection import api_origin
from civ_agent.orchestration.contracts import Scope
from civ_agent.orchestration.job_contracts import JobGrant, NativeJob, NativeJobReport, host_job_id
from civ_agent.orchestration.store import JournalConflict
from civ_agent.proposal_gateway import PlayerConnection
from civ_agent.protocol import JsonObject


class JobAdmission:
    def __init__(
        self,
        *,
        game: str,
        player: str,
        civilization: str,
        connection: Callable[[Scope], Awaitable[PlayerConnection]],
    ):
        self.game, self.player, self.civilization = game, player, civilization
        self.connection = connection

    async def _invoke(self, op: str, request: NativeJob | NativeJobReport) -> JobGrant:
        try:
            # Validate nested copies again at this external boundary, including
            # models a caller may have mutated with model_copy(update=...).
            if isinstance(request, NativeJobReport):
                report = NativeJobReport.model_validate_json(request.model_dump_json())
                job = report.job
            else:
                report = None
                job = NativeJob.model_validate_json(request.model_dump_json())
        except (ValidationError, ValueError):
            raise JournalConflict("NATIVE_JOB_REQUEST_INVALID") from None
        if (job.scope.game, job.scope.player) != (self.game, self.player):
            raise JournalConflict("NATIVE_JOB_SCOPE_CONFLICT")
        connection = await self.connection(job.scope)
        with api_origin("coordinator"):
            state = await connection.tool("player_communication", {"op": "state", "epoch": job.scope.epoch})
        if (state.get("epoch"), state.get("civilization")) != (job.scope.epoch, self.civilization):
            raise JournalConflict("NATIVE_JOB_CONNECTION_SCOPE_CONFLICT")
        payload: JsonObject = {"op": op, "epoch": job.scope.epoch, "job": host_job_id(job)}
        if op == "job_claim":
            payload["purpose"] = job.purpose
            if job.parent is not None:
                payload["parentJob"] = host_job_id(job.parent)
            if job.review is not None:
                payload["review"] = {
                    "cycleId": job.review.cycle_id,
                    "sourceTurn": job.review.source_turn,
                    "targetTurn": job.review.target_turn,
                }
        elif report is not None:
            payload["usage"] = report.usage.model_dump(mode="json")
            # Keep null fields required by the actual host JSON schema.
            payload["telemetry"] = report.telemetry.model_dump(mode="json")
        with api_origin("coordinator"):
            result = await connection.tool("player_communication", payload)
        try:
            return JobGrant.model_validate(result)
        except ValidationError:
            raise JournalConflict("NATIVE_JOB_HOST_REJECTED") from None

    async def claim(self, job: NativeJob) -> JobGrant:
        """Retry this same immutable identity after ACK loss, never allocate a new ID."""
        return await self._invoke("job_claim", job)

    async def heartbeat(self, request: NativeJob | NativeJobReport) -> JobGrant:
        """Renew the host's 90-second lease; it may also carry cumulative totals."""
        return await self._invoke("job_heartbeat", request)

    async def report(self, report: NativeJobReport) -> JobGrant:
        """Publish a frozen cumulative snapshot without extending its lease."""
        return await self._invoke("job_report", report)

    async def release(self, request: NativeJob | NativeJobReport) -> JobGrant:
        """Release compute and lease; exact duplicate reports remain host-idempotent."""
        return await self._invoke("job_release", request)
