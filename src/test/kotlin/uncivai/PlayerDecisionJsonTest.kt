package uncivai

import com.badlogic.gdx.utils.JsonReader
import com.badlogic.gdx.utils.JsonValue
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import uncivai.player.FreeGreatPersonDecision
import uncivai.player.FreeGreatPersonSnapshot
import uncivai.player.PlayerCommandJson

class PlayerDecisionJsonTest {
    @Test
    fun `snapshot JSON has an explicit null when no decision is pending`() {
        val encoded = PlayerCommandJson.encode(
            FreeGreatPersonSnapshot(epoch = "epoch-1", decision = null),
        )

        assertEquals("""{"epoch":"epoch-1","decision":null}""", encoded)
        val root = JsonReader().parse(encoded)
        assertEquals(listOf("epoch", "decision"), root.childNames())
        assertTrue(root.get("epoch").isString)
        assertTrue(root.get("decision").isNull)
    }

    @Test
    fun `decision JSON contains only detached sorted player fields with stable types`() {
        val encoded = PlayerCommandJson.encode(
            FreeGreatPersonSnapshot(
                epoch = "epoch-2",
                decision = FreeGreatPersonDecision(
                    handle = "decision-handle",
                    remainingChoices = 2,
                    mayaLimited = true,
                    options = listOf("Great Engineer", "Great Scientist"),
                ),
            ),
        )

        assertEquals(
            """{"epoch":"epoch-2","decision":{"handle":"decision-handle","remainingChoices":2,"mayaLimited":true,"options":["Great Engineer","Great Scientist"]}}""",
            encoded,
        )
        val root = JsonReader().parse(encoded)
        val decision = root.get("decision")
        assertEquals(
            listOf("handle", "remainingChoices", "mayaLimited", "options"),
            decision.childNames(),
        )
        assertTrue(decision.get("handle").isString)
        assertTrue(decision.get("remainingChoices").isNumber)
        assertTrue(decision.get("mayaLimited").isBoolean)
        assertTrue(decision.get("options").isArray)
        assertEquals(
            listOf("Great Engineer", "Great Scientist"),
            decision.get("options").map { it.asString() },
        )
        assertNoEngineObjects(encoded)
    }

    private fun assertNoEngineObjects(encoded: String) {
        for (
        forbidden in listOf(
            "GreatPersonManager",
            "UnitManager",
            "Civilization",
            "MapUnit",
            "TileMap",
            "civID",
            "unitId",
        )
        ) {
            assertFalse("JSON exposed $forbidden", encoded.contains(forbidden))
        }
    }

    private fun JsonValue.childNames(): List<String> = buildList {
        var value = child
        while (value != null) {
            add(requireNotNull(value.name))
            value = value.next
        }
    }
}
