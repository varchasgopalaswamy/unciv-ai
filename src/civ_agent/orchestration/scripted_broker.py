"""Credential-free provider fixture behind the real authenticated broker ledger."""

from aiohttp import web

from civ_agent.tls import write_private_json


class ScriptedAdvisoryProvider:
    def __init__(self, root):
        self.root, self.calls = root, 0

    async def evaluate(self, body, *, timeout):
        self.calls += 1
        write_private_json(self.root / "provider-count.json", {"calls": self.calls})
        answers = []
        for question in body["questions"]:
            choices = [choice["value"] for choice in question["choices"]]
            preferred = {
                "preservation": "consider",
                "beliefs": "reconsider",
                "retrieval_include": "include",
                "retrieval_priority": "normal",
                "response_needed": "needed",
                "response_timing": "can_wait",
                "delivery_needed": "needed",
                "delivery_timing": "can_wait",
            }.get(question["name"].split(":")[0])
            selection = (
                preferred
                if preferred in choices
                else "keep"
                if "keep" in choices
                else "defer"
                if "defer" in choices
                else choices[0]
            )
            answers.append(
                {
                    "type": "choice",
                    "name": question["name"],
                    "choice": selection,
                    "confidence": 0.99,
                    "probabilities": [
                        {"value": value, "probability": 0.99 if value == selection else 0.01 / (len(choices) - 1)}
                        for value in choices
                    ],
                }
            )
        return {"answers": answers, "usage": {"input_tokens": 10, "output_tokens": 10}, "latencyMs": 0}

    async def system_one(self, request):
        wire = await request.json()
        body = {
            "model": wire["model"],
            "input": wire["state"],
            "questions": [
                {
                    "name": name,
                    "instructions": question["instructions"],
                    "choices": [
                        {"value": value, "description": description}
                        for value, description in question["criteria"].items()
                    ],
                }
                for name, question in wire["questions"].items()
            ],
        }
        result = await self.evaluate(body, timeout=120)
        return web.json_response(
            {
                "model": body["model"],
                "answers": {
                    answer["name"]: {
                        **{key: answer[key] for key in ("type", "choice", "confidence")},
                        "probabilities": {p["value"]: p["probability"] for p in answer["probabilities"]},
                    }
                    for answer in result["answers"]
                },
                "usage": result["usage"],
            }
        )
