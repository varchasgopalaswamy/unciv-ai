"""Stable role decisions: typed choices in, validated gateway operations out."""

import copy
import json
import threading
import time
from collections import OrderedDict
from types import MappingProxyType
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
    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise AttributeError("Compiled decision contract is immutable")
        object.__setattr__(self, name, value)

    @property
    def models(self):
        return self._models

    @property
    def definitions(self):
        return copy.deepcopy(self._definitions)

    @property
    def names(self):
        return self._names

    @property
    def recoverable(self):
        return self._recoverable

    @property
    def codec(self):
        return self._codec

    @property
    def schema(self):
        return copy.deepcopy(self._schema)

    def __init__(self, purpose, catalogue, *, report_model=None, compact=True):
        self.purpose = purpose
        self._models = {}
        self.report_model = report_model
        self._definitions = {}
        self._names = set()
        self._recoverable = set()
        inspections, actions = [], []
        for tool in sorted(catalogue, key=lambda t: t["name"]):
            name = tool["name"]
            if name in {"inspect_bundle", "command_help", "finish_role", "economy_wait"}:
                continue
            self._names.add(name)
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
                self._models[name] = None
            else:
                model = NATIVE_MODELS.get(name) or tool_model(name, purpose)
                self._models[name] = model
                shape = model.model_json_schema()
                if name == "chat_read" and "handledMessages" not in tool["inputSchema"]["properties"]:
                    # The gateway exposes acknowledgment only for routed Main
                    # gameplay. Retain that capability restriction on the wire.
                    shape["properties"].pop("handledMessages", None)
                if issubclass(model, DeliveryOptions):
                    self._recoverable.add(name)
                    inspection_arguments(shape)
                arguments = self._import(name, shape)
            operation = closed({"tool": tag(name), "arguments": arguments})
            if name in self._recoverable:
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
            self._models["finish_role"] = model
            variants.append(
                closed({"kind": tag("finish"), "arguments": self._import("finish", model.model_json_schema())})
            )
        if purpose == "gameplay":
            self._models["economy_wait"] = tool_model("economy_wait", purpose)
            variants.append(
                closed(
                    {
                        "kind": tag("wait"),
                        "arguments": self._import(
                            "wait",
                            self._models["economy_wait"].model_json_schema(),
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
        self._codec = StrictSchema(
            {**closed({"decision": {"anyOf": variants}}), "$defs": self._definitions}, compact=compact
        )
        self._schema = self._codec.schema
        self.fingerprint = self._codec.fingerprint
        self._models = MappingProxyType(self._models)
        self._names = frozenset(self._names)
        self._recoverable = frozenset(self._recoverable)
        self._sealed = True

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
            if key in self._definitions and self._definitions[key] != definition:
                raise UnsupportedSchema("Conflicting definition: " + key)
            self._definitions[key] = definition
        return schema

    def parse(self, text):
        value = self._codec.decode(json.loads(text))["decision"]
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
                        self._models[name].model_validate(arguments).model_dump(by_alias=True, exclude_unset=True)
                    )
                if name == "war_goals" and (arguments.get("action", "read") == "read") != (kind == "inspect"):
                    raise ValueError("Keep war_goals reads and actions in separate batches")
        elif kind in {"finish", "wait"}:
            name = "finish_role" if kind == "finish" else "economy_wait"
            value["arguments"] = (
                self._models[name].model_validate(value["arguments"]).model_dump(by_alias=True, exclude_unset=True)
            )
        elif kind == "submit_report":
            assert self.report_model is not None
            value["report"] = self.report_model.model_validate(value["report"]).model_dump()
        return value

    def encode(self, decision):
        """For test fixtures and deterministic host-authored examples, never model repair."""
        return encoded(self._codec.encode({"decision": decision}))

    def costs(self):
        value = encoded(self._schema)
        return {
            "fingerprint": self.fingerprint,
            "schemaCharacters": len(value),
            "schemaBytes": len(value.encode()),
            "sizeUnit": "characters/UTF-8 bytes, not tokens",
        }


_CONTRACT_CACHE_LIMIT = 64
_CONTRACT_CACHE = OrderedDict()
_CONTRACT_LOCK = threading.RLock()
_CONTRACT_METRICS = {"hits": 0, "misses": 0, "buildMs": 0.0, "failures": 0}


def compile_decision_contract(purpose, catalogue, *, report_model=None, compact=True, policy_key=None):
    """Reuse immutable compilation, never request handlers or delivered authority.

    The entire granted catalogue remains part of the key, including descriptions
    and input schemas. Fresh gateway catalogue checks still run at every dispatch.
    """
    canonical = encoded(sorted(catalogue, key=lambda tool: tool["name"]))
    report_schema = encoded(report_model.model_json_schema()) if report_model is not None else None
    key = (purpose, canonical, report_model, report_schema, compact, encoded(policy_key))
    with _CONTRACT_LOCK:
        if key in _CONTRACT_CACHE:
            _CONTRACT_METRICS["hits"] += 1
            _CONTRACT_CACHE.move_to_end(key)
            return _CONTRACT_CACHE[key]
        _CONTRACT_METRICS["misses"] += 1
        started = time.monotonic()
        try:
            result = DecisionContract(purpose, json.loads(canonical), report_model=report_model, compact=compact)
        except BaseException:
            _CONTRACT_METRICS["failures"] += 1
            raise
        finally:
            _CONTRACT_METRICS["buildMs"] += (time.monotonic() - started) * 1000
        _CONTRACT_CACHE[key] = result
        while len(_CONTRACT_CACHE) > _CONTRACT_CACHE_LIMIT:
            _CONTRACT_CACHE.popitem(last=False)
        return result


def decision_contract_cache_metrics():
    with _CONTRACT_LOCK:
        return {**_CONTRACT_METRICS, "entries": len(_CONTRACT_CACHE), "limit": _CONTRACT_CACHE_LIMIT}


def clear_decision_contract_cache():
    """Drop process-local compilation; no persisted or native state is changed."""
    with _CONTRACT_LOCK:
        _CONTRACT_CACHE.clear()
        _CONTRACT_METRICS.update(hits=0, misses=0, buildMs=0.0, failures=0)
