package uncivai

import kotlinx.serialization.json.JsonObject
import java.nio.file.Files
import java.nio.file.LinkOption.NOFOLLOW_LINKS
import java.nio.file.Path
import java.nio.file.attribute.PosixFilePermissions
import java.sql.DriverManager

/** Operator-only monitoring records. Schema shared with civ_agent.monitoring. */
internal class MonitoringStore(private val path: Path) {
    data class Record(
        val collection: String,
        val key: String,
        val payload: JsonObject,
        val epoch: String? = null,
        val civilization: String? = null,
        val gameTurn: Int? = null,
    )

    fun write(records: List<Record>) {
        require(!Files.isSymbolicLink(path.parent) && !Files.isSymbolicLink(path))
        Files.createDirectories(path.parent, PosixFilePermissions.asFileAttribute(PosixFilePermissions.fromString("rwx------")))
        if (!Files.exists(path, NOFOLLOW_LINKS)) {
            try {
                Files.createFile(path, PosixFilePermissions.asFileAttribute(PosixFilePermissions.fromString("rw-------")))
            } catch (_: java.nio.file.FileAlreadyExistsException) {
                // The scheduler may create the shared database concurrently.
            }
        }
        require(Files.isRegularFile(path, NOFOLLOW_LINKS))
        require(Files.getPosixFilePermissions(path).all { it in PosixFilePermissions.fromString("rw-------") })
        DriverManager.getConnection("jdbc:sqlite:$path").use { db ->
            db.createStatement().use { statement ->
                statement.execute("PRAGMA busy_timeout=1000")
                statement.execute("PRAGMA journal_mode=WAL")
                statement.execute("PRAGMA synchronous=FULL")
                statement.execute(
                    """CREATE TABLE IF NOT EXISTS monitor_records (
                        revision INTEGER PRIMARY KEY AUTOINCREMENT,
                        collection VARCHAR NOT NULL, key VARCHAR NOT NULL,
                        epoch VARCHAR, civilization VARCHAR, game_turn INTEGER,
                        activation VARCHAR, sequence INTEGER, payload TEXT NOT NULL,
                        updated_ms INTEGER NOT NULL, UNIQUE(collection, key))""",
                )
                statement.execute("CREATE INDEX IF NOT EXISTS monitor_changes ON monitor_records(collection, revision)")
                statement.execute("CREATE INDEX IF NOT EXISTS monitor_turns ON monitor_records(collection, epoch, civilization, game_turn)")
                statement.execute("CREATE INDEX IF NOT EXISTS monitor_events ON monitor_records(collection, epoch, activation, sequence)")
            }
            db.autoCommit = false
            try {
                db.prepareStatement(
                    """INSERT OR REPLACE INTO monitor_records
                        (collection, key, payload, epoch, civilization, game_turn, updated_ms)
                        SELECT ?, ?, ?, ?, ?, ?, ? WHERE NOT EXISTS
                        (SELECT 1 FROM monitor_records WHERE collection=? AND key=? AND payload=?)""",
                ).use { statement ->
                    for (record in records) {
                        val payload = record.payload.toString()
                        statement.setString(1, record.collection)
                        statement.setString(2, record.key)
                        statement.setString(3, payload)
                        statement.setString(4, record.epoch)
                        statement.setString(5, record.civilization)
                        statement.setObject(6, record.gameTurn)
                        statement.setLong(7, System.currentTimeMillis())
                        statement.setString(8, record.collection)
                        statement.setString(9, record.key)
                        statement.setString(10, payload)
                        statement.addBatch()
                    }
                    statement.executeBatch()
                }
                db.commit()
            } catch (failure: Exception) {
                db.rollback()
                throw failure
            }
        }
    }
}
