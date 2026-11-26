"""Deterministic native feedback and receipt-grounded structured effect claims."""

from dataclasses import dataclass, field, replace
from typing import Any

from pydantic import Field
from pydantic_ai import ModelRetry
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ToolReturn

from civ_agent.decision_contracts import READS
from civ_agent.schemas import BoundaryModel


def receipt_kind(receipt):
    return receipt.get("result", {}).get("kind") if receipt.get("tool") == "player_command" else receipt.get("tool")


class EffectClaim(BoundaryModel):
    operationId: str
    kind: str = Field(description="Copy actionOutcomes.kind exactly: the command kind or action tool name.")
    # A summary remains authored prose. The assertion of an effect is grounded
    # in the exact confirmed operation/kind, not an NLP judgment of that prose.
    summary: str = Field(min_length=1, max_length=600)


@dataclass
class VerifiedOutcomes(AbstractCapability[Any]):
    id: str | None = "verified-native-outcomes-v1"
    receipts: dict = field(default_factory=dict, init=False)

    async def for_run(self, ctx):
        return type(self)()

    def validate(self, claims):
        for claim in claims:
            receipt = self.receipts.get(claim["operationId"])
            if (
                receipt is None
                or receipt.get("status") != "confirmed"
                or receipt["result"].get("error")
                or receipt_kind(receipt) != claim["kind"]
            ):
                raise ModelRetry(
                    "An effect claim lacks a confirmed native receipt of that kind in this run. Inspect outcomes and correct the claim; do not repeat committed actions."
                )

    async def after_tool_execute(self, ctx, *, call, tool_def, args, result):
        value = result.return_value if isinstance(result, ToolReturn) else result
        if not isinstance(value, dict) or call.tool_name not in {"execute_commands", "act", "construction_intent"}:
            return result
        outcomes = []
        for row in value.get("receipts", []):
            if not row.get("tool") or row["tool"] in READS:
                continue
            self.receipts[row["operationId"]] = row
            native = row.get("result", {})
            outcome = {
                "operationId": row["operationId"],
                "status": row["status"],
                "kind": receipt_kind(row),
                **{key: native[key] for key in ("actionSummary", "construction", "movement", "error") if key in native},
            }
            movement = native.get("movement") or {}
            if (
                movement.get("requestedDestination") is not None
                and movement.get("end") != movement["requestedDestination"]
            ):
                outcome["unmetIntendedOutcome"] = (
                    "Movement stopped before the requested destination; reconsider current facts."
                )
            outcomes.append(outcome)
        if not outcomes:
            return result
        updated = {
            **value,
            "actionOutcomes": outcomes,
            "outcomeInstruction": "Confirmed effects stay committed. Reconsider partial or unmet outcomes; never automatically undo or repeat them.",
        }
        return replace(result, return_value=updated) if isinstance(result, ToolReturn) else updated
