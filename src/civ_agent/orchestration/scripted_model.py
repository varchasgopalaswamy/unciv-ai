"""Credential-free integration driver. Decisions use only model-delivered messages."""

import json
import re

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel

from civ_agent.orchestration.contracts import canonical
from civ_agent.response_compaction import expand_response

STORE_TASK_PREFIX = "Native stores fixture: ordinary role work"
STORE_NOTE_PREFIX = "Native stores fixture note"
STORE_CITY_REASON = (
    "Native stores fixture: record nonbinding construction intent; choose actions from permitted native facts."
)


def store_task(startup):
    view = startup.get("reads", {}).get("player_turn", {})
    label = startup.get("nativeStoreJob", "turn-" + str(view.get("turn", "off-turn")))
    return f"{STORE_TASK_PREFIX} ({startup['role']}, {label})."


def store_results(messages, calls):
    """Read actual SDK success/retry parts; a model's request proves no write."""
    results = []
    for message in messages:
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if not isinstance(part, ToolReturnPart | RetryPromptPart) or part.tool_call_id not in calls:
                continue
            invocation = calls[part.tool_call_id]
            if isinstance(part, ToolReturnPart) and part.outcome != "success":
                continue
            results.append((invocation.tool_name, invocation.args_as_dict(), part))
    return results


def store_plan_read(results, task):
    for name, _, part in results:
        if name != "read_plan" or not isinstance(part, ToolReturnPart) or not isinstance(part.content, str):
            continue
        for line in part.content.splitlines():
            match = re.fullmatch(r"\d+\. \[[^]]*\] \[([^]]+)\] (.+)", line)
            if match and match.group(2) == task:
                return match.group(1)
    return None


def scripted_store_setup(startup, messages, calls, call, *, typed_plans=False, view=None):
    results, task = store_results(messages, calls), store_task(startup)
    written = any(
        name == "write_plan"
        and isinstance(part, ToolReturnPart)
        and arguments.get("items") == [{"content": task, "status": "in_progress"}]
        for name, arguments, part in results
    )
    if not written:
        return call("write_plan", {"items": [{"content": task, "status": "in_progress"}]})
    if store_plan_read(results, task) is None:
        if any(name == "read_plan" and isinstance(part, ToolReturnPart) for name, _, part in results):
            raise ValueError("SCRIPTED_NATIVE_PLAN_NOT_DELIVERED")
        return call("read_plan", {})
    role = startup["role"]
    if view is None:
        view = startup.get("reads", {}).get("player_turn", {})
    marker = (
        f"{STORE_NOTE_PREFIX}: {role}, {startup.get('nativeStoreJob', 'turn-' + str(view.get('turn', 'off-turn')))}."
    )
    file, content = None, marker
    if role == "gameplay":
        file = "MEMORY.md"
    elif role == "economy":
        city = next((city for city in view.get("cities", []) if city.get("owner") == "economy"), None)
        if city is not None:
            file = "city-" + city["handle"] + ".md"
            content = canonical(
                {
                    "type": "city_plan",
                    "city": city["handle"],
                    "reasons": {"construction": STORE_CITY_REASON},
                    "expectedBenefits": ["Keep an authored plan available across worker replacement."],
                    "reconsiderWhen": ["Permitted native facts change."],
                }
            )
    elif role == "military":
        file = startup.get("nativeStoreContributionFile")
    if file is None:
        return None
    if role == "economy" and typed_plans:
        document = json.loads(content)
        city = document["city"]
        reads = [
            part
            for name, arguments, part in results
            if name == "read_city_plan" and arguments.get("city") == city and isinstance(part, ToolReturnPart)
        ]
        if not reads:
            return call("read_city_plan", {"city": city})
        writes = [
            part
            for name, arguments, part in results
            if name == "save_city_plan" and arguments.get("city") == city and isinstance(part, ToolReturnPart)
        ]
        if not writes:
            return call("save_city_plan", {key: value for key, value in document.items() if key != "type"})
        order = list(calls)
        verified = next(
            (part for part in reversed(reads) if order.index(part.tool_call_id) > order.index(writes[-1].tool_call_id)),
            None,
        )
        if verified is None:
            return call("read_city_plan", {"city": city})
        if not isinstance(verified.content, dict) or STORE_CITY_REASON not in str(verified.content.get("content")):
            raise ValueError("SCRIPTED_NATIVE_CITY_PLAN_WRITE_NOT_DELIVERED")
        return None
    search = next(
        (
            part
            for name, arguments, part in results
            if name == "search_memory" and arguments.get("query") == file and isinstance(part, ToolReturnPart)
        ),
        None,
    )
    if search is None:
        return call("search_memory", {"query": file})
    if not isinstance(search.content, dict):
        raise ValueError("SCRIPTED_NATIVE_MEMORY_SEARCH_INVALID")
    exists = any(match.get("file") == file for match in search.content.get("matches", []))
    reads = [
        (arguments, part)
        for name, arguments, part in results
        if name == "read_memory" and arguments.get("file") == file
    ]
    previous = None
    if exists or search.content.get("truncated"):
        if not reads:
            return call("read_memory", {"file": file})
        first = reads[0][1]
        if not isinstance(first, ToolReturnPart) or not isinstance(first.content, str):
            raise ValueError("SCRIPTED_NATIVE_MEMORY_READ_FAILED")
        previous = first.content
    if role == "military" and previous is not None:
        if marker not in previous:
            raise ValueError("SCRIPTED_NATIVE_CONTRIBUTION_CHANGED")
        return None  # Immutable contributions may already exist on a resumed job.
    writes = [
        (arguments, part)
        for name, arguments, part in results
        if name == "write_memory" and arguments.get("file") == file
    ]
    if not any(isinstance(part, ToolReturnPart) for _, part in writes):
        arguments = {"file": file, "content": content}
        if role == "economy" and previous is not None:
            arguments["old_text"] = previous
        return call("write_memory", arguments)
    last_write = next(part.tool_call_id for _, part in reversed(writes) if isinstance(part, ToolReturnPart))
    order = list(calls)
    verified = next(
        (
            part
            for _, part in reversed(reads)
            if isinstance(part, ToolReturnPart) and order.index(part.tool_call_id) > order.index(last_write)
        ),
        None,
    )
    if verified is None:
        return call("read_memory", {"file": file})
    if (
        not isinstance(verified.content, str)
        or (marker if role != "economy" else STORE_CITY_REASON) not in verified.content
    ):
        raise ValueError("SCRIPTED_NATIVE_MEMORY_WRITE_NOT_DELIVERED")
    if role == "gameplay" and previous is not None and previous.rstrip() not in verified.content:
        raise ValueError("SCRIPTED_NATIVE_MEMORY_APPEND_LOST_PRIOR_NOTES")
    return None


def scripted_store_finish(startup, messages, calls, call):
    results, task = store_results(messages, calls), store_task(startup)
    task_id = store_plan_read(results, task)
    if task_id is None:
        raise ValueError("SCRIPTED_NATIVE_PLAN_NOT_DELIVERED")
    updates = {"updates": [{"task_id": task_id, "status": "completed"}]}
    if not any(
        name == "update_task_statuses" and arguments == updates and isinstance(part, ToolReturnPart)
        for name, arguments, part in results
    ):
        return call("update_task_statuses", updates)
    return None


def dictionaries(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from dictionaries(child)
    elif isinstance(value, list):
        for child in value:
            yield from dictionaries(child)


def command_operation(command):
    body = dict(command)
    metadata = {key: body.pop(key) for key in ("intent", "reviewAfter") if key in body}
    return {"tool": "player_command", "arguments": {"command": body, **metadata}}


def respond(messages, info):
    startup = None
    calls = {}
    replies = []
    confirmed = set()
    for message in messages:
        if isinstance(message, ModelResponse):
            calls.update({part.tool_call_id: part for part in message.parts if isinstance(part, ToolCallPart)})
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if isinstance(part, UserPromptPart) and isinstance(part.content, str) and startup is None:
                try:
                    startup = json.loads(part.content)
                except ValueError:
                    pass
            if not isinstance(part, ToolReturnPart) or not isinstance(part.content, dict):
                continue
            body = expand_response(dict(part.content))
            if body.get("roleFinished") is True:
                replies.append((part.tool_name, body))
            invocation = calls.get(part.tool_call_id)
            operations = invocation.args_as_dict().get("operations", []) if invocation is not None else []
            if invocation is not None and invocation.tool_name == "execute_commands":
                operations = [command_operation(command) for command in invocation.args_as_dict()["commands"]]
            for index, receipt in enumerate(body.get("receipts", [])):
                result = receipt.get("result", {})
                replies.append((receipt.get("tool", part.tool_name), result))
                if receipt.get("status") == "confirmed" and index < len(operations):
                    confirmed.add(canonical(operations[index]))
    if not startup or startup.get("role") not in {"gameplay", "economy", "military", "communication"}:
        raise ValueError("SCRIPTED_STARTUP_INVALID")
    role = startup["role"]
    step = sum(isinstance(message, ModelResponse) for message in messages) + 1

    def call(name, arguments):
        if startup.get("nativeStoreExercise") and name == "finish_role":
            pending = scripted_store_finish(startup, messages, calls, call)
            if pending is not None:
                return pending
        if name == "act" and all(operation["tool"] == "player_command" for operation in arguments["operations"]):
            name = "execute_commands"
            arguments = {
                "commands": [
                    {
                        **operation["arguments"]["command"],
                        **{key: value for key, value in operation["arguments"].items() if key != "command"},
                    }
                    for operation in arguments["operations"]
                ]
            }
        return ModelResponse(parts=[ToolCallPart(name, arguments, "script-" + str(step) + "-" + name)])

    if any(value.get("roleFinished") is True for name, value in replies if name == "finish_role"):
        return call("finish_report", {"summary": "Completed the bounded scripted role check."})
    view = startup["reads"].get("player_turn", {})
    for name, value in replies:
        if name == "player_turn":
            view = value
    if startup.get("nativeStoreExercise"):
        last_return = next(
            (
                part
                for message in reversed(messages)
                if isinstance(message, ModelRequest)
                for part in reversed(message.parts)
                if isinstance(part, ToolReturnPart)
            ),
            None,
        )
        if last_return is not None and last_return.tool_name == "save_city_plan" and last_return.outcome != "success":
            # A sibling can invalidate the delivered city view while this role
            # is writing its note. Follow the tool's feedback before retrying.
            return call("player_turn", {"arguments": {}, "recoverBaseline": "missing"})
        pending = scripted_store_setup(
            startup,
            messages,
            calls,
            call,
            typed_plans=info is not None and any(tool.name == "save_city_plan" for tool in info.function_tools),
            view=view,
        )
        if pending is not None:
            return pending
    if role == "communication":
        if not replies:
            return call("chat_read", {"arguments": {"includeContext": True}})
        social = startup["reads"]["chat_read"]
        for name, value in replies:
            if name == "chat_read":
                social = value
        for event in social.get("messages", []):
            message = event.get("body", {})
            if event.get("kind") != "message" or message.get("author") == social.get("civilization"):
                continue
            arguments = {"text": "Acknowledged " + message["message"] + ": " + message["text"][:2000]}
            if not message["public"]:
                arguments["recipients"] = sorted(set(message["participants"]) - {social["civilization"]})
            operation = {"tool": "chat_send", "arguments": arguments}
            if canonical(operation) not in confirmed:
                return call("act", {"operations": [operation]})
        return call("finish_role", {"arguments": {}})
    if not replies or replies[-1][0] in {"player_command", "execute_commands", "economy_wait"}:
        return call("player_turn", {"arguments": {}, "recoverBaseline": "missing"})
    if role == "military":
        reviews = [value["militaryCompletion"] for _, value in replies if "militaryCompletion" in value]
        if not reviews or replies[-1][0] != "military_review":
            return call("military_review", {"arguments": {}})
        review = reviews[-1]
        if review.get("nextCursor") is not None:
            return call("military_review", {"arguments": {"cursor": review["nextCursor"]}})
        targets = sorted({item["target"] for page in reviews for item in page.get("items", [])})
        return call(
            "finish_role",
            {
                "arguments": {
                    "decisions": [
                        {
                            "targets": targets,
                            "reason": "Hold position during the bounded orchestration check; accept the observed exposure.",
                        }
                    ]
                    if targets
                    else []
                }
            },
        )
    if role == "gameplay":
        for alert in view.get("alerts", []):
            offered = alert.get("command")
            if alert.get("canAcknowledge") and offered and offered.get("kind") == "ACKNOWLEDGE_ALERT":
                operation = {"tool": "player_command", "arguments": {"command": offered}}
                if canonical(operation) not in confirmed:
                    return call("act", {"operations": [operation]})
        for unit in view.get("units", []):
            if unit.get("canFoundCity"):
                operation = {
                    "tool": "player_command",
                    "arguments": {"command": {"kind": "FOUND_CITY", "target": unit["handle"]}},
                }
                if canonical(operation) not in confirmed:
                    return call("act", {"operations": [operation]})
        if not view.get("researchQueue") and view.get("researchOptions"):
            choice = view["researchOptions"][0]
            details = [value for name, value in replies if name == "research_details"]
            if not details:
                return call("research_details", {"arguments": {"names": [choice]}})
            for offered in dictionaries(details[-1]):
                if offered.get("kind") == "SET_RESEARCH_QUEUE" and offered.get("queue"):
                    operation = {"tool": "player_command", "arguments": {"command": offered}}
                    if canonical(operation) not in confirmed:
                        return call("act", {"operations": [operation]})
        if startup.get("waitForEconomy") and not any(part.tool_name == "economy_wait" for part in calls.values()):
            return call("economy_wait", {"arguments": {}})
        arguments = {}
        if not view.get("canEndTurn") and view.get("turnControls", {}).get("canDeferChoices"):
            arguments = {
                "deferChoices": True,
                "deferReason": "Explicitly defer native-permitted choices for the bounded orchestration check.",
            }
        return call("finish_role", {"arguments": arguments})
    if replies[-1][0] == "finish_role" and replies[-1][1].get("error") == "ECONOMY_WORKER_SKIPS_REQUIRED":
        targets = replies[-1][1]["roleCompletion"]["idleWorkers"]
        return call(
            "finish_role",
            {
                "arguments": {
                    "workerSkips": [
                        {
                            "targets": targets,
                            "reason": "Deliberately hold these delivered idle Economy workers for this bounded scripted pass; issue no work or travel order.",
                        }
                    ]
                    if targets
                    else []
                }
            },
        )
    cities = view.get("cities", [])
    for city in cities:
        if city.get("constructionQueue"):
            continue
        details = [
            value
            for name, value in replies
            if name == "construction_details"
            and value.get("query", {}).get("arguments", {}).get("target") == city["handle"]
        ]
        if not details:
            return call("construction_details", {"arguments": {"target": city["handle"]}, "recoverBaseline": "missing"})
        if details[-1].get("data", {}).get("detailLevel") == "catalogue":
            choices = [
                row["name"]
                for row in details[-1]["data"].get("constructions", [])
                if row.get("canQueue") and row["name"] != "Nothing"
            ]
            if choices:
                return call(
                    "construction_details",
                    {"arguments": {"target": city["handle"], "names": choices[:1]}, "recoverBaseline": "missing"},
                )
        for offer in dictionaries(details[-1]):
            if offer.get("kind") != "QUEUE_CONSTRUCTION" or offer.get("target") != city["handle"]:
                continue
            if offer.get("selection") in city.get("constructionQueue", []):
                continue
            command = {key: offer[key] for key in ("kind", "target", "selection")}
            operation = {"tool": "player_command", "arguments": {"command": command}}
            if canonical(operation) not in confirmed:
                return call("act", {"operations": [operation]})
    return call("finish_role", {"arguments": {}})


def scripted_model(request):
    if request.selection.model != "scripted-v1":
        raise ValueError("SCRIPTED_MODEL_REQUIRED")
    return FunctionModel(respond, model_name="scripted-v1")


def scripted_review_model(request):
    if request.selection.model != "scripted-v1":
        raise ValueError("SCRIPTED_MODEL_REQUIRED")

    def review(messages, info):
        # This checks structured publication only. It makes no factual claim or
        # strategic-quality claim beyond what a bounded scripted exercise proves.
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "submit_report",
                    {
                        "assessment": "Use permitted observations to decide the next legal actions; this scripted report does not establish a best strategy.",
                        "review": {
                            "horizonTurns": 3,
                            "outcomeAssessment": "This exercise checks durable orchestration and publication.",
                            "materialAssumptions": [
                                {
                                    "assessment": "The best long-term strategy remains untested.",
                                    "status": "unknown",
                                    "sources": [],
                                }
                            ],
                            "plans": [
                                {
                                    "name": "Inspect and develop",
                                    "objective": "Resolve current native decisions using observed legal options.",
                                    "investment": "Read the required choices before acting.",
                                    "expectedObservations": ["The next permitted overview records the actual outcome."],
                                    "firstActions": ["Inspect the current native choices."],
                                    "relativeOutcome": "Strategic gains relative to alternatives have not been measured.",
                                    "risks": "Rival plans and long-term outcomes remain uncertain.",
                                }
                            ],
                            "preferredPlan": None,
                            "reconsiderWhen": "Observed outcomes justify a different plan.",
                            "diplomacy": {
                                "assessment": "No diplomatic initiative is required by this scripted check.",
                                "initiatives": [],
                            },
                        },
                    },
                    "scripted-review-report",
                )
            ]
        )

    return FunctionModel(review, model_name="scripted-v1")


def scripted_peace_model(phase):
    if phase.selection.model != "scripted-v1":
        raise ValueError("SCRIPTED_MODEL_REQUIRED")

    def report(messages, info):
        body = (
            {
                key: "This bounded scripted consultation does not establish a strategic recommendation; retain the supplied facts and stated uncertainty."
                for key in (
                    "goalAssessment",
                    "battlefield",
                    "continuingWar",
                    "peaceConsequences",
                    "uncertainties",
                    "recommendation",
                )
            }
            if phase.phase == "military"
            else {"approve": True, "reason": "Approve the supplied exact terms for the bounded integration exercise."}
        )
        return ModelResponse(parts=[ToolCallPart("submit_report", body, "scripted-peace-report")])

    return FunctionModel(report, model_name="scripted-v1")
