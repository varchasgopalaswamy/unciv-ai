package uncivai

import com.badlogic.gdx.utils.JsonReader
import com.badlogic.gdx.utils.JsonValue
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import uncivai.player.PlayerSession
import uncivai.player.PlayerSummary
import uncivai.player.PlayerSummaryJson
import java.lang.reflect.Modifier

class PlayerSummaryJsonTest {
    @Test
    fun `JSON is an explicit scalar whitelist and preserves null research`() {
        val summary = PlayerSummary(
            civilization = "Rome",
            ruleset = "Civ V - Vanilla",
            turn = 17,
            isYourTurn = true,
            treasury = 321,
            cityCount = 1,
            unitCount = 2,
            currentResearch = null,
            freePolicies = 2,
            freeTechnologies = 1,
            freeGreatPeople = 1,
        )

        val encoded = PlayerSummaryJson.encode(summary)

        assertEquals(
            """{"civilization":"Rome","ruleset":"Civ V - Vanilla","turn":17,"isYourTurn":true,"treasury":321,"cityCount":1,"unitCount":2,"currentResearch":null,"freePolicies":2,"freeTechnologies":1,"freeGreatPeople":1}""",
            encoded,
        )
        assertJsonContract(encoded, expectedResearch = null)
        assertFalse(encoded.contains("TechManager"))
        assertFalse(encoded.contains("PolicyManager"))
        assertFalse(encoded.contains("GreatPersonManager"))
        assertFalse(encoded.contains("UnitManager"))
    }

    @Test
    fun `JSON emits non-null research as a string`() {
        val encoded = PlayerSummaryJson.encode(
            PlayerSummary(
                civilization = "Greece",
                ruleset = "Civ V - Gods & Kings",
                turn = 18,
                isYourTurn = false,
                treasury = 654,
                cityCount = 2,
                unitCount = 3,
                currentResearch = "Writing",
                freePolicies = 1,
                freeTechnologies = 2,
                freeGreatPeople = 2,
            ),
        )

        assertJsonContract(encoded, expectedResearch = "Writing")
    }

    @Test
    fun `public DTO and session surfaces contain only the declared player contract`() {
        // This guards the ordinary public API shape; it is not an in-process reflection sandbox.
        val fields = PlayerSummary::class.java.declaredFields
            .filterNot { Modifier.isStatic(it.modifiers) }
        assertEquals(expectedFieldTypes, fields.associate { it.name to it.type })
        assertTrue(fields.all { Modifier.isFinal(it.modifiers) })

        val sessionMethods = PlayerSession::class.java.declaredMethods
            .filterNot { it.isSynthetic }
            .associateBy { it.name }
        assertEquals(
            setOf(
                "capabilities",
                "turn",
                "execute",
                "summary",
                "cities",
                "renameCity",
                "freeGreatPersonChoice",
                "chooseFreeGreatPerson",
            ),
            sessionMethods.keys,
        )
        assertEquals(0, sessionMethods.getValue("summary").parameterCount)
        assertEquals(PlayerSummary::class.java, sessionMethods.getValue("summary").returnType)
        assertEquals(0, sessionMethods.getValue("cities").parameterCount)
        assertEquals(
            uncivai.player.OwnedCitiesSnapshot::class.java,
            sessionMethods.getValue("cities").returnType,
        )
        assertEquals(
            listOf(uncivai.player.RenameCityRequest::class.java),
            sessionMethods.getValue("renameCity").parameterTypes.toList(),
        )
        assertEquals(
            uncivai.player.RenameCityReceipt::class.java,
            sessionMethods.getValue("renameCity").returnType,
        )
        assertEquals(0, sessionMethods.getValue("freeGreatPersonChoice").parameterCount)
        assertEquals(
            uncivai.player.FreeGreatPersonSnapshot::class.java,
            sessionMethods.getValue("freeGreatPersonChoice").returnType,
        )
        assertEquals(
            listOf(uncivai.player.ChooseFreeGreatPersonRequest::class.java),
            sessionMethods.getValue("chooseFreeGreatPerson").parameterTypes.toList(),
        )
        assertEquals(
            uncivai.player.ChooseFreeGreatPersonReceipt::class.java,
            sessionMethods.getValue("chooseFreeGreatPerson").returnType,
        )
    }

    private fun assertJsonContract(encoded: String, expectedResearch: String?) {
        val root = JsonReader().parse(encoded)
        assertTrue(root.isObject)
        assertEquals(expectedKeys, root.childNames())

        assertTrue(root.get("civilization").isString)
        assertTrue(root.get("ruleset").isString)
        for (key in integerKeys) assertTrue("$key must be a JSON number", root.get(key).isNumber)
        assertTrue(root.get("isYourTurn").isBoolean)

        val research = root.get("currentResearch")
        if (expectedResearch == null) {
            assertTrue("currentResearch must be an explicit JSON null", research.isNull)
        } else {
            assertTrue(research.isString)
            assertEquals(expectedResearch, research.asString())
        }
    }

    private fun JsonValue.childNames(): List<String> = buildList {
        var value = child
        while (value != null) {
            add(requireNotNull(value.name))
            value = value.next
        }
    }

    companion object {
        private val expectedKeys = listOf(
            "civilization",
            "ruleset",
            "turn",
            "isYourTurn",
            "treasury",
            "cityCount",
            "unitCount",
            "currentResearch",
            "freePolicies",
            "freeTechnologies",
            "freeGreatPeople",
        )
        private val integerKeys = listOf(
            "turn",
            "treasury",
            "cityCount",
            "unitCount",
            "freePolicies",
            "freeTechnologies",
            "freeGreatPeople",
        )
        private val expectedFieldTypes = linkedMapOf(
            "civilization" to String::class.java,
            "ruleset" to String::class.java,
            "turn" to Int::class.javaPrimitiveType,
            "isYourTurn" to Boolean::class.javaPrimitiveType,
            "treasury" to Int::class.javaPrimitiveType,
            "cityCount" to Int::class.javaPrimitiveType,
            "unitCount" to Int::class.javaPrimitiveType,
            "currentResearch" to String::class.java,
            "freePolicies" to Int::class.javaPrimitiveType,
            "freeTechnologies" to Int::class.javaPrimitiveType,
            "freeGreatPeople" to Int::class.javaPrimitiveType,
        )
    }
}
