"""Remove schema annotations without changing constraints or literal defaults."""

import copy

from civ_agent.protocol import JsonObject


def schema_brief(schema: JsonObject) -> JsonObject:
    result = copy.deepcopy(schema)
    result.pop("title", None)
    for field in ("properties", "$defs", "definitions", "patternProperties", "dependentSchemas"):
        if isinstance(result.get(field), dict):
            result[field] = {name: schema_brief(value) for name, value in result[field].items()}
    for field in ("items", "additionalProperties", "contains", "if", "then", "else", "not", "propertyNames"):
        if isinstance(result.get(field), dict):
            result[field] = schema_brief(result[field])
    for field in ("allOf", "anyOf", "oneOf", "prefixItems"):
        if isinstance(result.get(field), list):
            result[field] = [schema_brief(value) for value in result[field]]
    return result
