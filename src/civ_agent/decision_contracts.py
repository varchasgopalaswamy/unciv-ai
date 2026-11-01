"""Stable role decisions: typed choices in, validated gateway operations out."""

import copy
import json
from typing import Annotated

from pydantic import Field

from civ_agent.delivery_options import DeliveryOptions
from civ_agent.game_commands import command_arguments
from civ_agent.memory_records import Intent
from civ_agent.military_completion import MilitaryDisposition
from civ_agent.schemas import BoundaryModel
from civ_agent.strict_schema import StrictSchema, UnsupportedSchema, encoded
from civ_agent.tool_schemas import SpecialistFinishRole, tool_model
from civ_agent.victory_snapshot import VictoryQuery

DECISION_INSTRUCTIONS = (
    "Return one structured decision matching the supplied schema. The host executes validated decisions. "
    "inspect and act contain 1–12 independent operations with all arguments already decided. Keep reads and "
    "actions separate. Never submit a program, conditional, loop, or reference to an unseen result. "
    "Actions execute in order and stop when reconsideration is needed; inspect each receipt before continuing. "
    "Never repeat a confirmed action. wait and finish are standalone. Use submit_report in report phases. "
    "Include every required key in the selected schema variant. To leave an optional argument unspecified, "
    "include its key with null; the host omits that argument when executing the operation. Otherwise supply its value. "
    "Do not drop required keys. An optional argument that itself accepts null uses {value: ...} to set it; "
    "{value: null} explicitly clears it. Optional memory patch fields set to null remain unchanged. "
    "Emit compact JSON without whitespace outside quoted strings. "
    "Observation revisions and military review bases are bound by the host to delivered context. "
    "For military completion, retrieve all remaining review pages and explain dispositions for their targets; "
    "zero remaining targets needs no review token. Social promises remain exact, nonbinding authored text."
    " Inspections always check current information. The host sends full data, lossless changes, or an unchanged "
    "receipt as appropriate. Leave recoverBaseline null normally; use missing or mismatched only when you "
    "cannot reconstruct a prior baseline. This recovers transport data, not fresher facts or additional content."
)

READS = frozenset(
    {
        "player_turn",
        "player_capabilities",
        "player_result",
        "research_details",
        "policy_details",
        "victory_progress",
        "city_details",
        "unit_details",
        "construction_details",
        "map_region",
        "player_details",
        "movement_preview",
        "mission_preview",
        "player_inspect",
        "memory_read",
        "memory_search",
        "history_read",
        "chat_read",
        "chat_history",
        "trade_options",
        "trade_prepare",
        "trade_read",
        "agreement_list",
        "agreement_read",
        "native_advice",
        "military_review",
    }
)


class NamedOptions(BoundaryModel):
    names: list[Annotated[str, Field(min_length=1, max_length=256)]] = Field(default_factory=list, max_length=16)


class MilitaryFinish(SpecialistFinishRole):
    decisions: list[MilitaryDisposition] = Field(default_factory=list, max_length=1000)


NATIVE_MODELS = {
    "research_details": NamedOptions,
    "policy_details": NamedOptions,
    "victory_progress": VictoryQuery,
    "player_capabilities": BoundaryModel,
    "player_result": BoundaryModel,
}


def closed(properties, required=None):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties) if required is None else required,
        "additionalProperties": False,
    }


def tag(value):
    return {"type": "string", "const": value}


def inspection_arguments(shape):
    """Keep content contracts intact while moving delivery to the operation."""
    if "properties" in shape:
        shape["properties"].pop("delivery")
    elif "anyOf" in shape:
        for variant in shape["anyOf"]:
            inspection_arguments(variant)
    else:
        raise UnsupportedSchema("Unsupported inspection delivery contract")


class DecisionContract:
    def __init__(self, purpose, catalogue, *, report_model=None, compact=True):
        self.purpose = purpose
        self.models = {}
        self.report_model = report_model
        self.definitions = {}
        self.names = set()
        self.recoverable = set()
        inspections, actions = [], []
        for tool in sorted(catalogue, key=lambda t: t["name"]):
            name = tool["name"]
            if name in {"inspect_bundle", "command_help", "finish_role", "economy_wait"}:
                continue
            self.names.add(name)
            if name == "player_command":
                command = copy.deepcopy(tool["inputSchema"])
                # Metadata is shared outside the command union on the model wire.
                for shape in command.get("$defs", {}).values():
                    for key in ("intent", "reviewAfter"):
                        shape.get("properties", {}).pop(key, None)
                body = self._import(name, command)
                arguments = closed(
                    {
                        "command": body,
                        "intent": self._import("intent", Intent.model_json_schema()),
                        **({"reviewAfter": {"type": "boolean"}} if purpose == "military" else {}),
                    },
                    ["command"],
                )
                self.models[name] = None
            else:
                model = NATIVE_MODELS.get(name) or tool_model(name, purpose)
                self.models[name] = model
                shape = model.model_json_schema()
                if name == "chat_read" and "handledMessages" not in tool["inputSchema"]["properties"]:
                    # The gateway exposes acknowledgment only for routed Main
                    # gameplay. Retain that capability restriction on the wire.
                    shape["properties"].pop("handledMessages", None)
                if issubclass(model, DeliveryOptions):
                    self.recoverable.add(name)
                    inspection_arguments(shape)
                arguments = self._import(name, shape)
            operation = closed({"tool": tag(name), "arguments": arguments})
            if name in self.recoverable:
                operation["properties"]["recoverBaseline"] = {
                    "type": "string",
                    "enum": ["missing", "mismatched"],
                    "description": "Use null for normal fresh inspections. Use missing or mismatched only to recover a transport baseline.",
                }
            if tool.get("description"):
                operation["description"] = tool["description"]
            if name == "war_goals":
                # Read and mutation variants of a coordination surface stay separate.
                read = copy.deepcopy(operation)
                read["properties"]["arguments"] = self._import(
                    "read_war_goals", tool_model(name, "economy").model_json_schema()
                )
                inspections.append(read)
                if purpose in {"gameplay", "military"}:
                    mutation = operation["properties"]["arguments"]
                    read_ref = mutation["discriminator"]["mapping"].pop("read")
                    mutation["oneOf"] = [variant for variant in mutation["oneOf"] if variant.get("$ref") != read_ref]
                    actions.append(operation)
            else:
                (inspections if name in READS else actions).append(operation)
        variants = []
        for kind, operations in (("inspect", inspections), ("act", actions)):
            if operations:
                variants.append(
                    closed(
                        {
                            "kind": tag(kind),
                            "operations": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 12,
                                "items": {"anyOf": operations},
                            },
                        }
                    )
                )
        if purpose in {"gameplay", "economy", "military", "communication"}:
            model = (
                MilitaryFinish
                if purpose == "military"
                else (BoundaryModel if purpose == "communication" else tool_model("finish_role", purpose))
            )
            self.models["finish_role"] = model
            variants.append(
                closed({"kind": tag("finish"), "arguments": self._import("finish", model.model_json_schema())})
            )
        if purpose == "gameplay":
            self.models["economy_wait"] = tool_model("economy_wait", purpose)
            variants.append(
                closed(
                    {
                        "kind": tag("wait"),
                        "arguments": self._import(
                            "wait",
                            self.models["economy_wait"].model_json_schema(),
                        ),
                    }
                )
            )
        if report_model is not None:
            variants.append(
                closed(
                    {
                        "kind": tag("submit_report"),
                        "report": self._import(
                            "report",
                            report_model.model_json_schema(),
                        ),
                    }
                )
            )
        self.codec = StrictSchema(
            {**closed({"decision": {"anyOf": variants}}), "$defs": self.definitions}, compact=compact
        )
        self.schema = self.codec.schema
        self.fingerprint = self.codec.fingerprint

    def _import(self, prefix, schema):
        schema = copy.deepcopy(schema)
        definitions = schema.pop("$defs", {})

        def rename(value):
            if isinstance(value, dict):
                for key, part in value.items():
                    if key == "$ref":
                        value[key] = "#/$defs/" + prefix + "_" + part.removeprefix("#/$defs/")
                    elif key == "discriminator":
                        part["mapping"] = {
                            k: "#/$defs/" + prefix + "_" + v.removeprefix("#/$defs/")
                            for k, v in part["mapping"].items()
                        }
                    else:
                        rename(part)
            elif isinstance(value, list):
                for part in value:
                    rename(part)

        rename(schema)
        for name, definition in definitions.items():
            rename(definition)
            key = prefix + "_" + name
            if key in self.definitions and self.definitions[key] != definition:
                raise UnsupportedSchema("Conflicting definition: " + key)
            self.definitions[key] = definition
        return schema

    def parse(self, text):
        value = self.codec.decode(json.loads(text))["decision"]
        kind = value["kind"]
        if kind in {"inspect", "act"}:
            # Validate every operation, including semantic validators, before dispatch.
            for operation in value["operations"]:
                name, arguments = operation["tool"], operation["arguments"]
                if name == "player_command":
                    arguments = {**arguments.pop("command"), **arguments}
                    operation["arguments"] = command_arguments(arguments)
                else:
                    if operation.pop("recoverBaseline", None) is not None:
                        arguments["delivery"] = "full"
                    operation["arguments"] = (
                        self.models[name].model_validate(arguments).model_dump(by_alias=True, exclude_unset=True)
                    )
                if name == "war_goals" and (arguments.get("action", "read") == "read") != (kind == "inspect"):
                    raise ValueError("Keep war_goals reads and actions in separate batches")
        elif kind in {"finish", "wait"}:
            name = "finish_role" if kind == "finish" else "economy_wait"
            value["arguments"] = (
                self.models[name].model_validate(value["arguments"]).model_dump(by_alias=True, exclude_unset=True)
            )
        elif kind == "submit_report":
            assert self.report_model is not None
            value["report"] = self.report_model.model_validate(value["report"]).model_dump()
        return value

    def encode(self, decision):
        """For test fixtures and deterministic host-authored examples, never model repair."""
        return encoded(self.codec.encode({"decision": decision}))

    def costs(self):
        value = encoded(self.schema)
        return {
            "fingerprint": self.fingerprint,
            "schemaCharacters": len(value),
            "schemaBytes": len(value.encode()),
            "sizeUnit": "characters/UTF-8 bytes, not tokens",
        }
