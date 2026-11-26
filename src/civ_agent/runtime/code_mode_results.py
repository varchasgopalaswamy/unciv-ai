"""Pure result access shared by host callers and the worker-local Monty prelude.

Only real tools grant observation credit or effects. These projections neither
read nor authorize anything; authored dictionaries are used directly.
"""

from pathlib import Path
from typing import Literal

from pydantic import JsonValue
from pydantic_ai import ModelRetry

from civ_agent.protocol import JsonObject
from civ_agent.runtime import code_mode_local, code_mode_selectors_local

RESULT_HELPERS = frozenset({"result_data", "result_feedback", "result_view", "required_result_field", "result_entity"})
LOCAL_HELPER_VERSION = code_mode_local._cm_version
LOCAL_HELPER_SOURCE = Path(code_mode_local.__file__).read_text()
RESULT_SELECTORS = frozenset({"select_one", "movement_options", "receipt_result", "bundle_query"})
LOCAL_SELECTOR_VERSION = code_mode_selectors_local._cm_selector_version
LOCAL_SELECTOR_SOURCE = (
    Path(code_mode_selectors_local.__file__).read_text().split("# Worker-local selector source starts here.\n", 1)[1]
)
type Envelope = Literal["native", "review"]


def _project(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except (ValueError, TypeError, RecursionError) as error:
        raise ModelRetry(str(error)) from None


def result_data(result: JsonObject, *, envelope: Envelope = "native") -> dict[str, JsonValue] | list[JsonValue]:
    """Copy one confirmed native receipt result, or explicit review source/data.

    Native is the default and requires exactly one confirmed receipt with no
    error or unexecuted work. Use result_feedback(result=packet) for partial or
    multi-receipt results; this function never chooses an ambiguous first row.
    Frozen reads require envelope='review'. Empty data is valid. The helper
    creates no freshness, provenance, authority or inspection credit.
    """
    return _project(code_mode_local.result_data, result, envelope=envelope)


def result_feedback(result: JsonObject, *, envelope: Envelope = "native") -> JsonObject:
    """Copy the complete validated envelope, retaining receipts and all feedback.

    Native receipt results live at feedback['receipts'][index]['result']; frozen
    review data lives at feedback['data']. This never retries an action.
    """
    return _project(code_mode_local.result_feedback, result, envelope=envelope)


def result_view(result: JsonObject, *, envelope: Envelope = "native") -> JsonObject:
    """Copy a validated result as status/data/feedback for explicit branching.

    status='confirmed' exposes one native receipt's result or review data.
    status='needs_recovery' has data=None and keeps every original receipt,
    error and unexecuted operation in feedback. It does not raise for ordinary
    rejections or partial results. status='select_receipt' has data=None when
    a successful native envelope has zero or multiple receipts; select the
    intended operation explicitly from feedback. Malformed envelopes still
    produce correctable errors. No effects, retries or observation credit.
    """
    return _project(code_mode_local.result_view, result, envelope=envelope)


def required_result_field(data: JsonObject, key: str) -> JsonValue:
    """Copy a required dictionary field; distinguish missing from present empty.

    First unwrap the documented envelope and narrow its type:
        data = result_data(result=packet)
        assert isinstance(data, dict)
        required_result_field(data=data, key='militaryCompletion')
    Present {}, [], false, zero and null are retained. This does not unwrap.
    """
    return _project(code_mode_local.required_result_field, data, key)


def result_entity(result: JsonObject, *, envelope: Envelope = "native") -> JsonObject:
    """Copy a city_details/unit_details result's documented data.entity object.

    Use the original packet for receipts or review source labels. This checks
    the known entity-detail shape, never guesses another tool's payload shape.
    Missing data/entity is correctable feedback; a present empty entity is valid.
    """
    return _project(code_mode_local.result_entity, result, envelope=envelope)


def select_one(rows: list[JsonObject], *, where: JsonObject) -> JsonObject | None:
    """Copy the sole exact field match; return None for no match and reject ambiguity.

    Example: option = select_one(rows=moves, where={'destination': {'x': 2, 'y': 0}}).
    Check for None before using the full preserved option/command. Matching is
    type-sensitive with no ranking, coercion, missing-field defaults or effects.
    """
    return _project(code_mode_selectors_local.select_one, rows, where=where)


def movement_options(result: JsonObject, *, envelope: Envelope = "native") -> JsonObject:
    """Copy unit_details movement data as unit/movement/options plus original feedback.

    Read sections=['movement'] first. The native data.entity has scalar movement
    and a separate moves list. This returns the full moves as options, retaining
    command/context/confirmation fields; it neither selects nor executes a move.
    Frozen source/data requires envelope='review'. Missing moves is an error.
    """
    return _project(code_mode_selectors_local.movement_options, result, envelope=envelope)


def receipt_result(result: JsonObject, *, operation_id: str) -> JsonObject:
    """Copy an exact native operationId as receipt plus the entire original feedback.

    receipt is None if absent. Inspect its status before reading result; rejected
    and unexecuted receipts stay explicit, and confirmed actions stay committed.
    This does not treat another confirmed receipt as the intended operation.
    """
    return _project(code_mode_selectors_local.receipt_result, result, operation_id=operation_id)


def bundle_query(
    result: JsonObject, *, query_id: str | None = None, tool: str | None = None, arguments: JsonObject | None = None
) -> JsonObject:
    """Copy one inspect_bundle query plus the full receipt-preserving bundle.

    Choose query_id alone, or tool plus exact canonical arguments. Duplicate
    tool/arguments are ambiguous: use the returned queryId. query is None when
    absent. Inspect query.status/receipt; rejected, unexecuted and not_returned
    remain distinct. bundle.feedback retains every original receipt/source/error.
    """
    return _project(code_mode_selectors_local.bundle_query, result, query_id=query_id, tool=tool, arguments=arguments)
