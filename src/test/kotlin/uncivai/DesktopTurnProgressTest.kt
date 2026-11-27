package uncivai

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.nio.file.Files

class DesktopTurnProgressTest {
    @get:Rule val directory = TemporaryFolder()

    @Test fun `desktop progress presents only recognized stages for the displayed turn`() {
        val path = directory.root.toPath().resolve("progress.json")
        var now = 0L
        val progress = DesktopTurnProgress(path) { now }
        assertNull(progress.message("g1", 8, 17))

        fun write(stage: String) {
            Files.writeString(path, """{"epoch":"g1","turn":8,"handoff":17,"stage":"$stage"}""")
            now += 250_000_000
        }

        write("PREPARING")
        assertEquals("The AI is preparing its turn.", progress.message("g1", 8, 17))
        write("WAITING_FOR_MODEL")
        assertEquals("The AI is waiting to retry a model request.", progress.message("g1", 8, 17))
        assertNull(progress.message("g1", 9, 17))
        assertNull(progress.message("g1", 8, 18))
        assertNull(progress.message("g2", 8, 17))
        write("private details")
        assertNull(progress.message("g1", 8, 17))
    }
}
