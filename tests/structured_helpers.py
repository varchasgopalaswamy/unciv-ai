"""Scripted provider choices encoded against the actual captured wire schema."""

import json

from jsonschema import Draft202012Validator, ValidationError


def decision_text(schema, decision):
    def fill(shape, value):
        if "$ref" in shape:
            return fill(schema["$defs"][shape["$ref"].split("/")[-1]], value)
        if "anyOf" in shape:
            for variant in shape["anyOf"]:
                try:
                    candidate = fill(variant, value)
                    Draft202012Validator({**variant, "$defs": schema.get("$defs", {})}).validate(candidate)
                    return candidate
                except (ValueError, KeyError, TypeError, ValidationError):
                    continue
            raise ValueError("No fixture variant: " + str(value))
        if shape.get("type") == "object":
            if shape.get("properties", {}).keys() == {"value"}:
                return {"value": fill(shape["properties"]["value"], value)}
            if not isinstance(value, dict) or value.keys() - shape.get("properties", {}).keys():
                raise ValueError("Unknown fixture fields")
            return {
                key: fill(field, value[key]) if key in value else None
                for key, field in shape.get("properties", {}).items()
            }
        if shape.get("type") == "array":
            return [fill(shape["items"], item) for item in value]
        return value

    value = fill(schema, {"decision": decision})
    Draft202012Validator(schema).validate(value)
    return json.dumps(value)


def report_text(report, model=None):
    from civ_agent.decision_contracts import DecisionContract
    from civ_agent.strategic_review import StrategicAdvice

    contract = DecisionContract("consultation", [], report_model=model or StrategicAdvice)
    return contract.encode({"kind": "submit_report", "report": report})
