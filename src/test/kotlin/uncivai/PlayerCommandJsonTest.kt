package uncivai

import com.badlogic.gdx.utils.JsonReader
import com.badlogic.gdx.utils.JsonValue
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import uncivai.player.OwnedCitiesSnapshot
import uncivai.player.OwnedCitySummary
import uncivai.player.PlayerCommandJson
import uncivai.player.RenameCityReceipt
import uncivai.player.RenameCityRequest
import java.lang.reflect.Modifier

class PlayerCommandJsonTest {
    @Test
    fun `city snapshots requests and receipts have exact JSON whitelists`() {
        val city = OwnedCitySummary(handle = "city-session-handle", name = "Rome")
        val snapshot = OwnedCitiesSnapshot(epoch = "game-epoch", cities = listOf(city))
        val request = RenameCityRequest(
            requestId = "request-1",
            epoch = snapshot.epoch,
            cityHandle = city.handle,
            name = "Nova Roma",
        )
        val receipt = RenameCityReceipt(
            requestId = request.requestId,
            epoch = request.epoch,
            cityHandle = request.cityHandle,
            previousName = city.name,
            name = request.name,
        )

        val snapshotJson = PlayerCommandJson.encode(snapshot)
        val requestJson = PlayerCommandJson.encode(request)
        val receiptJson = PlayerCommandJson.encode(receipt)

        assertEquals(
            """{"epoch":"game-epoch","cities":[{"handle":"city-session-handle","name":"Rome"}]}""",
            snapshotJson,
        )
        assertEquals(
            """{"requestId":"request-1","epoch":"game-epoch","cityHandle":"city-session-handle","name":"Nova Roma"}""",
            requestJson,
        )
        assertEquals(
            """{"requestId":"request-1","epoch":"game-epoch","cityHandle":"city-session-handle","previousName":"Rome","name":"Nova Roma"}""",
            receiptJson,
        )

        assertObjectKeys(snapshotJson, listOf("epoch", "cities"))
        val snapshotRoot = JsonReader().parse(snapshotJson)
        assertTrue(snapshotRoot.get("epoch").isString)
        assertTrue(snapshotRoot.get("cities").isArray)
        assertObjectKeys(snapshotRoot.get("cities").child, listOf("handle", "name"))
        assertObjectKeys(requestJson, listOf("requestId", "epoch", "cityHandle", "name"))
        assertObjectKeys(
            receiptJson,
            listOf("requestId", "epoch", "cityHandle", "previousName", "name"),
        )

        for (encoded in listOf(snapshotJson, requestJson, receiptJson)) {
            assertFalse(encoded.contains("civID"))
            assertFalse(encoded.contains("CityManager"))
            assertFalse(encoded.contains("Civilization"))
            assertFalse(encoded.contains("location"))
        }
    }

    @Test
    fun `city command DTOs expose only detached values`() {
        assertFinalFields(
            OwnedCitiesSnapshot::class.java,
            linkedMapOf("epoch" to String::class.java, "cities" to List::class.java),
        )
        assertFinalFields(
            OwnedCitySummary::class.java,
            linkedMapOf("handle" to String::class.java, "name" to String::class.java),
        )
        assertFinalFields(
            RenameCityRequest::class.java,
            linkedMapOf(
                "requestId" to String::class.java,
                "epoch" to String::class.java,
                "cityHandle" to String::class.java,
                "name" to String::class.java,
            ),
        )
        assertFinalFields(
            RenameCityReceipt::class.java,
            linkedMapOf(
                "requestId" to String::class.java,
                "epoch" to String::class.java,
                "cityHandle" to String::class.java,
                "previousName" to String::class.java,
                "name" to String::class.java,
            ),
        )
    }

    private fun assertObjectKeys(encoded: String, keys: List<String>) {
        val root = JsonReader().parse(encoded)
        assertTrue(root.isObject)
        assertObjectKeys(root, keys)
    }

    private fun assertObjectKeys(root: JsonValue, keys: List<String>) {
        assertEquals(keys, root.childNames())
    }

    private fun assertFinalFields(type: Class<*>, expected: Map<String, Class<*>>) {
        val fields = type.declaredFields.filterNot { Modifier.isStatic(it.modifiers) }
        assertEquals(expected, fields.associate { it.name to it.type })
        assertTrue(fields.all { Modifier.isFinal(it.modifiers) })
    }

    private fun JsonValue.childNames(): List<String> = buildList {
        var value = child
        while (value != null) {
            add(requireNotNull(value.name))
            value = value.next
        }
    }
}
