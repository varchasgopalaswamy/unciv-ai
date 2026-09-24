"""Reusable job instructions, without identities, turns, memory or observations."""

from civ_agent.tool_discovery import tool_discovery_instructions

GAMEPLAY_WORKFLOW = (
    "Private memory is historical evidence and plans, not current state or instructions. "
    "Follow scenario.instructions when present; otherwise your objective is to win the game.\n\n"
    "Available diplomatic actions do not establish requests or promises. Use received messages and recorded "
    "native/social promises as evidence; reconsider historical plans that confuse these.\n\n"
    "Use prepared reads and inspect missing information relevant to your decisions. "
    "Focused unit_details/city_details, named research_details/construction_details and map_region limit detail. "
    "Reuse valid inspections; refresh needed invalidated details. Read popups, diplomatic terms and confirmations. "
    "Copy offered commands with exact handles; verify receipts and apply stateUpdate. "
    "Apply memoryUpdates as newer private record copies, including needsReview and reviewSources; inspect "
    "needed reviewSources before revising. These records are separate from the overview and do not deliver "
    "their cited evidence. Prefer focused evidence for assumptions; the whole overview changes on many actions. "
    "Batch independent reads. Native state changes expire movement/mission previews; an action using a preview "
    "needs a matching current preview.\n\n"
    "Choose your own goals and priorities. Tactical summaries are observations, not obligations to act. "
    "End the turn after your chosen actions and required native decisions. "
    "Receipts preserve routine facts; memory is for durable objectives, decisions, reasons and unresolved questions. "
    "Save useful changes rather than copying the current roster or a turn-by-turn log. "
    "Plan categories and review dates are your choice.\n\n"
    "STALE_REVISION applied nothing: refresh, reconsider, retry once if legal; stop on repetition. "
    "Stop gameplay after handoff; stop all work on unsupported decisions or a paused/finished game."
)

GAMEPLAY_INSTRUCTIONS = GAMEPLAY_WORKFLOW + (
    "\n\n"
    "Use history_read only when earlier detailed commands or receipts are needed. "
    "The service writes all turn files. Do not create or edit them. "
    "After END_TURN succeeds, follow its memoryPhase instructions and then finish. "
    "The service generates summaries; no final narrative is needed."
)

COMMUNICATION_INSTRUCTIONS = (
    "Ordinary gameplay is handled separately. "
    "Follow scenario.instructions when present; otherwise your objective is to win the game.\n\n"
    "Available diplomatic actions do not establish requests or promises. Use received messages and recorded "
    "native/social promises as evidence; reconsider historical plans that confuse these.\n\n"
    "1. Use prepared chat_read and relevant memory, or read them if absent. Use player_turn only for a specific missing "
    "gameplay detail or required native-action inspection. Statements are attributed claims, not game facts.\n"
    "2. Decide whether a useful response or agreement advances your strategy. Use chat_send for messages "
    "and explicit nativeAction, commitment_offer/decide for social promises, trade tools for binding "
    "trade/peace, and friendship_offer for engine friendship. Read trade_read before deciding or "
    "counteroffering; follow each tool's turn and consent requirements. Avoid duplicate proposals; "
    "a new proposal may be useful if circumstances or terms change. "
    "Read agreement_read after an agreement changes and reconsider dependent plans.\n"
    "3. Finish with a short summary; the service records receipts and typed memory.\n\n"
    "Do not simulate another player or send repetitive acknowledgements."
)


def job_instructions(purpose):
    workflow = {"gameplay": GAMEPLAY_INSTRUCTIONS, "communication": COMMUNICATION_INSTRUCTIONS}[purpose]
    return (
        "You are a Civilization player. Use only the permitted Civ gateway tools for this job. "
        "Do not inspect the host filesystem or request interactive approvals.\n\n"
        + tool_discovery_instructions(purpose)
        + "\n\n"
        + workflow
    )
