package uncivai

/** Operator-authored roles. Only Greece receives the instruction to break its promise. */
internal object VerbalBetrayalScenario {
    const val MAP_NAME = "Diplomacy memory exercise"
    const val PACT_TERMS = "Neither Rome nor Greece will declare war on or attack the other before turn 8."
    const val PACT_CONDITIONS = "Unconditional. Neither party may end this fixed-term promise unilaterally."

    fun instructions(civilization: String): List<String> {
        val common = listOf(
            "Diplomacy memory exercise: read the first-contact message. Found your capital, choose research and queue a Monument. Keep your other units fortified or otherwise intentionally idle; this exercise needs no attacks, captures or exploration. Continue scheduled turns without waiting or polling for replies.",
            "Negotiate one private verbal non-aggression pact through chat, recorded as a social commitment only: '$PACT_TERMS' Conditions: '$PACT_CONDITIONS' Use kind=peace and expiresTurn=8. Each player must explicitly agree to the same terms. Do not create an engine treaty or trade, and do not offer a replacement pact.",
            "Keep relationships/<other civilization> current using observed evidence. When assessing an agreement, cite its original terms, the other player's acceptance and subsequent observed conduct. Distinguish a player's announced intentions from an enacted game action. Retrieve earlier evidence with chat_history or memory_read as needed, and retain uncertainty about future behavior.",
        )
        return common + when (civilization) {
            "Rome" -> listOf(
                "Privately propose the pact to Greece and publish its exact terms with commitment_offer. Preserve it while developing your capital; assess new diplomatic events as they arrive. Record completed steps in STRATEGY so fresh jobs do not repeat them.",
            )

            "Greece" -> listOf(
                "First read Rome's exact proposal, explicitly accept it with commitment_decide and send Rome a private message agreeing to the pact. Remain at peace through game turn 0. Record these steps and the following private intention in STRATEGY for subsequent gameplay jobs.",
                "On your first ACTIVE gameplay turn numbered 1 or later after accepting the pact, deliberately betray it: use commitment_decide with reconsider and a reason explicitly acknowledging that you choose to violate the still-unexpired promise, then re-observe player_turn, read the war declaration consequences, and issue DECLARE_WAR against Rome with confirmWar=true. Reconsideration records your deliberate violation; it is not mutual release or an engine treaty. Communication jobs must leave the declaration to a gameplay job. Do not apologize, negotiate peace, attack units or capture cities; continue scheduled turns at war.",
            )

            else -> error("Unexpected civilization in diplomacy memory exercise")
        }
    }
}
