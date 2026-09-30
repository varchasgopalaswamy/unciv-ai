package uncivai.player

import com.unciv.models.translations.fillPlaceholders
import com.unciv.models.translations.getPlaceholderParameters
import com.unciv.models.translations.getPlaceholderText

/** Audited native templates only, before translation and visibility filtering.
 *
 * Unknown event prose is preserved. This never processes chat, promises or notes.
 * Numerical effects below follow CityStateFunctions, QuestManager and the bundled
 * ruins rules; they describe native effects, not a player's expected response.
 */
internal fun notificationGameplayText(text: String): String {
    val replacement = when (text.getPlaceholderText()) {
        "[] is grateful that you killed a Barbarian that was threatening them!" ->
            "Killed a Barbarian threatening []: +12 Influence; 5 additional turns without intrusion penalties."

        "[] is upset that you demanded tribute from [], whom they have pledged to protect!" ->
            "Native diplomatic opinion with [] decreased after you demanded tribute from its protected city-state []."

        "[] is upset that you attacked [], whom they have pledged to protect!" ->
            "Native diplomatic opinion with [] decreased after you attacked its protected city-state []."

        "[] is upset that you attacked [], whom they are allied with!" ->
            "Native diplomatic opinion with [] decreased after you attacked its allied city-state []."

        "[] is outraged that you destroyed [], whom they had pledged to protect!" ->
            "Native diplomatic opinion with [] decreased after you destroyed its protected city-state []."

        "[] is being attacked by []! Kill [] of the attacker's military units and they will be immensely grateful." ->
            "Military assistance quest from [] against []: kill [] military units for 100 Influence."

        "[] is deeply grateful for your assistance in the war against []!" ->
            "Military assistance quest for [] against [] completed: +100 Influence."

        "[] no longer needs your assistance against []." ->
            "Military assistance quest from [] against [] ended."

        "[] no longer needs your help with the [] quest." ->
            "[] ended the [] quest."

        "[] is being attacked by [] and asks all major civilizations to help them out by gifting them military units." ->
            "Military-unit gift request from [], which is being attacked by []."

        "City-States grow wary of your aggression. The resting point for Influence has decreased by [] for []." ->
            "The resting point for Influence has decreased by [] for []."

        "[] is afraid of your military power!" ->
            "[] can be asked for tribute due to your military power."

        "We have married into the ruling family of [], bringing them under our control." ->
            "Acquired [] through Diplomatic Marriage."

        "[] has married into the ruling family of [], bringing them under their control." ->
            "[] acquired [] through Diplomatic Marriage."

        "Your [] lost its faith after spending too long inside enemy territory!" ->
            "Your [] was destroyed after losing its religious strength in foreign territory without passage rights."

        "We have discovered cultural artifacts in the ruins! (+20 culture)" ->
            "Culture gained from ancient ruins (base reward: 20 Culture, modified by game speed)."

        "An ancient tribe trained us in their ways of combat!" ->
            "Unit gained 10 XP from ancient ruins."

        "We have found survivors in the ruins! Population added to []." ->
            "Ancient ruins added 1 population to []."

        "We have discovered the lost technology of [] in the ruins!" ->
            "Technology [] acquired from ancient ruins."

        "Our unit finds advanced weaponry hidden in the ruins!" ->
            "Unit upgraded for free by ancient ruins."

        "You find evidence of Barbarian activity. Nearby Barbarian camps are revealed!" ->
            "Ancient ruins revealed nearby Barbarian encampments."

        "We have found a crudely-drawn map in the ruins!" ->
            "Ancient ruins revealed map tiles."

        "We have found holy symbols in the ruins, giving us a deeper understanding of religion! (+[] Faith)",
        "We have found an ancient prophecy in the ruins, greatly increasing our spiritual connection! (+[] Faith)",
        ->
            "Gained [] Faith from ancient ruins."

        "{A new b'ak'tun has just begun!}\n{A Great Person joins you!}" ->
            "The Long Count granted a free Great Person choice."

        else -> return text
    }
    return replacement.fillPlaceholders(*text.getPlaceholderParameters().toTypedArray())
}
