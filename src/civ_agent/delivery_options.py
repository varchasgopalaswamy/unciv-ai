"""Transport recovery controls are independent of the requested information."""

from typing import Literal

from pydantic import Field, model_validator
from pydantic.json_schema import SkipJsonSchema

from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel

DELIVERY_FIELDS = {"delivery", "full"}


class DeliveryOptions(BoundaryModel):
    delivery: Literal["auto", "full"] = Field(
        default="auto",
        description="Transport only: auto normally; full recovers a missing or mismatched baseline. "
        "Use other arguments to select content.",
    )
    # Older clients and stored calls retain their original, replayable arguments.
    full: SkipJsonSchema[bool] = False

    @model_validator(mode="after")
    def consistent_delivery(self):
        if DELIVERY_FIELDS <= self.model_fields_set and self.full != (self.delivery == "full"):
            raise ValueError("Use delivery without the legacy full argument")
        return self


def full_delivery(arguments: JsonObject) -> bool:
    return arguments.get("delivery") == "full" or arguments.get("full") is True


def content_arguments(arguments: JsonObject) -> JsonObject:
    return {key: value for key, value in arguments.items() if key not in DELIVERY_FIELDS}
