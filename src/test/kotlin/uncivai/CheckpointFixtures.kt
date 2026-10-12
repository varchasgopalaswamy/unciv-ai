package uncivai

import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.attribute.PosixFilePermissions
import java.sql.DriverManager

/** Copy a stopped or quiescent fixture, including committed records still in its WAL. */
internal fun copyCheckpoint(source: Path, target: Path) {
    Files.copy(source, target)
    val accounting = source.resolveSibling("${source.fileName}.accounting.sqlite3")
    if (!Files.exists(accounting)) return // A legacy checkpoint carries its own accounting.
    val destination = target.resolveSibling("${target.fileName}.accounting.sqlite3")
    Files.createFile(destination, PosixFilePermissions.asFileAttribute(PosixFilePermissions.fromString("rw-------")))
    DriverManager.getConnection("jdbc:sqlite:$accounting").use { db ->
        db.prepareStatement("VACUUM INTO ?").use {
            it.setString(1, destination.toString())
            it.execute()
        }
    }
}
