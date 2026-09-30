package uncivai

import com.networknt.schema.InputFormat
import com.networknt.schema.SchemaRegistry
import com.networknt.schema.SpecificationVersion
import kotlinx.serialization.DeserializationStrategy
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import uncivai.player.PlayerSessionException
import uncivai.player.PlayerSessionFailure

/** Only bundled schemas are compiled. Callers provide data, never schemas or reference locations. */
internal class InputSchema(name: String) {
    val document = Json.parseToJsonElement(
        checkNotNull(javaClass.getResourceAsStream("/uncivai/schemas/$name.json")) { "Missing schema: $name" }
            .bufferedReader().use { it.readText() },
    ).jsonObject
    private val validator = SchemaRegistry.withDefaultDialect(SpecificationVersion.DRAFT_2020_12)
        .getSchema(document.toString(), InputFormat.JSON).also { it.initializeValidators() }

    fun validate(arguments: JsonObject): JsonObject {
        if (validator.validate(arguments.toString(), InputFormat.JSON).isNotEmpty()) invalid()
        return arguments
    }

    fun <T> decode(serializer: DeserializationStrategy<T>, arguments: JsonObject): T {
        validate(arguments)
        return try {
            Json.decodeFromJsonElement(serializer, arguments)
        } catch (_: SerializationException) {
            invalid()
        } catch (_: IllegalArgumentException) {
            invalid()
        }
    }

    private fun invalid(): Nothing = throw PlayerSessionException(PlayerSessionFailure.INVALID_ARGUMENT)
}

internal object InputSchemas {
    val communication = InputSchema("communication")
    val turnCommand = InputSchema("turn-command")
    val details = InputSchema("details")
    val victoryProgress = InputSchema("victory-progress")
    val movementPreview = InputSchema("movement-preview")
    val playerInspect = InputSchema("player-inspect")
    val missionPreview = InputSchema("mission-preview")
    val empty = InputSchema("empty")
}
