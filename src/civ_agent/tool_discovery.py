"""Shared startup guidance for tool discovery and private decision records."""

from typing import Literal


def tool_discovery_instructions(purpose: Literal["gameplay", "communication"]) -> str:
    names = (
        ("chat_read", "chat_send", "memory_search", "memory_update")
        if purpose == "communication"
        else ("player_turn", "player_capabilities", "victory_progress", "chat_read")
    )
    selected = ", ".join(f'"mcp__civ__{name}"' for name in names)
    return (
        "Civ tool discovery: Use tools in functions.exec. Startup core definitions and prepared reads, when "
        "supplied below, are already available: call those tools directly without another discovery or opening read. "
        "Otherwise start by reading these definitions:\n"
        f"const needed = new Set([{selected}]);\n"
        "text(ALL_TOOLS.filter(t => needed.has(t.name)));\n"
        "Gameplay actions use mcp__civ__player_command; private changes use mcp__civ__memory_update. "
        "Read definitions before calling any tool not supplied at startup. Fetch at most four new definitions per lookup for the next "
        "decisions; reuse definitions already read. Skip the index for known names. To explore, print names only:\n"
        'text(ALL_TOOLS.filter(t => t.name.startsWith("mcp__civ__")).map(t => t.name));\n'
        "Keyword searches also return names only. All job tools remain discoverable; do not preload the catalogue.\n\n"
        'For tools accepting intent, use {reason:"why"} or {plan:"existing active plan ID"}. '
        "Review flagged plans with memory_update before referencing them in intent. "
        "Save changed strategy, beliefs and useful durable notes; receipts already preserve routine state and actions. "
        "Retire obsolete records with memory_update before finishing; no memory write is needed when nothing useful changed. "
        "Follow error recovery.reads and instructions; stop if an action's outcome is uncertain. "
        "For STALE_ASSUMPTIONS, follow recovery.reads, reconsider and revise flagged plans, then make one fresh "
        "attempt. Skip only obsolete work or work already handled. A second stale rejection of the same action "
        "or memory edit stops the run for inspection. Never defer a persistent recovery failure to keep playing. "
        "REPEATED_STALE_ASSUMPTIONS is terminal; stop without further tool calls."
    )
