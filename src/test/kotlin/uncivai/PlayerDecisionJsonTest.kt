package uncivai

import com.badlogic.gdx.utils.JsonReader
import com.badlogic.gdx.utils.JsonValue
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import uncivai.player.ChooseFreeGreatPersonReceipt
import uncivai.player.ChooseFreeGreatPersonRequest
import uncivai.player.FreeGreatPersonDecision
import uncivai.player.FreeGreatPersonSnapshot
import uncivai.player.PlayerCommandJson
import java.lang.reflect.Modifier

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

    @Test
    fun `request receipt and DTO reflection expose only the command schema`() {
        val request = ChooseFreeGreatPersonRequest(
            requestId = "request-1",
            epoch = "epoch-3",
            decisionHandle = "decision-3",
            unitName = "Great Scientist",
        )
        val receipt = ChooseFreeGreatPersonReceipt(
            requestId = request.requestId,
            epoch = request.epoch,
            decisionHandle = request.decisionHandle,
            unitName = request.unitName,
            remainingChoices = 1,
        )

        val requestJson = PlayerCommandJson.encode(request)
        val receiptJson = PlayerCommandJson.encode(receipt)
        assertEquals(
            """{"requestId":"request-1","epoch":"epoch-3","decisionHandle":"decision-3","unitName":"Great Scientist"}""",
            requestJson,
        )
        assertEquals(
            """{"requestId":"request-1","epoch":"epoch-3","decisionHandle":"decision-3","unitName":"Great Scientist","remainingChoices":1}""",
            receiptJson,
        )
        assertEquals(
            listOf("requestId", "epoch", "decisionHandle", "unitName"),
            JsonReader().parse(requestJson).childNames(),
        )
        assertEquals(
            listOf("requestId", "epoch", "decisionHandle", "unitName", "remainingChoices"),
            JsonReader().parse(receiptJson).childNames(),
        )
        assertNoEngineObjects(requestJson + receiptJson)

        assertFields(
            FreeGreatPersonSnapshot::class.java,
            linkedMapOf(
                "epoch" to String::class.java,
                "decision" to FreeGreatPersonDecision::class.java,
            ),
        )
        assertFields(
            FreeGreatPersonDecision::class.java,
            linkedMapOf(
                "handle" to String::class.java,
                "remainingChoices" to Int::class.javaPrimitiveType,
                "mayaLimited" to Boolean::class.javaPrimitiveType,
                "options" to List::class.java,
            ),
        )
        assertFields(
            ChooseFreeGreatPersonRequest::class.java,
            linkedMapOf(
                "requestId" to String::class.java,
                "epoch" to String::class.java,
                "decisionHandle" to String::class.java,
                "unitName" to String::class.java,
            ),
        )
        assertFields(
            ChooseFreeGreatPersonReceipt::class.java,
            linkedMapOf(
                "requestId" to String::class.java,
                "epoch" to String::class.java,
                "decisionHandle" to String::class.java,
                "unitName" to String::class.java,
                "remainingChoices" to Int::class.javaPrimitiveType,
            ),
        )
    }

    private fun assertFields(type: Class<*>, expected: LinkedHashMap<String, Class<*>?>) {
        val fields = type.declaredFields.filterNot { Modifier.isStatic(it.modifiers) }
        assertEquals(expected, fields.associate { it.name to it.type })
        assertTrue(fields.all { Modifier.isFinal(it.modifiers) })
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
