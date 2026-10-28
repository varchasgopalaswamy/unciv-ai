package uncivai

import com.unciv.logic.map.HexCoord
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import java.nio.file.Files
import java.nio.file.Path

/** Measures copies of historical maps, using archived opening coordinates rather than regenerating seeds. */
object BenchmarkStartAssessment {
    @Serializable
    data class Position(val x: Int = 0, val y: Int = 0)

    @Serializable
    data class Case(
        val id: String,
        val gameFile: String,
        val majorStarts: Map<String, Position>,
        val minorStarts: List<Position>,
    )

    @JvmStatic
    fun main(args: Array<String>) {
        require(args.size == 2) { "manifest-json output-directory" }
        val json = Json { prettyPrint = true; ignoreUnknownKeys = true }
        val cases = json.decodeFromString<List<Case>>(Files.readString(Path.of(args[0])))
        val root = Path.of(args[1]).toAbsolutePath()
        Files.createDirectories(root)
        for (case in cases) {
            HeadlessRuntime.load(Path.of(case.gameFile), root.resolve(case.id)).use { runtime ->
                val map = runtime.game.tileMap
                val majors = case.majorStarts.mapValues { (_, p) -> map[HexCoord.of(p.x, p.y)] }
                val minors = case.minorStarts.map { p -> map[HexCoord.of(p.x, p.y)] }
                val assessment = MapBalanceSearch.evaluate(runtime.game, majors, minors)
                Files.writeString(root.resolve("${case.id}.json"), json.encodeToString(assessment))
                println("${case.id}: scores=${assessment.starts.map { it.nativeScore }} land=${assessment.starts.map { it.expansionLand }}")
            }
        }
    }
}
