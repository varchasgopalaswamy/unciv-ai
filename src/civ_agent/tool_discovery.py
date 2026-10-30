"""Shared startup guidance for tool discovery and private decision records."""

from typing import Literal


def tool_discovery_instructions(purpose: Literal["gameplay", "economy", "military", "communication"]) -> str:
    names = (
        ("chat_read", "chat_send", "memory_search", "memory_update")
        if purpose == "communication"
        else ("player_turn", "player_capabilities", "victory_progress", "chat_read", "chat_send")
    )
    selected = ", ".join(f'"mcp__civ__{name}"' for name in names)
    return (
        "Civ tool discovery: Call tools as tools.<name>(arguments) in functions.exec. "
        "Use descriptions and results already supplied. If the descriptions are missing, start here:\n"
        f"const needed = new Set([{selected}]);\n"
        "text(ALL_TOOLS.filter(t => needed.has(t.name)));\n"
        "Look up other tool descriptions only as needed. To find tool names:\n"
        'text(ALL_TOOLS.filter(t => t.name.startsWith("mcp__civ__")).map(t => t.name));\n'
        "Read the relevant descriptions rather than the whole catalogue. "
        "Follow error recovery instructions. Reconcile uncertain execution before retrying; stop on recoverable=false."
    )
