"""Keep native provider error types without retaining provider diagnostics."""

from math import isfinite

from pydantic_ai.exceptions import (
    ModelAPIError,
    ModelConnectionError,
    ModelHTTPError,
    ModelQuotaExceededError,
)

# Only identifiers that influence supported provider policy cross the boundary.
# An unknown code remains unknown; provider prose never determines a category.
MACHINE_IDENTIFIERS = frozenset(
    {
        "authentication_error",
        "billing_hard_limit_reached",
        "context_length_exceeded",
        "credit_balance_exhausted",
        "insufficient_quota",
        "invalid_api_key",
        "invalid_prompt",
        "invalid_request_error",
        "model_not_found",
        "organization_spend_limit_exceeded",
        "organization_usage_limit_exceeded",
        "overloaded",
        "overloaded_error",
        "permission_error",
        "project_spend_limit_exceeded",
        "rate_limit_error",
        "rate_limit_exceeded",
        "requests",
        "server_error",
        "server_is_overloaded",
        "service_unavailable",
        "service_unavailable_error",
        "slow_down",
        "tokens",
        "too_many_requests",
        "usage_limit_reached",
        "usage_not_included",
    }
)


def machine_identifier(value: object) -> str | None:
    return value if isinstance(value, str) and value in MACHINE_IDENTIFIERS else None


def nonnegative_seconds(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        seconds = float(value)
    except (OverflowError, ValueError):
        return None
    return seconds if isfinite(seconds) and seconds >= 0 else None


def sanitize_model_error(error: ModelAPIError, *, system: str, model_name: str) -> ModelAPIError:
    """Mutate public fields, retaining native subtype and SDK-attached accounting.

    The caller raises the returned error ``from None`` so the provider SDK's
    original exception, request and response never enter the durable failure.
    """
    body = error.body
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        body = body["error"]
    reset: dict[str, int | float] = {}
    if isinstance(body, dict):
        epoch = body.get("resets_at")
        if type(epoch) is int and 0 <= epoch <= 253402300799:
            reset["resets_at"] = epoch
        seconds = nonnegative_seconds(body.get("resets_in_seconds"))
        if seconds is not None:
            reset["resets_in_seconds"] = seconds
    code = machine_identifier(error.provider_error_code)
    error_type = machine_identifier(error.provider_error_type)
    retry_after = nonnegative_seconds(error.retry_after)
    in_stream = error.in_stream is not False
    hint = error.headers.get("x-should-retry") if isinstance(error, ModelHTTPError) and error.headers else None
    hint = hint.strip().lower() if isinstance(hint, str) else None
    if (
        system == "openai-codex"
        and isinstance(error, ModelHTTPError)
        and error.status_code == 429
        and "usage_limit_reached" in {code, error_type}
    ):
        replacement = ModelHTTPError.for_category(
            ModelQuotaExceededError,
            status_code=error.status_code,
            model_name=model_name,
            provider_error_code=code,
            provider_error_type=error_type,
            retry_after=retry_after,
            in_stream=in_stream,
        )
        # Native errors' Python serialization protocol retains the SDK's
        # attached partial-response/accounting state as well as public fields.
        replacement.__setstate__(error.__getstate__())
        error = replacement
    if isinstance(error, ModelHTTPError):
        error.headers = {"x-should-retry": hint} if hint in {"true", "false"} else None
        error.hint, error.suggested_model_id = None, None
    if isinstance(error, ModelConnectionError) and error.phase not in {"pool", "connect", "write", "read"}:
        error.phase = None
    error.model_name = model_name
    error.body = reset or None
    error.provider_error_code, error.provider_error_type = code, error_type
    error.retry_after, error.in_stream = retry_after, in_stream
    error.message = "MODEL_PROVIDER_ERROR"
    error.args = (error.message,)
    error.__cause__, error.__context__ = None, None
    return error
