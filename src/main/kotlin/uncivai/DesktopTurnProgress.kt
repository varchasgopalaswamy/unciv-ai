package uncivai

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import java.nio.file.Files
import java.nio.file.Path

/** Reads only the coarse turn stages intended for the waiting player. */
internal class DesktopTurnProgress(private val path: Path, private val nanoTime: () -> Long = System::nanoTime) {
    @Serializable
    private enum class Stage(val message: String) {
        PREPARING("The AI is preparing its turn."),
        REVIEWING("The AI is reviewing the position."),
        PLAYING("The AI is making decisions and taking actions."),
        WAITING_FOR_MODEL("The AI is waiting to retry a model request."),
        FINISHING("The AI is finishing its turn."),
    }

    @Serializable
    private data class Progress(val epoch: String, val turn: Int, val handoff: Long, val stage: Stage)

    private var nextRead = nanoTime()
    private var cached: Progress? = null

    fun message(epoch: String, turn: Int, handoff: Long): String? {
        val now = nanoTime()
        if (now - nextRead >= 0) {
            nextRead = now + 250_000_000
            cached = runCatching {
                Files.newInputStream(path).use { stream ->
                    val bytes = stream.readNBytes(1025)
                    if (bytes.size <= 1024) Json.decodeFromString<Progress>(bytes.decodeToString()) else null
                }
            }.getOrNull()
        }
        return cached?.takeIf { it.epoch == epoch && it.turn == turn && it.handoff == handoff }?.stage?.message
    }
}
