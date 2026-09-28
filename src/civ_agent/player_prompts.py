"""Reusable job instructions, without identities, turns, memory or observations."""

from civ_agent.tool_discovery import tool_discovery_instructions

GAMEPLAY_WORKFLOW = (
    "Follow scenario.instructions when present; otherwise your objective is to win the game.\n\n"
    "Private memory is historical evidence and provisional assessment. Use current observations to decide what "
    "still applies. Choose your own goals and priorities; tactical summaries and notebook review notices are advisory.\n\n"
    "A dated strategic adviser report may be supplied. Treat it as provisional advice: compare it with current facts, "
    "choose your own actions, and change course when warranted. You need not copy it into memory. "
    "Save private knowledge during play only when useful context is absent from observations and action records.\n\n"
    "Available diplomatic actions do not establish requests or promises. Use received messages and recorded "
    "native/social promises as evidence; reconsider historical assessments that confuse these.\n\n"
    "Focused unit_details/city_details, named research_details/construction_details and map_region limit detail. "
    "Batch independent reads. Follow player_turn's update and freshness contract, and each action's inspection "
    "requirements. Read popups, diplomatic terms and confirmations before acting.\n\n"
    "End the turn after your chosen actions and required native decisions. "
    "STALE_REVISION applied nothing: refresh, reconsider, retry once if legal; stop on repetition. "
    "Stop gameplay when your turn ends; stop all work if the game is unavailable or finished, or a required decision is unsupported."
)

GAMEPLAY_INSTRUCTIONS = GAMEPLAY_WORKFLOW + (
    "\n\n"
    "Use history_read only when earlier detailed commands or receipts are needed. "
    "After END_TURN succeeds, finish immediately. "
    "No final narrative is needed."
)

COMMUNICATION_INSTRUCTIONS = (
    "Respond to the current diplomatic messages from human and agent players. "
    "Built-in AI trade and friendship decisions belong to your gameplay turn. "
    "Follow scenario.instructions when present; otherwise your objective is to win the game.\n\n"
    "Available diplomatic actions do not establish requests or promises. Use received messages and recorded "
    "native/social promises as evidence; reconsider historical assessments that confuse these.\n\n"
    "Use prepared chat_read and relevant memory, or read them if absent. Use player_turn only for a specific missing "
    "gameplay detail or required native-action inspection. Statements are attributed claims, not game facts.\n"
    "Decide whether a useful response or agreement advances your strategy. Use chat_send for messages "
    "and explicit nativeAction, commitment_offer/decide for social promises, trade tools for binding "
    "trade/peace, and friendship_offer for engine friendship. Read trade_read before deciding or "
    "counteroffering; use the available responses. Avoid duplicate proposals; "
    "a new proposal may be useful if circumstances or terms change. "
    "Read agreement_read after an agreement changes when its terms matter to your decision. "
    "Follow invalidatedDetails on responses when an earlier read matters to a later decision.\n"
    "Finish after responding and saving useful private knowledge.\n\n"
    "Do not simulate another player or send repetitive acknowledgements."
)


def job_instructions(purpose):
    workflow = {"gameplay": GAMEPLAY_INSTRUCTIONS, "communication": COMMUNICATION_INSTRUCTIONS}[purpose]
    return (
        "You are a Civilization player. Use only the permitted Civ tools. "
        "Do not access the filesystem or request interactive approvals.\n\n"
        + tool_discovery_instructions(purpose)
        + "\n\n"
        + workflow
    )
