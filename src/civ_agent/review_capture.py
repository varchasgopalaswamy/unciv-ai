"""Capture permitted frozen strategic evidence independently of model scheduling."""

import json

from civ_agent.observations import overview
from civ_agent.proposal_gateway import public_result
from civ_agent.review_context import development_changes, review_position, upcoming_events
from civ_agent.strategic_review import REVIEW_READ_TOOLS, review_facts
from civ_agent.victory_snapshot import capture_victory


class ReviewCapture:
    def __init__(self, service, store, interval):
        self.service, self.store, self.interval = service, store, interval

    async def capture(self, snapshot, read, tools, turn, epoch):
        service = self.service
        reads = {"player_turn": public_result(review_facts(overview(snapshot)))}
        reads["victory_progress"] = await capture_victory(read, snapshot["turn"])
        for name, field in (("research_details", "technologies"), ("policy_details", "policies")):
            catalogue = await read(name, {})
            names = [row["name"] for row in catalogue[field]]
            details = []
            for offset in range(0, len(names), 16):
                details.extend((await read(name, {"names": names[offset : offset + 16]}))[field])
            reads[name] = {**catalogue, field: details}
        social = await read("player_communication", {"op": "state", "epoch": epoch})
        messages = (await service.io.call(service.memory.snapshot, "gameplay"))["events"]
        # The listener may be inside a long poll when the barrier closes. Include
        # its undelivered arrivals without changing the paused memory or cursor.
        cursor = await service.io.call(service.memory.cursor)
        while True:
            page = await read(
                "player_communication", {"op": "read", "epoch": epoch, "cursor": cursor, "limit": 100, "waitSeconds": 0}
            )
            messages.extend(page["events"])
            if not page["hasMore"]:
                break
            if page["nextCursor"] <= cursor:
                raise ValueError("STRATEGIC_REVIEW_CAPTURE_FAILED: communication cursor did not advance")
            cursor = page["nextCursor"]
        messages = list({event["id"]: event for event in messages}.values())[-50:]
        reads["chat_read"] = {**social, "messages": messages}
        conversations = await service.io.call(
            service.memory.conversation_context, [e["body"] for e in messages if e["kind"] == "message"]
        )
        conversations.pop("_evidenceKeys")
        reads["chat_read"]["conversationContext"] = conversations
        position = review_position(snapshot)
        previous = await service.io.call(self.store.last_position)
        reads["player_turn"]["developmentSincePreviousReview"] = development_changes(previous, position)
        reads["player_turn"]["upcomingEvents"] = upcoming_events(snapshot, social)
        reads["player_turn"]["decisionFeedback"] = await service.io.call(service.memory.decision_feedback, snapshot)
        reads["player_turn"]["recentProgress"] = await service.io.call(service.memory.progress_feedback, snapshot)
        catalogue = [tool for tool in tools if tool["name"] in REVIEW_READ_TOOLS]
        for tool in catalogue:
            if tool["name"] == "player_details":
                from civ_agent.tool_schemas import tool_model

                tool["inputSchema"] = tool_model("player_details", "review").model_json_schema()
            if tool["name"] == "player_turn":
                tool["description"] = "Read the frozen overview being reviewed."
            else:
                tool["description"] = "Read only the frozen review position. " + tool.get("description", "")
        actions = []
        first_turn = max(1, turn - self.interval + 1)
        previous_turn = await service.io.call(self.store.last_completed_turn)
        if previous_turn is not None:
            first_turn = 0 if previous_turn == 0 else previous_turn + 1
        for recent_turn in range(first_turn, turn + 1):
            offset = 0
            while True:
                page = await service.io.call(service.memory.history, recent_turn, offset)
                actions.extend(
                    {"turn": recent_turn, "action": action["action"], "commandStatus": action["outcome"]}
                    for action in page["actions"]
                    if action["outcome"] == "confirmed"
                )
                if not page["hasMore"]:
                    break
                offset = page["nextOffset"]
        total = len(actions)
        # Keep factual actions without carrying the player's strategic rationales
        # into an independent assessment. Full reasons remain in the audit history.
        while actions and len(json.dumps(actions, ensure_ascii=False)) > 10000:
            actions.pop(0)
        return {
            "turn": turn,
            "civilization": service.civilization,
            "observation": review_facts(snapshot),
            "reads": {name: public_result(review_facts(value)) for name, value in reads.items()},
            "catalogue": catalogue,
            "actions": {"entries": actions, "omittedEarlierActions": total - len(actions)},
        }
