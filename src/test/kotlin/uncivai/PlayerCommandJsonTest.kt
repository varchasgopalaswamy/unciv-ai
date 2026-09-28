package uncivai

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test
import uncivai.player.OwnedCitiesSnapshot
import uncivai.player.OwnedCitySummary
import uncivai.player.PlayerCommandJson
import uncivai.player.PlayerTurnJson
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest

class PlayerCommandJsonTest {
    @Test fun `owned city snapshots expose only detached handles and names`() {
        val snapshot = OwnedCitiesSnapshot("epoch", listOf(OwnedCitySummary("city1", "Rome")))
        assertEquals("""{"epoch":"epoch","cities":[{"handle":"city1","name":"Rome"}]}""", PlayerCommandJson.encode(snapshot))
    }

    @Test fun `rename and free person requests use the consolidated schema`() {
        for (request in listOf(
            TurnCommandRequest("rename", "epoch", "revision", TurnCommandKind.RENAME_CITY, "city1", "Nova Roma"),
            TurnCommandRequest("choice", "epoch", "revision", TurnCommandKind.CHOOSE_FREE_GREAT_PERSON, selection = "Great Scientist"),
        )) {
            val payload = Json.parseToJsonElement(PlayerTurnJson.encode(request)).jsonObject
            assertEquals(request, InputSchemas.turnCommand.decode(TurnCommandRequest.serializer(), payload))
            assertFalse(payload.keys.any { it in setOf("cityHandle", "decisionHandle", "unitName") })
        }
    }
}
