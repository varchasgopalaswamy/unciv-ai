"""Shared startup guidance for tool discovery and private decision records."""

from typing import Literal


def tool_discovery_instructions(purpose: Literal["gameplay", "communication"]) -> str:
    names = (
        ("chat_read", "chat_send", "memory_search", "memory_update")
        if purpose == "communication"
        else ("player_turn", "player_capabilities", "victory_progress", "chat_read", "chat_send")
    )
    selected = ", ".join(f'"mcp__civ__{name}"' for name in names)
    return (
        "Civ tool discovery: Use tools in functions.exec. Startup core definitions and prepared reads, when "
        "supplied below, are already available: call those tools directly without another discovery or opening read. "
        "Otherwise start by reading these definitions:\n"
        f"const needed = new Set([{selected}]);\n"
        "text(ALL_TOOLS.filter(t => needed.has(t.name)));\n"
        "Gameplay actions use mcp__civ__player_command; private changes use mcp__civ__memory_update. "
        "Read definitions before calling any tool not supplied at startup. Fetch a small relevant set for the next "
        "decisions; reuse definitions already read. Skip the index for known names. To explore, print names only:\n"
        'text(ALL_TOOLS.filter(t => t.name.startsWith("mcp__civ__")).map(t => t.name));\n'
        "Keyword searches also return names only. Do not preload the catalogue.\n\n"
        'For optional intent annotations, use {reason:"why this action"}; strategy is an optional historical memory ID. '
        "Intent records your reasoning. Follow memory_update's record and ownership rules. "
        "No memory write is needed when nothing useful changed. "
        "Follow error recovery.reads and instructions; stop if an action's outcome is uncertain. "
        "Follow recoverable error responses. "
        "recoverable=false is terminal: stop without further tool calls."
    )
