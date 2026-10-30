"""Bounded read bundles reuse the ordinary permission, evidence and replay paths."""

from __future__ import annotations

import copy
import hashlib
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import Field, ValidationError

from civ_agent.delivery_options import DeliveryOptions, full_delivery
from civ_agent.diplomacy_tools import argument_error
from civ_agent.protocol import JsonObject
from civ_agent.response_compaction import compact_response, expand_response
from civ_agent.schemas import BoundaryModel

if TYPE_CHECKING:
    from civ_agent.proposal_gateway import JobAccess, ProposalGateway


class InspectionQuery(BoundaryModel):
    tool: Literal[
        "player_turn",
        "city_details",
        "unit_details",
        "construction_details",
        "player_details",
        "map_region",
        "movement_preview",
        "mission_preview",
        "player_inspect",
        "memory_read",
        "memory_search",
        "military_review",
    ]
    arguments: JsonObject = Field(default_factory=dict)


class InspectionBundle(DeliveryOptions):
    queries: Annotated[list[InspectionQuery], Field(min_length=1, max_length=12)] = Field(
        description="Independent reads with arguments already known. Results are ordered; each query uses its "
        "normal tool schema and permissions. Never include actions or depend on another result in this bundle."
    )


async def run_bundle(
    gateway: ProposalGateway, job: JobAccess, arguments: JsonObject, call_id: str
) -> tuple[JsonObject, bool]:
    from civ_agent.tool_schemas import tool_model

    try:
        request = InspectionBundle.model_validate(arguments)
    except ValidationError as error:
        return argument_error(InspectionBundle, arguments, error), False
    async with job.bundle_lock:
        gateway.check(job)
        frozen = job.review_inputs is not None
        if not frozen:
            async with job.lock:
                saved = await gateway.io.call(gateway.memory.call, job.id, call_id, "inspect_bundle", arguments)
                if saved is not None:
                    for item in expand_response(saved).get("results", []):
                        gateway.accept_coordination(job, item["result"])
                        gateway.accept_payloads(job, item["tool"], item["arguments"], item["result"], replayed=True)
                    return saved, True
        allowed = {tool["name"] for tool in await gateway._tools(job)}
        queries = []
        # Validate the whole request before delivering any partial inspections.
        for index, query in enumerate(request.queries):
            if query.tool not in allowed:
                return {"error": "TOOL_NOT_PERMITTED", "queryIndex": index, "applied": False}, False
            model = tool_model(query.tool, "review" if frozen else job.purpose)
            try:
                model.model_validate(query.arguments)
            except ValidationError as error:
                return {**argument_error(model, query.arguments, error), "queryIndex": index}, False
            supplied = copy.deepcopy(query.arguments)
            if full_delivery(arguments) and issubclass(model, DeliveryOptions):
                supplied.pop("full", None)
                supplied["delivery"] = "full"
            queries.append((query.tool, supplied))
        before = (job.observation or {}).get("revision")
        replies = []
        prefix = "inspection:" + hashlib.sha256(call_id.encode()).hexdigest()
        for index, (name, supplied) in enumerate(queries):
            # The parent already owns gateway admission. Re-entering admission
            # here could deadlock a checkpoint waiting for this accepted bundle.
            reply = await gateway._admitted_call(job, name, supplied, f"{prefix}:{index}", source="internal")
            replies.append({"tool": name, "arguments": supplied, "result": expand_response(reply)})
            if "error" in reply:
                break
        result = {
            "results": replies,
            "remaining": len(queries) - len(replies),
            "positionChanged": before != (job.observation or {}).get("revision"),
        }
        if replies and "error" in replies[-1]["result"]:
            result.update(error="INSPECTION_FAILED", queryIndex=len(replies) - 1, applied=False)
        result = compact_response(result, full=full_delivery(arguments), share=job.compact_overviews)
        if not frozen:
            async with job.lock:
                gateway.check(job)
                result = await gateway.io.call(
                    gateway.memory.call, job.id, call_id, "inspect_bundle", arguments, result
                )
                assert result is not None
        return result, False
