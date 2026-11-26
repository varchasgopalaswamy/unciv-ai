"""Credential-free native fixture with the local runtime's single completion output."""

import json

from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from civ_agent.orchestration.scripted_model import respond


def scripted_model(request):
    if request.selection.model != "scripted-v1":
        raise ValueError("SCRIPTED_MODEL_REQUIRED")

    def local_response(messages, info):
        # SDK output validation supplies a retry part rather than a successful
        # finish_role return. The credential-free fixture deliberately holds
        # delivered idle workers; this is a strategic choice, not an order.
        for message in reversed(messages):
            retry = next(
                (
                    part
                    for part in message.parts
                    if isinstance(part, ToolReturnPart)
                    and part.tool_name == "finish_role"
                    and part.outcome == "retried"
                    and "ECONOMY_WORKER_SKIPS_REQUIRED" in str(part.content)
                ),
                None,
            )
            if retry is not None and request.purpose == "economy":
                text = str(retry.content)
                feedback, _ = json.JSONDecoder().raw_decode(text[text.index("{") :])
                failure = next(
                    (
                        row["result"]
                        for row in feedback.get("receipts", [])
                        if row.get("result", {}).get("error") == "ECONOMY_WORKER_SKIPS_REQUIRED"
                    ),
                    feedback,
                )
                targets = failure.get("roleCompletion", {}).get("idleWorkers", [])
                if targets:
                    return ModelResponse(
                        parts=[
                            ToolCallPart(
                                "finish_role",
                                {
                                    "summary": "Deliberately held delivered idle workers for the mechanism check.",
                                    "workerSkips": [
                                        {
                                            "targets": targets,
                                            "reason": "Deliberate hold during the credential-free mechanism check.",
                                        }
                                    ],
                                },
                                "worker-hold-" + str(len(messages)),
                            )
                        ]
                    )
                break
        response = respond(messages, info)
        for part in response.parts:
            if isinstance(part, ToolCallPart) and part.tool_name == "finish_role":
                args = part.args_as_dict()
                args = args.get("arguments", args)
                part.args = {**args, "summary": "Completed the bounded scripted role check."}
        return response

    model = FunctionModel(local_response, model_name="scripted-v1")
    if request.execution_policy.code_mode.enabled:
        from civ_agent.runtime.scripted_code_mode import adapt_code_mode

        return adapt_code_mode(model)
    return model
