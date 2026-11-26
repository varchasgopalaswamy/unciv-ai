"""Explicit production intents and revision-bound references to individual queue entries."""

import hashlib
import json
from typing import Annotated, Any, Literal, cast

from pydantic import Field
from pydantic_ai import ModelRetry, RunContext, Tool

from civ_agent.schemas import BoundaryModel, NonnegativeInt


class QueueEntryReference(BoundaryModel):
    city: str
    queueRevision: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    index: NonnegativeInt
    name: str


class KeepConstruction(BoundaryModel):
    kind: Literal["keep_current"]
    city: str


class NewConstruction(BoundaryModel):
    kind: Literal["append_new", "start_new"]
    city: str
    selection: Annotated[str, Field(min_length=1)]


class ExistingConstruction(BoundaryModel):
    kind: Literal["prioritize_existing", "remove_existing"]
    city: str
    entry: QueueEntryReference


type ConstructionIntent = Annotated[
    KeepConstruction | NewConstruction | ExistingConstruction, Field(discriminator="kind")
]


def construction_state(city, queue, revision):
    identity = hashlib.sha256(json.dumps([city, revision, queue], separators=(",", ":")).encode()).hexdigest()
    return {
        "city": city,
        "queueRevision": identity,
        "queue": [
            {"city": city, "queueRevision": identity, "index": index, "name": name} for index, name in enumerate(queue)
        ],
    }


def observed_construction(ctx, intent):
    from civ_agent.runtime.player_observations import PlayerObservations

    observations = next(cap for cap in ctx.capabilities.values() if isinstance(cap, PlayerObservations))
    current = observations.overview or {}
    city = next((row for row in current.get("cities", []) if row["handle"] == intent.city), None)
    if city is None or not isinstance(city.get("constructionState"), dict):
        raise ModelRetry("Read player_turn for the city's current queue before choosing a construction intent.")
    state = city["constructionState"]
    if isinstance(intent, ExistingConstruction):
        entry = intent.entry.model_dump()
        if entry not in state["queue"] or intent.entry.city != intent.city:
            raise ModelRetry(
                "The queue reference is stale or belongs to another city. Inspect the queue and reconsider."
            )
    return state


def construction_tool(execute):
    def validate(ctx, intent):
        observed_construction(ctx, intent)

    async def construction_intent(ctx, intent):
        """Keep current production, append/start a NEW entry, or prioritize/remove an observed existing entry.

        Repeated units are distinct entries. Copy the exact constructionState.queue reference for existing entries.
        Inspect returned outcomes; unmet intent requires a new decision, never automatic undo or replay.
        """
        before = observed_construction(ctx, intent)
        if isinstance(intent, KeepConstruction):
            return {
                "constructionOutcome": {
                    "intent": intent.kind,
                    "nativeEffect": "none",
                    "queue": before["queue"],
                    "unmetIntendedOutcome": None,
                }
            }
        if isinstance(intent, NewConstruction):
            command = {
                "kind": "START_CONSTRUCTION" if intent.kind == "start_new" else "QUEUE_CONSTRUCTION",
                "target": intent.city,
                "selection": intent.selection,
            }
        else:
            command = {
                "kind": "MOVE_CONSTRUCTION" if intent.kind == "prioritize_existing" else "REMOVE_CONSTRUCTION",
                "target": intent.city,
                "index": intent.entry.index,
            }
            if intent.kind == "prioritize_existing":
                if intent.entry.index == 0:
                    return {
                        "constructionOutcome": {
                            "intent": intent.kind,
                            "nativeEffect": "already_current",
                            "queue": before["queue"],
                            "unmetIntendedOutcome": None,
                        }
                    }
                command["toIndex"] = 0
        result = await execute(ctx, command)
        receipt = next(
            (row["result"] for row in result.get("receipts", []) if row.get("tool") == "player_command"), result
        )
        native = receipt.get("construction")
        after = native.get("after") if native else None
        unmet = None
        if "error" in receipt or "error" in result:
            unmet = "Native command rejected; reconsider the reported reason."
        elif after is None:
            unmet = "The resulting queue is unavailable; inspect it before claiming the intended effect."
        elif intent.kind == "start_new" and (not after or after[0] != intent.selection):
            unmet = "The selected new entry did not become current production."
        elif intent.kind == "prioritize_existing" and (not after or after[0] != intent.entry.name):
            unmet = "The referenced entry did not become current production."
        elif intent.kind == "remove_existing" and len(before["queue"]) > 1 and len(after) >= len(before["queue"]):
            unmet = "The native queue does not confirm removal of the referenced entry."
        elif intent.kind == "append_new" and after.count(intent.selection) <= sum(
            row["name"] == intent.selection for row in before["queue"]
        ):
            unmet = "The native queue does not confirm a new entry for the selected construction."
        return {
            **result,
            "constructionOutcome": {
                "intent": intent.kind,
                "nativeEffect": native or receipt.get("actionSummary"),
                "queue": after,
                "unmetIntendedOutcome": unmet,
            },
        }

    construction_intent.__annotations__ = {"ctx": RunContext, "intent": ConstructionIntent, "return": dict}
    return Tool(cast(Any, construction_intent), takes_ctx=True, sequential=True, args_validator=validate, max_retries=2)
