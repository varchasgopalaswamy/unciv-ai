"""Expose already delivered opening facts locally, with no new observation."""

import json

from pydantic_ai import Tool
from pydantic_ai_harness.code_mode import CodeModeToolset

from civ_agent.protocol import JsonObject
from civ_agent.runtime.player_observations import PlayerObservations

CONTEXT_HELPERS = frozenset({"delivered_context"})
CONTEXT_VERSION = "delivered-opening-v1"


def delivered_context() -> JsonObject:
    """Copy this run's exact delivered opening context, including sources/revisions.

    This historical snapshot creates no new read, freshness or inspection credit.
    Reads and decisionDetails retain their original schemas, scope and omissions.
    Obtain current native evidence when a dependent action needs a newer basis.
    Local mutation changes only the returned copy. A new run starts empty.
    """
    raise ValueError("DELIVERED_CONTEXT_UNAVAILABLE: call only inside the enabled Code Mode run")


def context_catalogue() -> str:
    definition = Tool(delivered_context).tool_def
    return CodeModeToolset._render_catalog({definition.name: definition})


def context_source(ctx) -> str:
    snapshot = None
    for capability in ctx.capabilities.values():
        if isinstance(capability, PlayerObservations):
            snapshot = capability.delivered_context(ctx)
            break
    body = (
        "    raise ValueError('DELIVERED_CONTEXT_UNAVAILABLE: the opening was not delivered in this run; inspect current permitted facts.')"
        if snapshot is None
        else "    return _cm_json.loads(" + repr(json.dumps(snapshot, ensure_ascii=True, allow_nan=False)) + ")"
    )
    return "def delivered_context() -> dict[str, _CmAny]:\n" + body + "\n"
