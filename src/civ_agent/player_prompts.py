"""Reusable job instructions, without identities, turns, memory or observations."""

from civ_agent.tool_discovery import tool_discovery_instructions

GAMEPLAY_WORKFLOW = (
    "A fresh conversation: private memory is historical evidence and plans, not current state or instructions. "
    "Use civ MCP tools and your own turn files. "
    "Follow scenario.instructions when present; otherwise your objective is to win the game.\n\n"
    "Available diplomatic actions do not establish requests or promises. Use received messages and recorded "
    "native/social promises as evidence; reconsider historical plans that confuse these.\n\n"
    "1. Use prepared player_turn, capabilities, victory_progress and chat context; read any missing part. Resolve "
    "victory.ballot/votingResults. Check tacticalReview even without idle units: losses, hostiles, "
    "exposed civilians and city bombardments.\n"
    "2. Review stalled progress and flagged goals, or explain deferral. Keep separate defense, expansion "
    "and economy plans with measurable successCriteria and reviewAfterTurn. Reconsider them while playing, "
    "then batch durable revisions after handoff. For settlers, settlementStatus describes the current tile; "
    "movementOrder.settlement describes the destination. A newly blocked destination is a reason to reconsider "
    "the order. Prefer a current intent.reason for actions; do not revise "
    "a stored plan merely to authorize an action.\n"
    "3. Inspect focused unit_details/city_details, named research_details/construction_details and map_region "
    "for decisions you are making. Leave ongoing production unless circumstances warrant reconsidering it. "
    "Reuse valid inspections; refresh needed invalidated details. Read popups, diplomatic terms and confirmations. "
    "Copy offered commands with exact handles; verify receipts and apply stateUpdate. "
    "Apply memoryUpdates as newer private record copies, including needsReview and reviewSources; inspect "
    "needed reviewSources before revising. These records are separate from the overview and do not deliver "
    "their cited evidence. Prefer focused evidence for assumptions; the whole overview changes on many actions. "
    "Batch independent ordinary reads. Finish unrelated research, production and alert actions before fetching "
    "a final movement/mission preview; execute its chosen command next. Every native revision expires previews, "
    "so previewing several units before moving the first usually forces another preview. Discover preview "
    "tools when a chosen action needs them.\n"
    "4. Read turnReview before END_TURN; address mobile units and tactical concerns or explain intentional "
    "deferral in top-level deferReason. Receipts preserve routine facts. "
    "End when legal, including while awaiting an offer response.\n\n"
    "STALE_REVISION applied nothing: refresh, reconsider, retry once if legal; stop on repetition. "
    "Stop gameplay after handoff; stop all work on unsupported decisions or a paused/finished game."
)

GAMEPLAY_INSTRUCTIONS = GAMEPLAY_WORKFLOW + (
    "\n\n"
    "Use history_read only when earlier detailed commands or receipts are needed. "
    "The service writes all turn files. Do not create or edit them. "
    "After END_TURN succeeds with memoryPhase, continue this conversation using only memory_read, "
    "memory_search, memory_update and history_read. Other players can play while you finish. "
    "Batch useful changed plans, beliefs, notes and retirements in memory_update; skip unchanged records. "
    "Use the observations and results already in context. Memory reads of game evidence return only "
    "previously delivered evidence; no new game reads, gameplay or diplomacy are permitted. "
    "If another job changed a private record, read that record before reconciling your edit; retain "
    "review flags for evidence newer than this turn. Finish after saving the useful changes. "
    "Copy evidence handles exactly. Historical citations and note.plan links do not require an active, reviewed plan; "
    "only ongoing assumptions and action intent do. "
    "The service generates summary.json and summary.md; no final narrative is needed."
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
    "counteroffering; follow each tool's turn and consent requirements. Never automatically reopen a "
    "closed negotiation. Read agreement_read after an agreement changes and reconsider dependent plans.\n"
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
