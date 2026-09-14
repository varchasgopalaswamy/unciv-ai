package uncivai

import com.networknt.schema.InputFormat
import com.networknt.schema.SchemaRegistry
import com.networknt.schema.SpecificationVersion
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import uncivai.mcp.COMMUNICATION_INPUT_SCHEMA
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure
import uncivai.player.PlayerTurnJson
import uncivai.player.TurnCommandKind
import uncivai.player.TurnCommandRequest

class InputSchemaTest {
    @Test
    fun `request contracts enforce types limits and operation fields without disclosing input`() {
        val sentinel = "secret-sentinel"
        val invalid = listOf(
            obj("op" to "read", "epoch" to "g1", "cursor" to "1"),
            obj("op" to "read", "epoch" to "g1", "cursor" to -1),
            obj("op" to "read", "epoch" to "g1", "limit" to 101),
            obj("op" to "read", "epoch" to "g1", "waitSeconds" to 26),
            obj("op" to "state", "epoch" to "g1", "waitSeconds" to 1),
            obj("op" to "send", "epoch" to "g1", "requestId" to "r1", "text" to sentinel, "to" to true),
            obj("op" to "send", "epoch" to "g1", "requestId" to "r1", "text" to " "),
            obj("op" to "send", "epoch" to "g1", "requestId" to "r1", "text" to sentinel.repeat(500)),
            obj("op" to "send", "epoch" to "g1", "requestId" to "r1", "text" to sentinel, "unknown" to sentinel),
            obj("op" to "job_claim", "epoch" to "g1", "job" to "j1"),
            obj("op" to "job_release", "epoch" to "g1", "job" to "j1", "usage" to obj("input_tokens" to -1)),
            obj("op" to "trade_decide", "epoch" to "g1", "requestId" to "r1", "proposal" to "d1", "version" to "1", "decision" to "accept"),
            obj("op" to "withdraw", "epoch" to "g1", "requestId" to "r1", "proposal" to "p1", "version" to 1),
            obj("op" to "not-an-operation", "epoch" to "g1"),
        )
        // Validate the actual schema encoded by the SDK, including nested $ref resolution.
        val advertised = SchemaRegistry.withDefaultDialect(SpecificationVersion.DRAFT_2020_12)
            .getSchema(Json.encodeToString(COMMUNICATION_INPUT_SCHEMA), InputFormat.JSON)
        for (request in invalid) {
            val error = assertThrows(PlayerSessionException::class.java) { InputSchemas.communication.decode(CommunicationRequest.serializer(), request) }
            assertEquals(PlayerSessionFailure.INVALID_ARGUMENT, error.reason)
            assertFalse(error.toString().contains(sentinel))
            assertTrue(advertised.validate(obj("request" to request).toString(), InputFormat.JSON).isNotEmpty())
        }
        val valid = listOf(
            obj("op" to "read", "epoch" to "g1", "cursor" to 0, "limit" to 100, "waitSeconds" to 25),
            obj("op" to "send", "epoch" to "g1", "requestId" to "r1", "text" to "Hello", "to" to "Greece"),
            obj("op" to "trade_decide", "epoch" to "g1", "requestId" to "r2", "proposal" to "d1", "version" to 1, "decision" to "decline"),
            obj("op" to "job_release", "epoch" to "g1", "job" to "j1", "usage" to obj("input_tokens" to 2147483648L)),
        )
        for (request in valid) {
            InputSchemas.communication.decode(CommunicationRequest.serializer(), request)
            assertTrue(advertised.validate(obj("request" to request).toString(), InputFormat.JSON).isEmpty())
        }
    }

    @Test
    fun `trade term validation rejects malformed nested values and incomplete counters`() {
        val term = obj("type" to "Gold", "name" to "Gold", "amount" to 10, "duration" to -1)
        val offer = obj("op" to "trade_offer", "epoch" to "g1", "requestId" to "r1", "partner" to "Greece", "give" to listOf(term), "receive" to emptyList<String>())
        assertEquals(10, InputSchemas.communication.decode(CommunicationRequest.serializer(), offer).give.single().amount)
        for (bad in listOf(
            JsonObject(offer + obj("proposal" to "d1")),
            JsonObject(offer + obj("version" to 1)),
            JsonObject(offer + obj("give" to listOf(JsonObject(term + obj("amount" to "10"))))),
            JsonObject(offer + obj("give" to listOf(JsonObject(term + obj("amount" to 0))))),
            JsonObject(offer + obj("give" to listOf(JsonObject(term + obj("extra" to true))))),
        )) {
            assertThrows(PlayerSessionException::class.java) { InputSchemas.communication.validate(bad) }
        }
    }

    @Test
    fun `game command schema matches serialized defaults enums and full integer bounds`() {
        val schemaKinds = InputSchemas.turnCommand.document.getValue("properties").jsonObject.getValue("kind").jsonObject.getValue("enum").jsonArray.map { it.jsonPrimitive.content }.toSet()
        assertEquals(TurnCommandKind.entries.map { it.name }.toSet(), schemaKinds)
        for (kind in TurnCommandKind.entries) {
            val command = TurnCommandRequest("r1", "g1", "rev1", kind, socialVersion = 4)
            val encoded = Json.parseToJsonElement(PlayerTurnJson.encode(command)).jsonObject
            assertEquals(command, InputSchemas.turnCommand.decode(TurnCommandRequest.serializer(), encoded))
        }
        val command = obj("requestId" to "r1", "epoch" to "g1", "revision" to "rev1", "kind" to "MOVE_UNIT")
        for (position in listOf(obj("x" to "1", "y" to 2), obj("x" to 2147483648L, "y" to 2), obj("x" to 1))) {
            assertThrows(PlayerSessionException::class.java) { InputSchemas.turnCommand.validate(JsonObject(command + obj("destination" to position))) }
        }
        assertThrows(PlayerSessionException::class.java) { InputSchemas.details.validate(obj("names" to listOf("Pottery", "Pottery"))) }
        assertThrows(PlayerSessionException::class.java) { InputSchemas.empty.validate(obj("hidden" to JsonPrimitive(true))) }
    }
}
