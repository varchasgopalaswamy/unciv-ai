"""Exact peace terms and typed advisory/decision contracts shared by runtimes."""

import copy

from pydantic import Field

from civ_agent.schemas import BoundaryModel


class TacticalOverview(BoundaryModel):
    goalAssessment: str = Field(min_length=1, max_length=4000)
    battlefield: str = Field(min_length=1, max_length=4000)
    continuingWar: str = Field(min_length=1, max_length=4000)
    peaceConsequences: str = Field(min_length=1, max_length=4000)
    uncertainties: str = Field(min_length=1, max_length=2000)
    recommendation: str = Field(min_length=1, max_length=2000)


class PeaceApproval(BoundaryModel):
    approve: bool
    reason: str = Field(min_length=1, max_length=3000)


def peace_terms(name, payload, selected=None):
    if (
        name == "player_command"
        and payload.get("kind") == "CITY_STATE_ACTION"
        and payload.get("selection") == "makePeace"
    ):
        return {k: payload[k] for k in ("kind", "target", "selection")}
    if name not in {"trade_offer", "trade_decide"} or name == "trade_decide" and payload.get("decision") != "accept":
        return None
    terms = selected if name == "trade_decide" else payload
    items = [*(terms or {}).get("give", []), *(terms or {}).get("receive", [])]
    items.extend(clause["item"] for clause in (terms or {}).get("clauses", []))
    if not any(item.get("type") == "PeaceProposal" or item.get("name") == "Peace Treaty" for item in items):
        return None
    return {
        k: copy.deepcopy((terms or {}).get(k))
        for k in ("author", "partner", "give", "receive", "clauses", "proposal", "version", "nativeRequestId")
    }
