"""Opt-in pure selectors, installed after the legacy worker-local result helpers.

Host imports are excluded from the worker source at the explicit marker below.
Neither implementation performs a tool call, authorizes a command, or retries it.
"""

from civ_agent.runtime.code_mode_local import (
    _cm_copy,
    _cm_nonempty,
    _cm_object,
    _cm_retry,
    _cm_validated,
    _CmAny,
    result_entity,
)

# Worker-local selector source starts here.
_cm_selector_version = "result-selectors-v1"


def _cm_selector_equal(left: _CmAny, right: _CmAny) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_cm_selector_equal(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _cm_selector_equal(left[index], right[index]) for index in range(len(left))
        )
    return left == right


def select_one(rows: _CmAny, *, where: _CmAny) -> dict[str, _CmAny] | None:
    """Copy the sole exact top-level field match, or return None; reject ambiguity."""
    criteria = _cm_object(where)
    if not criteria or not isinstance(rows, list):
        return _cm_retry(
            "RESULT_SELECTION_INVALID", "Supply a list of objects and nonempty exact field criteria.", where
        )
    copied = _cm_copy(rows)
    matches = []
    for row in copied:
        if not isinstance(row, dict):
            return _cm_retry("RESULT_SELECTION_INVALID", "Every candidate must be a JSON object.", row)
        if all(key in row and _cm_selector_equal(row[key], expected) for key, expected in criteria.items()):
            matches.append(row)
    if len(matches) > 1:
        return _cm_retry(
            "RESULT_SELECTION_AMBIGUOUS",
            "More than one candidate matches. Add observed identifying fields; do not choose an arbitrary first row.",
            {"matches": len(matches)},
        )
    return matches[0] if matches else None


def movement_options(result: _CmAny, *, envelope: str = "native") -> dict[str, _CmAny]:
    """Copy unit_details movement options and retain their complete original feedback."""
    feedback = _cm_validated(result, envelope)
    entity = result_entity(result=feedback, envelope=envelope)
    if envelope == "native" and feedback["receipts"][0]["tool"] != "unit_details":
        return _cm_retry("RESULT_MOVEMENT_INVALID", "Use a unit_details movement-section result.", feedback)
    movement = entity.get("movement")
    moves = entity.get("moves")
    if (
        not _cm_nonempty(entity.get("handle"))
        or isinstance(movement, bool)
        or not isinstance(movement, (str, int, float))
        or not isinstance(moves, list)
        or not all(isinstance(move, dict) for move in moves)
    ):
        return _cm_retry(
            "RESULT_MOVEMENT_INVALID",
            "Read unit_details with sections=['movement']. Its data.entity has handle, scalar movement, and a moves list; "
            "missing moves does not mean there are no options.",
            feedback,
        )
    return {"feedback": feedback, "unit": entity["handle"], "movement": movement, "options": moves}


def receipt_result(result: _CmAny, *, operation_id: str) -> dict[str, _CmAny]:
    """Select one exact operation ID without promoting rejected or unexecuted receipts."""
    feedback = _cm_validated(result, "native")
    if not _cm_nonempty(operation_id):
        return _cm_retry("RESULT_RECEIPT_INVALID", "Supply the exact nonempty observed operationId.", feedback)
    receipt = select_one(feedback["receipts"], where={"operationId": operation_id})
    return {"feedback": feedback, "receipt": receipt}


def _cm_selector_bundle(result: _CmAny) -> dict[str, _CmAny]:
    bundle = _cm_object(result)
    entries = bundle.get("receiptEntries")
    if type(bundle.get("receiptVersion")) is not int or bundle["receiptVersion"] != 1 or not isinstance(entries, list):
        return _cm_retry(
            "RESULT_BUNDLE_INVALID", "Use the receipt-preserving inspect_bundle result (receiptVersion=1).", bundle
        )
    feedback = _cm_validated(bundle.get("feedback"), "native")
    if not _cm_selector_equal(bundle.get("unexecuted"), feedback["unexecuted"]):
        return _cm_retry("RESULT_BUNDLE_INVALID", "Bundle and native unexecuted operation IDs must agree.", feedback)
    identifiers = []
    for index in range(len(entries)):
        entry = entries[index]
        if (
            not isinstance(entry, dict)
            or not _cm_nonempty(entry.get("queryId"))
            or type(entry.get("queryIndex")) is not int
            or entry["queryIndex"] != index
            or not _cm_nonempty(entry.get("tool"))
            or not isinstance(entry.get("arguments"), dict)
        ):
            return _cm_retry(
                "RESULT_BUNDLE_INVALID", "Every query needs its exact index, ID, tool and arguments.", feedback
            )
        identifiers.append(entry["queryId"])
        receipt = feedback["receipts"][index] if index < len(feedback["receipts"]) else None
        if not _cm_selector_equal(entry.get("receipt"), receipt):
            return _cm_retry(
                "RESULT_BUNDLE_INVALID", "A query receipt must equal its original native receipt.", feedback
            )
        if receipt is not None and receipt.get("tool", entry["tool"]) != entry["tool"]:
            return _cm_retry("RESULT_BUNDLE_INVALID", "A query and its receipt must name the same tool.", feedback)
        status = receipt["status"] if receipt is not None else "not_returned"
        if entry.get("status") != status:
            return _cm_retry("RESULT_BUNDLE_INVALID", "Keep each native receipt's status unchanged.", feedback)
    if len(identifiers) != len(set(identifiers)):
        return _cm_retry("RESULT_BUNDLE_INVALID", "Query IDs must be unique.", feedback)
    return bundle


def bundle_query(
    result: _CmAny, *, query_id: str | None = None, tool: str | None = None, arguments: _CmAny = None
) -> dict[str, _CmAny]:
    """Select one rich bundle query by ID or exact tool/arguments, retaining all feedback."""
    bundle = _cm_selector_bundle(result)
    if query_id is not None:
        if not _cm_nonempty(query_id) or tool is not None or arguments is not None:
            return _cm_retry("RESULT_QUERY_INVALID", "Choose query_id alone, or tool plus exact arguments.", bundle)
        where = {"queryId": query_id}
    else:
        if not _cm_nonempty(tool) or not isinstance(arguments, dict):
            return _cm_retry("RESULT_QUERY_INVALID", "Choose query_id alone, or tool plus exact arguments.", bundle)
        where = {"tool": tool, "arguments": _cm_object(arguments)}
    return {"bundle": bundle, "query": select_one(bundle["receiptEntries"], where=where)}
