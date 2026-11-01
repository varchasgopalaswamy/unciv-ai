"""Lossless compilation of owned contracts to the provider's strict subset.

Null means omission for an optional field. An optional field which itself accepts
null uses {"value": ...} to distinguish setting/clearing from omission. Defaults
are applied only by the canonical validator, never by the wire codec.
"""

import copy
import hashlib
import json

from jsonschema import Draft202012Validator, FormatChecker

from civ_agent.protocol import JsonObject


def encoded(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def fingerprint(value: object) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


class UnsupportedSchema(ValueError):
    pass


SUPPORTED = {
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "anyOf",
    "$ref",
    "$defs",
    "enum",
    "const",
    "description",
    "title",
    "default",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "pattern",
    "format",
    "minItems",
    "maxItems",
}


class StrictSchema:
    def __init__(self, schema: JsonObject, *, compact: bool = True):
        self.original = copy.deepcopy(schema)
        self.definitions = self.original.pop("$defs", {})
        self.compact = compact
        self.compiled_defs: JsonObject = {}
        result = self._compile(self.original)
        if result.get("type") != "object" or "anyOf" in result:
            raise UnsupportedSchema("The decision root must be an object, with unions nested in properties")
        if self.compiled_defs:
            # The codec keeps canonical reference names. Wire deduplication
            # must not mutate that graph, or decoding can reinsert aliases and
            # change the supposedly stable provider schema on later turns.
            result["$defs"] = copy.deepcopy(self.compiled_defs)
        self.schema = self._deduplicate(result) if compact else result
        self.validator = Draft202012Validator(self.schema, format_checker=FormatChecker())
        Draft202012Validator.check_schema(self.schema)
        self.fingerprint = fingerprint(self.schema)

    def _resolve(self, shape):
        seen = set()
        while "$ref" in shape:
            if shape["$ref"] in seen:
                raise UnsupportedSchema("Reference-only cycles are not supported")
            seen.add(shape["$ref"])
            shape = self.definitions[shape["$ref"].removeprefix("#/$defs/")]
        return shape

    def _nullable(self, shape):
        shape = self._resolve(shape)
        return shape.get("type") == "null" or any(self._nullable(s) for s in shape.get("anyOf", []))

    @staticmethod
    def _map(shape):
        """An enum-keyed map is a closed object, not an arbitrary JSON escape."""
        if "propertyNames" not in shape:
            return shape
        keys = shape["propertyNames"]
        if set(keys) - {"enum", "type"} or not isinstance(keys.get("enum"), list):
            raise UnsupportedSchema("Only finite enum map keys are supported")
        value = shape["additionalProperties"]
        if not isinstance(value, dict):
            raise UnsupportedSchema("Map values require a contract")
        return {
            **{k: v for k, v in shape.items() if k not in {"propertyNames", "additionalProperties"}},
            "properties": {key: value for key in keys["enum"]},
            "required": [],
            "additionalProperties": False,
        }

    def _compile(self, raw):
        shape = self._map(copy.deepcopy(raw))
        if "oneOf" in shape:
            # Only discriminated, disjoint tagged unions are equivalent to anyOf.
            if "discriminator" not in shape:
                raise UnsupportedSchema("oneOf requires an explicit discriminator")
            discriminator = shape["discriminator"].get("propertyName")
            tags = set()
            optional_tags = 0
            for variant in shape["oneOf"]:
                resolved = self._resolve(variant)
                field = resolved.get("properties", {}).get(discriminator, {})
                values = field.get("enum", [field["const"]] if "const" in field else [])
                optional_tags += discriminator not in resolved.get("required", [])
                if optional_tags > 1 or not values or tags.intersection(values):
                    raise UnsupportedSchema("Discriminated oneOf variants must have disjoint tags")
                tags.update(values)
            shape["anyOf"] = shape.pop("oneOf")
            shape.pop("discriminator")
            shape.pop("type", None)  # RootModel's object annotation is redundant.
        unknown = set(shape) - SUPPORTED
        if unknown:
            raise UnsupportedSchema("Unsupported schema keywords: " + ", ".join(sorted(unknown)))
        if "type" in shape and not isinstance(shape["type"], str):
            raise UnsupportedSchema("Express nullable types as explicit anyOf alternatives")
        if "format" in shape and shape["format"] not in {
            "date-time",
            "time",
            "date",
            "duration",
            "email",
            "hostname",
            "ipv4",
            "ipv6",
            "uuid",
        }:
            raise UnsupportedSchema("Unsupported string format")
        if "format" in shape and shape["format"] not in FormatChecker.checkers:
            raise UnsupportedSchema("No runtime validator for this string format")
        if not {"type", "$ref", "anyOf"} & shape.keys():
            raise UnsupportedSchema("Every subschema requires an explicit type, union or reference")
        if "anyOf" in shape and "properties" in shape:
            raise UnsupportedSchema("Object intersections must be expressed as complete union variants")
        if "$ref" in shape:
            if set(shape) - {"$ref", "description", "title", "default"}:
                raise UnsupportedSchema("Reference intersections require an explicit supported contract")
            ref = shape["$ref"]
            if not ref.startswith("#/$defs/") or ref[8:] not in self.definitions:
                raise UnsupportedSchema("Only local, defined references are supported")
            name = ref[8:]
            if name not in self.compiled_defs:
                self.compiled_defs[name] = {}  # recursion guard
                self.compiled_defs[name] = self._compile(self.definitions[name])
        shape.pop("default", None)
        if self.compact:
            shape.pop("title", None)
        if "const" in shape:
            shape["enum"] = [shape.pop("const")]
        if shape.get("type") == "object" and "anyOf" in shape and "properties" not in shape:
            shape.pop("type")  # Every alternative supplies its own closed object.
        if shape.get("type") == "object":
            if shape.get("additionalProperties") is not False:
                raise UnsupportedSchema("Objects must be closed")
            properties = shape.get("properties", {})
            required = shape.get("required", [])
            compiled = {}
            for key, field in properties.items():
                value = self._compile(field)
                if key not in required:
                    if self._nullable(field):
                        value = {
                            "type": "object",
                            "properties": {"value": value},
                            "required": ["value"],
                            "additionalProperties": False,
                        }
                    value = {"anyOf": [value, {"type": "null"}]}
                compiled[key] = value
            shape.update(properties=compiled, required=list(compiled))
        if "items" in shape:
            shape["items"] = self._compile(shape["items"])
        if "anyOf" in shape:
            shape["anyOf"] = [self._compile(s) for s in shape["anyOf"]]
        return shape

    @staticmethod
    def _deduplicate(schema):
        definitions = schema.pop("$defs", {})
        identities = {}
        replacements = {}
        # Canonicalize referenced shapes too; aliases collapse in subsequent passes.
        while True:
            changed = False
            for key, shape in list(definitions.items()):
                signature = encoded(shape)
                digest = fingerprint(shape)
                if digest in identities:
                    previous, exact = identities[digest]
                    if exact != signature:
                        raise UnsupportedSchema("Schema fingerprint collision")
                    if previous != key:
                        replacements["#/$defs/" + key] = "#/$defs/" + previous
                        del definitions[key]
                        changed = True
                else:
                    identities[digest] = key, signature

            def replace(value):
                if isinstance(value, dict):
                    for k, v in value.items():
                        if k == "$ref":
                            while v in replacements:
                                v = replacements[v]
                            value[k] = v
                        else:
                            replace(v)
                elif isinstance(value, list):
                    for v in value:
                        replace(v)

            replace(schema)
            replace(definitions)
            if not changed:
                break
            identities.clear()
        if definitions:
            schema["$defs"] = definitions
        return schema

    def _convert(self, raw, value, *, decode) -> object:
        shape = self._map(self._resolve(raw))
        variants = shape.get("anyOf", shape.get("oneOf"))
        if variants:
            for variant in variants:
                candidate = self._compile(variant) if decode else variant
                definitions = self.compiled_defs if decode else self.definitions
                if Draft202012Validator({**candidate, "$defs": definitions}).is_valid(value):
                    return self._convert(variant, value, decode=decode)
            raise ValueError("No matching union variant")
        if shape.get("type") == "object":
            result = {}
            for key, field in shape.get("properties", {}).items():
                optional = key not in shape.get("required", [])
                if decode and optional and value[key] is None:
                    continue
                if not decode and key not in value:
                    if not optional:
                        raise ValueError("Required field missing: " + key)
                    result[key] = None
                    continue
                part = value[key]
                wrapped = optional and self._nullable(field)
                if decode and wrapped:
                    part = part["value"]
                part = self._convert(field, part, decode=decode)
                result[key] = {"value": part} if wrapped and not decode else part
            return result
        if shape.get("type") == "array":
            return [self._convert(shape["items"], item, decode=decode) for item in value]
        return value

    def decode(self, value) -> JsonObject:
        self.validator.validate(value)
        result = self._convert(self.original, value, decode=True)
        assert isinstance(result, dict)
        return result

    def encode(self, value) -> JsonObject:
        result = self._convert(self.original, value, decode=False)
        self.validator.validate(result)
        assert isinstance(result, dict)
        return result
