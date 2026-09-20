"""Lossless result normalization for structured-capable model clients and history."""

import json
from typing import Any

RESULT_FORMAT_HEADER = "X-Civ-Result-Format"
STRUCTURED_RESULT_FORMAT = "structured"


def canonical_tool_result(result: Any) -> Any:
    """Remove only unannotated text blocks that duplicate structured JSON.

    Keep the MCP envelope, error status, metadata, and distinct content. Without
    structured content the original text/content remains the fallback. Never
    mutate the source result or conflate JSON booleans with numbers.
    """
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("structuredContent"), dict)
        or not isinstance(result.get("content"), list)
    ):
        return result
    structured = json.dumps(result["structuredContent"], sort_keys=True, ensure_ascii=False)
    content = []
    for block in result["content"]:
        if (
            isinstance(block, dict)
            and set(block) == {"type", "text"}
            and block["type"] == "text"
            and isinstance(block["text"], str)
        ):
            try:
                if json.dumps(json.loads(block["text"]), sort_keys=True, ensure_ascii=False) == structured:
                    continue
            except ValueError:
                pass
        content.append(block)
    return {**result, "content": content}
