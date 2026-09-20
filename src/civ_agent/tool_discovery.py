"""Shared model guidance for exploring the complete, job-scoped tool registry."""

from typing import Literal


def tool_discovery_instructions(purpose: Literal["gameplay", "communication"]) -> str:
    names = (
        ("chat_read", "chat_send", "memory_search", "memory_update")
        if purpose == "communication"
        else ("player_turn", "player_capabilities", "victory_progress", "chat_read")
    )
    selected = ", ".join(f'"mcp__civ__{name}"' for name in names)
    actions = (
        "Ordinary game actions use player_command: action kinds such as FOUND_CITY are arguments, not tool names. "
        "Start with overview choices, then use research_details(names=[candidates]) and "
        "construction_details(target=city, names=[candidates]); broad catalogues remain available for planning. "
        "Prefer unit_details with explicit sections, e.g. [movement] to move or [settlement] to found a city; "
        "city_details likewise accepts focused sections. Copy an inspected option's command and add your intent. "
        "For distant routes use movement_preview then its SET_UNIT_DESTINATION command. "
        "Start with player_turn. Successful actions return stateUpdate: apply its changes to your last overview; "
        "do not request player_turn after every command. A delta names baseStateId and new stateId. "
        "set replaces whole sections; collections upsert replaces rows by key, removed drops IDs, and order lists their order. "
        "Omitted sections/rows stay unchanged. Removals mean absent from this view, not necessarily destroyed. "
        "Read invalidatedDetails again only when needed for your next action; other prior inspections stay valid. "
        "A full update replaces the overview. If the base is missing/mismatched or refresh is unavailable, read player_turn. "
        "Keep successful receipts and never repeat an applied action to refresh state. Stop at turn_ended. "
        "An argument error with applied=false alone needs no state refresh; follow recovery.reads when supplied. "
        if purpose == "gameplay"
        else ""
    )
    return (
        "Civ tool discovery: In functions.exec, inspect ALL_TOOLS selectively. "
        "Start directly with exact definitions for the tools needed next; skip the index when their names are known. "
        "For this job's ordinary opening, run the lookup below first, then call the relevant tools and inspect "
        "their results before selecting further definitions. Read full descriptions and call signatures.\n"
        f"const needed = new Set([{selected}]);\n"
        "text(ALL_TOOLS.filter(t => needed.has(t.name)));\n"
        "Later, if you need to explore unknown capabilities, print a compact names-only index:\n"
        'text(ALL_TOOLS.filter(t => t.name.startsWith("mcp__civ__")).map(t => t.name));\n'
        "Broad keyword searches should likewise print names only. Select at most four new definitions per lookup, "
        "chosen for the next few steps from the observed state. Reuse definitions already read in this conversation; "
        "discover additional tools when new decisions need them, rather than preloading a whole-turn catalogue. "
        "All tools available to this job remain discoverable, including later-game capabilities."
        "\n"
        + actions
        + 'For tools accepting intent, use an object such as {reason:"why"}; plan must name an existing active plan ID. '
        + "In memory notes, plan is also an optional existing plan ID, never prose; omit it when unrelated. "
        + "Notes do not accept sources. Beliefs require sources already read, such as observation:communication after chat_read."
    )
