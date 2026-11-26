"""Payload measurements against the same permitted native snapshots."""

import json
from collections import Counter

from civ_agent.observations import DetailQuery, project_detail
from civ_agent.proposal_gateway import public_result


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


TEMPLATE_FIELDS = {
    "command",
    "commands",
    "foundCityCommand",
    "foundCityConfirmationField",
    "stopCommand",
    "removeCommand",
    "moveCommands",
    "confirmationField",
    "inspection",
    "inspections",
    "destinationInspection",
}


def without_templates(value):
    if isinstance(value, dict):
        return {key: without_templates(part) for key, part in value.items() if key not in TEMPLATE_FIELDS}
    if isinstance(value, list):
        return [without_templates(part) for part in value]
    return value


def offered_commands(value):
    if isinstance(value, dict):
        for key, part in value.items():
            if key in TEMPLATE_FIELDS:
                for command in part if isinstance(part, list) else [part]:
                    if isinstance(command, dict) and "kind" in command:
                        yield command
            else:
                yield from offered_commands(part)
    elif isinstance(value, list):
        for part in value:
            yield from offered_commands(part)


def measure_selective(calls, snapshots):
    focused, named, matches, templates = [], Counter(), Counter(), []
    added_chars = 0
    for name, arguments, result in calls:
        if not result:
            continue
        if name == "player_command":
            choices = {
                k: v for k, v in arguments.items() if k not in {"intent", "confirmAction", "confirmBreakPromise"}
            }
            if choices in templates:
                matches[choices["kind"]] += 1
            continue
        if "error" in result:
            continue
        offered = list(offered_commands(result))
        if offered:
            templates.extend(offered)
            added_chars += len(encoded(result)) - len(encoded(without_templates(result)))
        if name in {"research_details", "construction_details", "policy_details"} and arguments.get("names"):
            named[name] += 1
        if name not in {"unit_details", "city_details"} or "all" in arguments.get("sections", ["all"]):
            continue
        query = DetailQuery.model_validate(result["query"])
        # Match the delivered projection to its permitted persisted snapshot; no hidden state is consulted.
        snapshot = next((s for s in snapshots if public_result(project_detail(s, query)) == result), None)
        assert snapshot is not None, "Cannot match focused read to its persisted permitted snapshot"
        full_query = DetailQuery(tool=name, arguments={"target": arguments["target"], "sections": ["all"]})
        full = public_result(project_detail(snapshot, full_query))
        focused.append(
            {
                "tool": name,
                "sections": query.arguments["sections"],
                "focusedChars": len(encoded(result)),
                "equivalentFullChars": len(encoded(full)),
            }
        )
    return {
        "focusedReads": focused,
        "namedReads": dict(named),
        "commandsMatchingDeliveredOffers": dict(matches),
        "commandTemplateAddedChars": added_chars,
        "focusedChars": sum(row["focusedChars"] for row in focused),
        "equivalentFullChars": sum(row["equivalentFullChars"] for row in focused),
    }
