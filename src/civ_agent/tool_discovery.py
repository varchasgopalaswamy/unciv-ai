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
        "Civ tool discovery: In functions.exec, read full tool definitions and signatures before calling them. "
        "Start with:\n"
        f"const needed = new Set([{selected}]);\n"
        "text(ALL_TOOLS.filter(t => needed.has(t.name)));\n"
        "Read results before choosing further tools. Fetch at most four new definitions per lookup for the next "
        "decisions; reuse definitions already read. Skip the index for known names. To explore, print names only:\n"
        'text(ALL_TOOLS.filter(t => t.name.startsWith("mcp__civ__")).map(t => t.name));\n'
        "Keyword searches also return names only. All job tools remain discoverable; do not preload the catalogue.\n\n"
        'For tools accepting intent, use {reason:"why"} or {plan:"existing active plan ID"}. '
        "Review flagged plans with memory_update before referencing them in intent. "
        "Save useful memory and retire obsolete records with memory_update before finishing. "
        "Follow error recovery.reads and instructions; stop if an action's outcome is uncertain. "
        "On repeated STALE_ASSUMPTIONS, defer the affected action or memory change and continue other work; "
        "do not stop the gameplay turn for this."
    )
