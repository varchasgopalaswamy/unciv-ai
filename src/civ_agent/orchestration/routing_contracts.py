"""Immutable broker routing requests and independently checked decisions."""

from typing import Annotated, Literal

from pydantic import Field

from civ_agent.decisions_backends import BACKENDS, DEFAULT_BACKEND, provider_answers
from civ_agent.decisions_ledger import fingerprint
from civ_agent.decisions_policy import DecisionsSettings, routing_thresholds
from civ_agent.diplomacy_decisions import POLICY_VERSION, fallback, parse_answer, request_body
from civ_agent.orchestration.contracts import Digest
from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel, Identifier

ROUTING_VERSION = "durable-routing-v1"
type RoutingDirection = Literal["incoming", "outgoing"]


class RoutingSubmission(BoundaryModel):
    version: Literal["durable-routing-v1"] = ROUTING_VERSION
    requestId: Digest
    operationId: Identifier
    direction: RoutingDirection
    bundle: Annotated[JsonObject, Field(repr=False)]


def routing_request_id(operation_id, direction, bundle, backend=DEFAULT_BACKEND):
    return fingerprint([ROUTING_VERSION, operation_id, BACKENDS[backend].model, POLICY_VERSION, direction, bundle])


def routing_submission(operation_id, direction, bundle, backend=DEFAULT_BACKEND):
    return RoutingSubmission(
        requestId=routing_request_id(operation_id, direction, bundle, backend),
        operationId=operation_id,
        direction=direction,
        bundle=bundle,
    )


def routing_metadata(submission: RoutingSubmission, settings: DecisionsSettings) -> JsonObject:
    body = request_body(
        submission.direction,
        submission.bundle,
        backend=settings.decisions_backend,
        policy_version=settings.decisions_routing_policy,
    )
    return {
        "version": ROUTING_VERSION,
        "requestId": submission.requestId,
        "operationId": submission.operationId,
        "direction": submission.direction,
        "model": body["model"],
        "policyVersion": settings.decisions_routing_policy,
        "policyHash": fingerprint(settings.model_dump()),
        "inputHash": fingerprint(submission.bundle),
        "promptHash": fingerprint(body["questions"]),
        "contextSchemaVersion": submission.bundle.get("schemaVersion"),
        "thresholds": routing_thresholds(settings, submission.direction),
    }


def validate_routing_reply(submission: RoutingSubmission, supplied, settings: DecisionsSettings) -> JsonObject:
    """A stale or forged diagnostic can never grant batching or suppression."""
    metadata = routing_metadata(submission, settings)
    direction = submission.direction
    invalid = {**metadata, **fallback(direction, "malformed_broker_output")}
    if not isinstance(supplied, dict) or supplied.get("requestId") != submission.requestId:
        return invalid
    if supplied.get("fallback") == "broker_unavailable":
        return {**metadata, **fallback(direction, "broker_unavailable")}
    if any(supplied.get(key) != value for key, value in metadata.items()):
        return invalid
    immediate = fallback(direction, "unused")["choice"]
    if supplied.get("choice") == immediate and supplied.get("fallback") is not None:
        # Unavailable classifications always retain immediate attention/delivery.
        return supplied
    body = request_body(direction, submission.bundle, backend=settings.decisions_backend)
    try:
        normalized = provider_answers(body, {"model": body["model"], "answers": supplied.get("rawAnswers")}, "luna")
        checked = parse_answer(direction, {"answers": normalized}, thresholds=metadata["thresholds"])
    except (ValueError, TypeError, KeyError):
        return invalid
    keys = ("choice", "confidence", "questionBasis", "rawChoice", "rawChoiceConfidence", "appliedThreshold")
    if any(supplied.get(key) != checked.get(key) for key in keys):
        return invalid
    return supplied
