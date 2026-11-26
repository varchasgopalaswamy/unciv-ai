"""Strict schemas for data owned by this service, not extensible upstream events."""

import json
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError

from civ_agent.player_settings import valid_setting
from civ_agent.protocol import JsonObject


class BoundaryModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)


def _setting(value: str) -> str:
    if not valid_setting(value):
        raise ValueError("INVALID_SETTING")
    return value


type Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[!-~]+$")]
type PlayerToken = Annotated[str, Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]+$")]
type Setting = Annotated[str, Field(min_length=1, max_length=128), AfterValidator(_setting)]
type NonnegativeInt = Annotated[int, Field(ge=0)]
type Timeout = Annotated[int, Field(ge=1, le=86400)]


class StartRequest(BoundaryModel):
    activationId: Identifier
    epoch: Identifier
    civilization: Annotated[str, Field(min_length=1, max_length=128)]
    gameTurn: NonnegativeInt
    handoff: NonnegativeInt
    threadId: None
    mcpUrl: str
    playerToken: PlayerToken = Field(repr=False)
    prompt: Annotated[str, Field(min_length=1, max_length=1_000_000, repr=False)]
    model: Setting | None
    reasoningEffort: Setting | None = None
    timeoutSeconds: Timeout
    leaseSeconds: Annotated[int, Field(ge=1, le=300)]


class ConnectRequest(BoundaryModel):
    epoch: Identifier
    playerToken: PlayerToken = Field(repr=False)
    model: Setting | None
    reasoningEffort: Setting | None
    timeoutSeconds: Timeout


class InteractiveRequest(BoundaryModel):
    playerToken: PlayerToken = Field(repr=False)
    hostUrl: str
    model: Setting | None
    reasoningEffort: Setting | None


def validate[M: BoundaryModel](model: type[M], payload: object) -> M:
    """Never pass validation inputs, field names, or exception contexts to a caller."""
    try:
        return model.model_validate(payload)
    except ValidationError:
        raise ValueError("INVALID_ARGUMENT") from None


def argument_shape(shape: JsonObject, definitions: JsonObject) -> JsonObject:
    """Inline this argument's references, without unrelated union variants or titles."""
    if "$ref" in shape:
        return argument_shape(definitions[shape["$ref"].rsplit("/", 1)[1]], definitions)
    return {
        key: argument_shape(value, definitions)
        if isinstance(value, dict)
        else [argument_shape(item, definitions) if isinstance(item, dict) else item for item in value]
        if isinstance(value, list)
        else value
        for key, value in shape.items()
        if key not in {"title", "$defs"}
    }


def argument_issues(error: ValidationError, schema: JsonObject) -> list[JsonObject]:
    """Report error codes and schema-owned names, never invalid values or arbitrary keys."""

    def resolve(shape):
        while "$ref" in shape:
            shape = schema.get("$defs", {}).get(shape["$ref"].rsplit("/", 1)[-1], {})
        return shape

    def field_path(location):
        shape, fields = schema, []
        for part in location:
            shape = resolve(shape)
            mapping = shape.get("discriminator", {}).get("mapping", {})
            if isinstance(part, str) and part in mapping:
                shape = resolve({"$ref": mapping[part]})
                continue  # Pydantic inserts union tags, which are not field names.
            variants = shape.get("anyOf", shape.get("oneOf", []))
            if variants:
                shape = {}
                for variant in variants:
                    candidate = resolve(variant)
                    if (isinstance(part, int) and "items" in candidate) or (
                        isinstance(part, str) and part in candidate.get("properties", {})
                    ):
                        shape = candidate
                        break
            if isinstance(part, int) and "items" in shape:
                fields.append(part)
                shape = shape["items"]
            elif isinstance(part, str) and part in shape.get("properties", {}):
                fields.append(part)
                shape = shape["properties"][part]
            else:
                break
        return fields

    return [
        {"fields": field_path(issue["loc"]), "code": issue["type"]}
        for issue in error.errors(include_input=False, include_context=False, include_url=False)[:20]
    ]


def argument_feedback(error: ValidationError, schema: JsonObject, prefix: list | None = None) -> JsonObject:
    """Explain native validation constraints using counts/bounds, never rejected text."""
    issues = argument_issues(error, schema)
    limits, instructions = [], []
    errors = error.errors(include_input=True, include_context=True, include_url=False)
    for issue, detail in zip(issues, errors, strict=False):
        fields = [*(prefix or []), *issue["fields"]]
        label = ".".join(map(str, fields)) or "arguments"
        context = detail.get("ctx", {})
        value = detail.get("input")
        limit: JsonObject = {"fields": fields}
        code = detail["type"]
        if code in {"too_long", "too_short", "string_too_long", "string_too_short"}:
            upper = code.endswith("long")
            bound = context.get("max_length" if upper else "min_length")
            if not isinstance(bound, int):
                continue
            limit["maximum" if upper else "minimum"] = bound
            if isinstance(value, (str, list, dict, tuple)):
                limit["received"] = len(value)
            unit = "characters" if code.startswith("string_") else "items"
            instructions.append(f"{label} accepts {'at most' if upper else 'at least'} {bound} {unit}.")
        elif code in {"less_than", "less_than_equal", "greater_than", "greater_than_equal"}:
            upper = code.startswith("less")
            inclusive = code.endswith("equal")
            key = ("l" if upper else "g") + ("e" if inclusive else "t")
            bound = context.get(key)
            if not isinstance(bound, (int, float)):
                continue
            limit["maximum" if upper else "minimum"] = bound
            limit["inclusive"] = inclusive
            instructions.append(
                f"{label} must be {'<=' if upper and inclusive else '<' if upper else '>=' if inclusive else '>'} {bound}."
            )
        else:
            continue
        limits.append(limit)
    allowed, required = [], []

    def resolve(shape):
        if "$ref" in shape:
            return resolve(schema.get("$defs", {}).get(shape["$ref"].rsplit("/", 1)[-1], {}))
        return shape

    for issue in issues:
        shape = schema
        for field in issue["fields"]:
            shape = resolve(shape)
            variants = shape.get("anyOf", shape.get("oneOf", []))
            if variants:
                shape = next((resolve(v) for v in variants if field in resolve(v).get("properties", {})), {})
            shape = shape.get("items", {}) if isinstance(field, int) else shape.get("properties", {}).get(field, {})
        shape = resolve(shape)
        fields = [*(prefix or []), *issue["fields"]]
        options = shape.get("enum") or shape.get("propertyNames", {}).get("enum")
        if options:
            entry = {"fields": fields, "values": options}
            if entry not in allowed:
                allowed.append(entry)
            instructions.append(
                f"{'.'.join(map(str, fields))} accepts only these values or keys: {json.dumps(options)}."
            )
        if issue["code"] == "missing":
            required.append(fields)
            instructions.append(f"Supply required field {'.'.join(map(str, fields))}.")
    return {
        "invalidFields": issues,
        "limits": limits,
        "allowedValues": allowed,
        "requiredFields": required,
        "instruction": " ".join(instructions),
    }


def start_error(error: ValidationError) -> str:
    """Retain actionable service codes without exposing credentials or prompt text."""
    errors = error.errors(include_input=False, include_context=False, include_url=False)
    if any(item["type"] in {"missing", "extra_forbidden"} for item in errors):
        return "INVALID_ARGUMENT"
    codes = {
        "activationId": "INVALID_BINDING",
        "epoch": "INVALID_BINDING",
        "civilization": "INVALID_BINDING",
        "mcpUrl": "INVALID_MCP_ENDPOINT",
        "playerToken": "INVALID_PLAYER_TOKEN",
        "threadId": "INVALID_THREAD",
        "gameTurn": "INVALID_GAME_TURN",
        "handoff": "INVALID_GAME_TURN",
        "prompt": "INVALID_PROMPT",
        "model": "INVALID_MODEL",
        "reasoningEffort": "INVALID_REASONING_EFFORT",
        "timeoutSeconds": "INVALID_TIMEOUT",
        "leaseSeconds": "INVALID_TIMEOUT",
    }
    return codes.get(str(errors[0]["loc"][0]), "INVALID_ARGUMENT") if errors[0]["loc"] else "INVALID_ARGUMENT"
