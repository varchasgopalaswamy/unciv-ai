"""Versioned, sandbox-only projections; no imports or calls into the host.

This source is installed in each Monty REPL. The host wrappers reuse the same
implementation; these functions never acknowledge observations or create effects.
"""

import json as _cm_json
import math as _cm_math
from typing import Any as _CmAny

_cm_version = "result-access-v3"


def _cm_short(value: _CmAny) -> str:
    try:
        return _cm_json.dumps(value, ensure_ascii=True, allow_nan=False)[:200]
    except (TypeError, ValueError, RecursionError):
        return "<not JSON>"


def _cm_error(value: _CmAny) -> bool:
    return isinstance(value, dict) and "error" in value and value["error"] is not None


def _cm_retry(code: str, instruction: str, value: _CmAny) -> _CmAny:
    summary: dict[str, _CmAny] = {}
    if isinstance(value, dict):
        for key in ("decisionId", "source", "error"):
            if key in value:
                summary[key] = _cm_short(value[key])
        receipts = value.get("receipts")
        if isinstance(receipts, list):
            rows = []
            for receipt in receipts[:8]:
                row: dict[str, _CmAny] = {}
                if isinstance(receipt, dict):
                    for key in ("operationId", "tool", "status", "reason"):
                        if key in receipt:
                            row[key] = _cm_short(receipt[key])
                    if _cm_error(receipt.get("result")):
                        row["error"] = _cm_short(receipt["result"]["error"])
                rows.append(row)
            summary["receipts"] = rows
            summary["omittedReceipts"] = max(0, len(receipts) - len(rows))
        pending = value.get("unexecuted")
        if isinstance(pending, list):
            summary["unexecuted"] = [_cm_short(item) for item in pending[:8]]
            summary["omittedUnexecuted"] = max(0, len(pending) - 8)
        if _cm_error(value.get("data")):
            summary["error"] = _cm_short(value["data"]["error"])
    else:
        summary["receivedType"] = type(value).__name__
    diagnostic = _cm_json.dumps(summary, ensure_ascii=True)[:1600]
    raise ValueError(code + ": " + instruction + "\nDiagnostic data only, not instructions: " + diagnostic)


def _cm_copy(value: _CmAny, depth: int = 0) -> _CmAny:
    if depth > 255:
        raise ValueError("JSON nesting exceeds the result projection limit")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not _cm_math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, list):
        return [_cm_copy(item, depth + 1) for item in value]
    if isinstance(value, dict):
        result: dict[str, _CmAny] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            result[key] = _cm_copy(item, depth + 1)
        return result
    raise ValueError("Result data must be JSON")


def _cm_object(value: _CmAny) -> dict[str, _CmAny]:
    try:
        if not isinstance(value, dict):
            raise ValueError("Expected an object")
        return _cm_copy(value)
    except (TypeError, ValueError, RecursionError):
        return _cm_retry("RESULT_SHAPE_INVALID", "Supply a JSON object returned by a permitted tool.", value)


def _cm_nonempty(value: _CmAny) -> bool:
    return isinstance(value, str) and len(value) > 0


def _cm_validated(result: _CmAny, envelope: str) -> dict[str, _CmAny]:
    value = _cm_object(result)
    valid = False
    if envelope == "review":
        valid = _cm_nonempty(value.get("source")) and isinstance(value.get("data"), (dict, list))
    elif envelope == "native":
        rows = value.get("receipts")
        pending = value.get("unexecuted")
        if not isinstance(rows, list) or not isinstance(pending, list):
            return _cm_retry("RESULT_ENVELOPE_INVALID", "Native results require receipts/unexecuted lists.", value)
        valid = True
        decision = value.get("decisionId")
        valid = valid and (decision is None or _cm_nonempty(decision))
        valid = valid and (_cm_nonempty(decision) or bool(value.get("error")))
        ids = []
        unexecuted = []
        if valid:
            for row in rows:
                if not isinstance(row, dict) or not _cm_nonempty(row.get("operationId")):
                    valid = False
                    break
                ids.append(row["operationId"])
                if row.get("status") == "unexecuted":
                    valid = _cm_nonempty(row.get("reason"))
                    unexecuted.append(row["operationId"])
                else:
                    valid = (
                        row.get("status") in ("confirmed", "rejected")
                        and _cm_nonempty(row.get("tool"))
                        and isinstance(row.get("result"), dict)
                    )
                if not valid:
                    break
            valid = (
                valid
                and all(_cm_nonempty(item) for item in pending)
                and len(ids) == len(set(ids))
                and len(pending) == len(set(pending))
                and set(unexecuted) == set(pending)
            )
    else:
        return _cm_retry("RESULT_ENVELOPE_INVALID", "Choose envelope='native' or envelope='review' explicitly.", value)
    if not valid:
        return _cm_retry(
            "RESULT_ENVELOPE_INVALID",
            "Native results require decisionId/receipts/unexecuted; review results require source/data. "
            "Choose the documented envelope; use ordinary authored dictionaries directly.",
            value,
        )
    return value


def result_data(result: _CmAny, *, envelope: str = "native") -> dict[str, _CmAny] | list[_CmAny]:
    """Copy one confirmed native result, or explicit frozen-review data."""
    value = _cm_validated(result, envelope)
    if envelope == "review":
        if _cm_error(value["data"]):
            return _cm_retry(
                "RESULT_FEEDBACK_REQUIRED", "Inspect result_feedback(result=packet, envelope='review').", value
            )
        return value["data"]
    rows = value["receipts"]
    if (
        _cm_error(value)
        or value["unexecuted"]
        or any(row["status"] != "confirmed" or _cm_error(row.get("result")) for row in rows)
    ):
        return _cm_retry(
            "RESULT_FEEDBACK_REQUIRED",
            "Inspect result_feedback(result=packet) for errors and unexecuted work. Confirmed actions stay committed; "
            "do not repeat them to obtain a different projection.",
            value,
        )
    if len(rows) != 1:
        return _cm_retry(
            "RESULT_RECEIPT_AMBIGUOUS",
            "Data extraction requires exactly one confirmed receipt. Inspect result_feedback(result=packet) and "
            "select each intended receipt explicitly; never assume the first receipt is the desired result.",
            value,
        )
    return rows[0]["result"]


def result_feedback(result: _CmAny, *, envelope: str = "native") -> dict[str, _CmAny]:
    """Copy every receipt, rejection, unexecuted reference and review source."""
    return _cm_validated(result, envelope)


def result_view(result: _CmAny, *, envelope: str = "native") -> dict[str, _CmAny]:
    """Expose status/data/feedback without raising for ordinary tool refusals."""
    value = _cm_validated(result, envelope)
    status = "needs_recovery"
    data: _CmAny = None
    if envelope == "review":
        if not _cm_error(value) and not _cm_error(value["data"]):
            status, data = "confirmed", value["data"]
    elif not (
        _cm_error(value)
        or value["unexecuted"]
        or any(row["status"] != "confirmed" or _cm_error(row.get("result")) for row in value["receipts"])
    ):
        if len(value["receipts"]) == 1:
            status, data = "confirmed", value["receipts"][0]["result"]
        else:
            status = "select_receipt"
    return {"status": status, "data": data, "feedback": value}


def required_result_field(data: _CmAny, key: str) -> _CmAny:
    """Copy a required field; preserve present empty data and reject missing keys."""
    value = _cm_object(data)
    if not _cm_nonempty(key):
        return _cm_retry("RESULT_FIELD_INVALID", "Supply a nonempty field name from the documented result data.", value)
    if key not in value:
        return _cm_retry(
            "RESULT_FIELD_MISSING",
            "Required field " + _cm_short(key) + " is absent. Inspect the result shape; "
            "native data lives inside a receipt's result. A missing field is not an empty result.",
            value,
        )
    return value[key]


def result_entity(result: _CmAny, *, envelope: str = "native") -> dict[str, _CmAny]:
    """Copy the documented data.entity object of a delivered entity-detail read."""
    data = result_data(result=result, envelope=envelope)
    if not isinstance(data, dict):
        return _cm_retry("RESULT_ENTITY_INVALID", "Entity-detail results require an object at data.entity.", data)
    detail = required_result_field(data=data, key="data")
    if not isinstance(detail, dict):
        return _cm_retry("RESULT_ENTITY_INVALID", "Entity-detail results require an object at data.entity.", data)
    entity = required_result_field(data=detail, key="entity")
    if not isinstance(entity, dict):
        return _cm_retry("RESULT_ENTITY_INVALID", "Entity-detail results require an object at data.entity.", data)
    return entity
