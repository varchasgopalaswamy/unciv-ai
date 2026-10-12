package uncivai

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.decodeFromJsonElement
import java.nio.file.Files
import java.nio.file.LinkOption.NOFOLLOW_LINKS
import java.nio.file.Path
import java.nio.file.attribute.PosixFilePermissions
import java.sql.DriverManager
import java.util.UUID
import java.util.concurrent.CompletableFuture
import java.util.concurrent.Executors

/** Small, immutable writes. A game checkpoint carries outstanding batches as a recovery outbox. */
@Serializable
internal data class AccountingRow(val collection: String, val key: String, val payload: JsonObject?)

@Serializable
internal data class AccountingBatch(val id: String = UUID.randomUUID().toString(), val rows: List<AccountingRow>)

@Serializable
internal data class AccountingMetadata(
    val telemetry: TelemetryState,
    val invocations: Long,
    val maxInvocations: Long,
    val maxConcurrent: Int,
    val maxPerCivilization: Int,
)

internal fun CommunicationState.accountingMetadata() = AccountingMetadata(
    telemetry.copy(turns = linkedMapOf(), windows = linkedMapOf(), pendingCalls = mutableMapOf()),
    invocations,
    maxInvocations,
    maxConcurrent,
    maxPerCivilization,
)

/** Operator accounting is never rewound with game state. Only this host writes this database. */
internal class HostAccountingStore(val path: Path) : AutoCloseable {
    private val writer = Executors.newSingleThreadExecutor { task -> Thread(task, "host-accounting").apply { isDaemon = true } }
    private val pending = linkedMapOf<String, AccountingBatch>()
    private val submitted = mutableMapOf<String, CompletableFuture<Unit>>()

    @Volatile var failure: Throwable? = null
        private set
    var onCommitted: (List<AccountingRow>) -> Unit = {}
    val id: String
    private val db: java.sql.Connection

    init {
        require(!Files.isSymbolicLink(path) && !Files.isSymbolicLink(path.parent))
        Files.createDirectories(path.parent)
        if (!Files.exists(path, NOFOLLOW_LINKS)) Files.createFile(path, PosixFilePermissions.asFileAttribute(PosixFilePermissions.fromString("rw-------")))
        require(Files.isRegularFile(path, NOFOLLOW_LINKS))
        require(Files.getPosixFilePermissions(path).all { it in PosixFilePermissions.fromString("rw-------") })
        db = DriverManager.getConnection("jdbc:sqlite:$path")
        db.createStatement().use {
            it.execute("PRAGMA journal_mode=WAL")
            it.execute("PRAGMA synchronous=FULL")
            it.execute("PRAGMA busy_timeout=1000")
            it.execute("CREATE TABLE IF NOT EXISTS identity (id TEXT NOT NULL, version INTEGER NOT NULL)")
            it.execute("CREATE TABLE IF NOT EXISTS records (collection TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(collection, key))")
            it.execute("CREATE TABLE IF NOT EXISTS commits (id TEXT PRIMARY KEY)")
        }
        id = db.createStatement().use { statement ->
            statement.executeQuery("SELECT id,version FROM identity").use { rows ->
                if (rows.next()) {
                    check(rows.getInt(2) == 1) { "Unsupported host accounting format" }
                    rows.getString(1)
                } else {
                    UUID.randomUUID().toString().also { value ->
                        db.prepareStatement("INSERT INTO identity VALUES (?,1)").use {
                            it.setString(1, value)
                            it.executeUpdate()
                        }
                    }
                }
            }
        }
    }

    @Synchronized fun stage(batch: AccountingBatch) {
        checkHealthy()
        pending[batch.id] = batch
    }

    @Synchronized fun outbox(): List<AccountingBatch> = pending.values.toList()

    @Synchronized fun submit(batch: AccountingBatch): CompletableFuture<Unit> {
        stage(batch)
        return submitted.getOrPut(batch.id) {
            CompletableFuture.supplyAsync({
                try {
                    checkHealthy()
                    HostTimings.measure("accounting.commit") { apply(batch) }
                    onCommitted(batch.rows)
                    synchronized(this) { pending.remove(batch.id) }
                    Unit
                } catch (error: Throwable) {
                    failure = error
                    throw error
                } finally {
                    synchronized(this) { submitted.remove(batch.id) }
                }
            }, writer)
        }
    }

    fun checkHealthy() {
        failure?.let { throw IllegalStateException("Host accounting persistence failed", it) }
    }

    private fun apply(batch: AccountingBatch) {
        db.autoCommit = false
        try {
            val fresh = db.prepareStatement("INSERT OR IGNORE INTO commits(id) VALUES (?)").use {
                it.setString(1, batch.id)
                it.executeUpdate() == 1
            }
            if (fresh) {
                db.prepareStatement("INSERT OR REPLACE INTO records VALUES (?,?,?)").use { upsert ->
                    db.prepareStatement("DELETE FROM records WHERE collection=? AND key=?").use { delete ->
                        for (row in batch.rows) {
                            val statement = if (row.payload == null) delete else upsert
                            statement.setString(1, row.collection)
                            statement.setString(2, row.key)
                            if (row.payload != null) statement.setString(3, row.payload.toString())
                            statement.executeUpdate()
                        }
                    }
                }
            }
            db.commit()
        } catch (error: Throwable) {
            db.rollback()
            throw error
        } finally {
            db.autoCommit = true
        }
    }

    fun rows(): List<AccountingRow> = CompletableFuture.supplyAsync({
        checkHealthy()
        db.createStatement().use { statement ->
            statement.executeQuery("SELECT collection,key,payload FROM records").use { rows ->
                buildList { while (rows.next()) add(AccountingRow(rows.getString(1), rows.getString(2), Json.decodeFromString<JsonObject>(rows.getString(3)))) }
            }
        }
    }, writer).join()

    /** Imports format 8 once. Repeating an interrupted import cannot overwrite newer reports. */
    fun importLegacy(state: CommunicationState, sourceId: String) {
        val rows = buildList {
            add(AccountingRow("meta", "accounting", state.accountingMetadata().recordJson()))
            state.jobs.forEach { (key, value) -> add(AccountingRow("jobs", key, value.recordJson())) }
            state.telemetry.turns.forEach { (key, value) -> add(AccountingRow("turns", key, value.recordJson())) }
            state.telemetry.windows.forEach { (key, value) -> add(AccountingRow("windows", key, value.recordJson())) }
            state.telemetry.pendingCalls.forEach { (key, value) -> add(AccountingRow("pending", key, value.recordJson())) }
        }
        submit(AccountingBatch("import:$sourceId", rows)).join()
    }

    fun restore(core: CommunicationState): CommunicationState {
        val rows = rows()
        val meta = rows.single { it.collection == "meta" && it.key == "accounting" }.payload!!
        val saved = Json.decodeFromJsonElement<AccountingMetadata>(meta)
        val telemetry = saved.telemetry
        val jobs = mutableMapOf<String, CommunicationJob>()
        for (row in rows) {
            when (row.collection) {
                "jobs" -> jobs[row.key] = Json.decodeFromJsonElement(row.payload!!)
                "turns" -> telemetry.turns[row.key] = Json.decodeFromJsonElement(row.payload!!)
                "windows" -> telemetry.windows[row.key] = Json.decodeFromJsonElement(row.payload!!)
                "pending" -> telemetry.pendingCalls[row.key] = Json.decodeFromJsonElement(row.payload!!)
            }
        }
        return core.copy(telemetry = telemetry, jobs = jobs, invocations = saved.invocations, maxInvocations = saved.maxInvocations, maxConcurrent = saved.maxConcurrent, maxPerCivilization = saved.maxPerCivilization)
    }

    override fun close() {
        try {
            CompletableFuture.runAsync({ db.close() }, writer).join()
        } finally {
            writer.shutdown()
        }
    }
}
