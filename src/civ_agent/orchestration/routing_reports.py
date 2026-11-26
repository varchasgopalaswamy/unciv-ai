"""Read-only classifier reports from scoped workflow links and final receipts."""

from pydantic import ValidationError
from sqlalchemy import func, select

from civ_agent import memory_schema as tables
from civ_agent.orchestration.contracts import Scope
from civ_agent.orchestration.routing_models import RoutingLink, RoutingResult
from civ_agent.orchestration.store import JournalConflict
from civ_agent.protocol import JsonObject
from civ_agent.telemetry import classifier_metadata

_UNCERTAIN = {"broker_unavailable", "completion_unknown", "malformed_broker_output", "not_submitted"}


def routing_reports(root, scope: Scope) -> list[JsonObject]:
    """Derive each request once, without another journal or accounting write.

    Links identify admitted workflow inputs. Only their final completion receipt
    establishes an observed outcome; an unfinished workflow retains explicit
    uncertainty and contributes no invented usage. Broker accounting remains the
    spending authority.
    """
    root.check_owner(scope)
    with root.store.transaction(scope):
        pass
    with root.memory.transaction() as db:
        if root.memory._meta(db, "epoch") != scope.epoch:
            raise JournalConflict("ROUTING_REPORT_EPOCH_CONFLICT")
        rows = list(
            db.execute(
                select(tables.meta.c.key, tables.meta.c.value)
                .where(
                    tables.meta.c.key.like("nativeRoutingLink:%"),
                    func.json_extract(tables.meta.c.value, "$.kind") == "routing",
                    func.json_extract(tables.meta.c.value, "$.scope.game") == scope.game,
                    func.json_extract(tables.meta.c.value, "$.scope.player") == scope.player,
                    func.json_extract(tables.meta.c.value, "$.scope.epoch") == scope.epoch,
                )
                .order_by(tables.meta.c.key)
            )
        )
    reports: dict[str, tuple[int, JsonObject]] = {}
    for key, encoded in rows:
        try:
            link = RoutingLink.model_validate_json(encoded)
        except ValidationError:
            raise JournalConflict("ROUTING_REPORT_LINK_CONFLICT") from None
        if (
            (link.scope.game, link.scope.player, link.scope.epoch) != (scope.game, scope.player, scope.epoch)
            or key != "nativeRoutingLink:" + link.id
            or link.request is None
        ):
            raise JournalConflict("ROUTING_REPORT_LINK_CONFLICT")
        request = link.request
        recorded = root.store.receipt(link.scope, "routing-result")
        rank = 0
        if recorded is None:
            outcome = {"requestId": request.request_id, "fallback": "completion_unknown"}
            status = "uncertain"
        else:
            try:
                result = RoutingResult.model_validate(recorded)
            except ValidationError:
                raise JournalConflict("ROUTING_REPORT_RECEIPT_CONFLICT") from None
            if (result.scope, result.request_id) != (request.scope, request.request_id):
                raise JournalConflict("ROUTING_REPORT_RECEIPT_CONFLICT")
            outcome = root.store.get(result.scope, result.outcome)
            if (
                not isinstance(outcome, dict)
                or outcome.get("requestId") != request.request_id
                or outcome.get("direction") != request.direction
            ):
                raise JournalConflict("ROUTING_REPORT_OUTCOME_CONFLICT")
            reason = outcome.get("fallback")
            uncertain = reason is not None and (not isinstance(reason, str) or reason in _UNCERTAIN)
            status = "uncertain" if uncertain else "completed"
            rank = 1 if status == "uncertain" else 2
        report = {
            "direction": request.direction,
            "status": status,
            **classifier_metadata(request.direction, outcome),
        }
        prior = reports.get(request.request_id)
        if prior is not None and prior[0] == rank and prior[1] != report:
            raise JournalConflict("ROUTING_REPORT_REQUEST_CONFLICT")
        if prior is None or rank > prior[0]:
            reports[request.request_id] = (rank, report)
    return [reports[identifier][1] for identifier in sorted(reports)]
