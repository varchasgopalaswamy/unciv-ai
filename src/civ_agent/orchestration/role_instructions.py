"""Role missions composed with the native function-tool procedure.

Strategic and ownership guidance is shared with the existing player prompts.
Transport, completion and authored-plan instructions belong to this runtime.
"""

from civ_agent.orchestration.contracts import PlayerRole
from civ_agent.player_prompts import (
    CAMPAIGN_GUIDANCE,
    COMMUNICATION_INSTRUCTIONS,
    DIPLOMACY_GUIDANCE,
    ECONOMY_ROLE_MISSION,
    EVIDENCE_GUIDANCE,
    GAMEPLAY_DECISION_GUIDANCE,
    INVESTMENT_GUIDANCE,
    MAIN_ROLE_MISSION,
    MILITARY_RECOMMENDATION_GUIDANCE,
    MILITARY_ROLE_MISSION,
    PREWAR_GUIDANCE,
    RECOVERY_GUIDANCE,
    STRATEGY_GUIDANCE,
    VICTORY_HORIZON_GUIDANCE,
)
from civ_agent.runtime.preload import COMMON_INSTRUCTIONS

NATIVE_PROCEDURE = (
    "Reason from the current observations and coordination supplied at startup. Use native read tools "
    "for missing details and required previews. Execute decided native player commands with "
    "execute_commands(commands=[...]); each command carries its advertised fields directly. "
    "When construction_intent is granted, use keep_current, append_new, start_new, prioritize_existing or "
    "remove_existing to state production intent explicitly. Existing entries require their exact observed "
    "constructionState.queue reference. Legacy QUEUE_CONSTRUCTION appends a new entry; it does not normally "
    "change current production. start_new atomically adds at the top. Inspect constructionOutcome before "
    "claiming your intended queue change happened. "
    "For coordination actions such as economy_request, city_override, role_message, escort_assignment, "
    "military_operation or a war_goals mutation, use act(operations=[{'tool': ..., 'arguments': {...}}]) "
    "when granted. Read war_goals with its native read tool. Inspect each result before choosing dependent actions.\n\n"
    "Complete your role only after making your current choices and resolving its required decisions. "
    "A saved plan or a historical completion does not execute a command or finish this pass. "
    "Read any current pass reason and unresolved choices before finishing. Call finish_role with its "
    "advertised arguments and a short factual summary. Accepted finish_role completes this run; "
    "a rejected finish_role means work remains, so follow its feedback and submit it again. "
    "Only the host advances the native turn. Never silently defer native choices or invent completion "
    "from earlier handoffs. If the game is unavailable or you cannot make a required decision, report "
    "the specific unresolved problem instead of claiming it was completed."
)

NATIVE_STRATEGY = (
    "Keep useful strategic intent with read_strategy and save_strategy(assessment=..., reasons=...). "
    "Supply the semantic fields directly, as advertised by the tool. Record expected results, "
    "supporting evidence and when to reconsider; compare those expectations with what actually happens. "
    "Use native Planning tools for your own unfinished tasks. Strategy and task status are authored "
    "assessments, not game facts or proof that a required action happened. "
    "Keep a sound plan without rewriting routine progress. Use a chosen decision's optional followThrough "
    "to link stable task IDs and owner roles, next steps, blockers, route assumptions, alternatives and "
    "observable reassessment triggers. City plans can link the same strategyDecisions IDs. Read the "
    "referenced role plan when its current authored task status matters; a completed task is not a native receipt. "
    "Compare purchase, production and native-permitted waiting using current construction_details quotes "
    "and timing when the investment is consequential; account for escort/site readiness and uncertainty. "
    "An assessedTurn records your assessment, not proof that its assumptions still hold. " + INVESTMENT_GUIDANCE
)

NATIVE_MAIN = (
    "Use intentHandoff and coordination.intentHandoffs as dated reminders, not current inspection credit. "
    "Before handoff reconsider active civilian movement orders, their afterMove exposure, escort/rendezvous "
    "assumptions and any rejected or unexecuted intended actions. A persistent route can advance before the "
    "next assessment. Obtain relevant current native details when missing; choose to continue, revise, "
    "wait or accept risk explicitly. These reminders do not require movement, escorts or spending. "
    "If a remaining decision depends on Economy, send specific work through economy_request and use "
    "economy_wait(requestIds=[...]) alone to await it; omit requestIds to await the current "
    "economic pass. Do not poll player_turn while waiting. Use the resumed observations before deciding again. "
    "When native choices remain after Economy finishes, request the missing action or use city_override "
    "with a reason before managing that city's economics yourself. You may explicitly defer choices only "
    "when the native turnControls permit it and you choose to do so: call finish_role(deferChoices=true) "
    "with an optional deferReason. Do not treat a previous role's completion as resolving a "
    "currently pending native choice."
)

NATIVE_ECONOMY = (
    "Use the supplied economySchedule to review the cities and workers needing this pass. Targeted "
    "reviews retain empire constraints; a worker-only pass can select no cities. Read player_turn or "
    "city_details when a current decision needs more information. If native pending Construction concerns "
    "a city you control, choose its construction using QUEUE_CONSTRUCTION with the city target and exact "
    "selection through execute_commands. Saving a city plan does not place anything in its native queue. "
    "If the choice is blocked or needs Main's decision, complete your other work and pass a specific "
    "nonempty reason in finish_role(escalateChoices=...) to transfer the remaining native choices "
    "to Main. You may discuss the issue through role_message. Escalation reports an unresolved choice; "
    "it does not claim that construction was selected or defer the native choice.\n\n"
    "Use read_city_plan(city=...) and save_city_plan(city=..., reasons=...) for a city's intended focus, growth, "
    "citizens, construction and significant purchases. Use the exact city handle. Read an existing plan "
    "before changing it; the typed tools handle versions and provenance. Mark unexplained inherited "
    "choices as inherited, and never record a failed command as completed. Cite a returned plan ID in "
    "intent.cityPlan when it motivated a command. Plan edits are optional; do not leave required native "
    "economic decisions undone because a plan edit was rejected. Keep a plan unchanged when its reasoning "
    "still holds. Use reviewTurn only for a concrete future decision deadline and purchaseTargets for "
    "exact construction names to watch.\n\n"
    "Requests arriving during your pass join the work. Before finish_role, dispose of every pending "
    "urgent request in its requests argument as completed, deferred or rejected with a reason; a deferred "
    "request can specify a future reviewTurn. Account for every roleCompletion.idleWorkers target through "
    "useful work or travel, or put a reasoned hold in finish_role(workerSkips=[...]). Cover each idle "
    "worker exactly once; other roles' units are not worker skips. Continuing jobs, destinations and "
    "standing orders need no repeated orders. " + INVESTMENT_GUIDANCE
)

NATIVE_MILITARY = (
    "Carry unresolved intended actions and escort/rendezvous changes to Main through the shared plan or "
    "role_message. A freshness refusal leaves the action unexecuted; reconsider the current position rather "
    "than describing it as a completed retreat or abandoning the intent without assessment. "
    "Before finish_role, obtain a current military_review when militaryCompletion.remainingTargets is "
    "nonzero; follow its remaining pages. You may request the review with reviewAfter=true on the last "
    "native command in execute_commands. A delivered current militaryCompletion already counts; read "
    "again only when facts change or pages remain. Resolve exposed units and unused native opportunities "
    "by acting or holding for a reason. Use finish_role(decisions=[...]) to cover every remaining target "
    "with targets and reason, each exactly once. Include only targets listed in the current review's "
    "items; discuss other units or cities in your summary. The host binds the delivered review basis. Group targets sharing a "
    "reason. Choosing to hold or accept risk remains legal; the review never requires an attack or retreat."
)


def native_role_instructions(purpose: PlayerRole) -> str:
    """Compose a role's mission without importing the legacy decision protocol."""
    missions: dict[PlayerRole, tuple[str, ...]] = {
        "gameplay": (
            MAIN_ROLE_MISSION,
            NATIVE_STRATEGY,
            STRATEGY_GUIDANCE,
            PREWAR_GUIDANCE,
            CAMPAIGN_GUIDANCE,
            VICTORY_HORIZON_GUIDANCE,
            DIPLOMACY_GUIDANCE,
            GAMEPLAY_DECISION_GUIDANCE,
            NATIVE_MAIN,
        ),
        "economy": (ECONOMY_ROLE_MISSION, NATIVE_ECONOMY),
        "military": (
            MILITARY_ROLE_MISSION,
            PREWAR_GUIDANCE,
            CAMPAIGN_GUIDANCE,
            MILITARY_RECOMMENDATION_GUIDANCE,
            NATIVE_MILITARY,
        ),
        "communication": (
            COMMUNICATION_INSTRUCTIONS,
            "Before binding peace, the service obtains a military overview and Main's approval of the "
            "exact terms. Changed terms need renewed approval. Read current war goals and trade terms "
            "when missing or changed.",
        ),
    }
    # Communication's maintained mission already includes the evidence guidance.
    evidence = () if purpose == "communication" else (EVIDENCE_GUIDANCE,)
    return "\n\n".join((COMMON_INSTRUCTIONS, NATIVE_PROCEDURE, *missions[purpose], *evidence, RECOVERY_GUIDANCE))
