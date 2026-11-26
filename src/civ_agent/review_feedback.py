"""Bounded report correction guidance shared by review runtimes."""

from pydantic import ValidationError


def correction_message(error):
    """Give actionable feedback without echoing the report, private input or a traceback."""
    if isinstance(error, ValidationError):
        first = error.errors(include_input=False, include_url=False)[0]
        location = ".".join(map(str, first["loc"])) or "report"
        reason = f"{location}: {first['msg']}"
    else:
        reason = str(error) or type(error).__name__
        reason = {
            "REVIEW_OUTPUT_STALLED": "The report stopped making progress. Keep the corrected report concise.",
            "REVIEW_OUTPUT_LIMIT": "The report was too long. Keep the corrected report concise.",
            "REVIEW_PROCESS_FAILED": "The previous review was interrupted.",
        }.get(reason, reason)
        if reason.startswith(("STRATEGIC_REVIEW_PLAN: ", "STRATEGIC_REVIEW_SOURCE: ")):
            reason = reason.split(": ", 1)[1]
    return (
        "Please correct the previous review report. "
        + reason[:400]
        + " Use the observations already supplied and return one complete corrected report."
    )
