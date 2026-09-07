package uncivai.mcp

import io.ktor.http.ContentType
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.server.application.Application
import io.ktor.server.application.ApplicationCallPipeline
import io.ktor.server.cio.CIO
import io.ktor.server.engine.embeddedServer
import io.ktor.server.response.header
import io.ktor.server.response.respondText
import io.ktor.util.AttributeKey
import io.modelcontextprotocol.kotlin.sdk.server.Server
import io.modelcontextprotocol.kotlin.sdk.server.mcpStatelessStreamableHttp
import kotlinx.coroutines.runBlocking
import uncivai.player.PlayerSession
import java.nio.charset.StandardCharsets
import java.security.MessageDigest

private const val DEFAULT_PORT = 8765
private const val LOOPBACK_HOST = "127.0.0.1"
private const val MCP_PATH = "/mcp"
private const val MCP_SESSION_ID_HEADER = "Mcp-Session-Id"
private const val LISTENER_GRACE_MILLIS = 1_000L
private const val LISTENER_TIMEOUT_MILLIS = 30_000L
private val AUTHENTICATED_PLAYER_KEY = AttributeKey<AuthenticatedPlayer>("authenticated-player")
private val TOKEN_PATTERN = Regex("[A-Za-z0-9_-]{43}")

/** A trusted host binding. Its credential and player facade are not exposed as public properties. */
class PlayerMcpBinding(
    internal val token: String,
    internal val session: PlayerSession,
)

/** Serves credential-bound player tools over stateless MCP Streamable HTTP on loopback. */
class PlayerMcpServer(
    bindings: List<PlayerMcpBinding>,
    private val port: Int = DEFAULT_PORT,
) : AutoCloseable {
    private val lifecycleLock = Any()
    private val authenticatedPlayers: List<AuthenticatedPlayer>
    private var listenerStop: (() -> Unit)? = null
    private var boundPort: Int? = null
    private var closed = false

    init {
        require(port in 0..65535) { "Port must be between 0 and 65535" }
        authenticatedPlayers = prepareAuthenticatedPlayers(bindings)
    }

    /** Starts the listener and returns its actual port. Repeated calls return the same port. */
    fun start(): Int = synchronized(lifecycleLock) {
        check(!closed) { "Player MCP server is closed" }
        boundPort?.let { return@synchronized it }

        val listener = embeddedServer(CIO, host = LOOPBACK_HOST, port = port) {
            configurePlayerMcp(authenticatedPlayers)
        }
        try {
            listener.start(wait = false)
            val actualPort = runBlocking { listener.engine.resolvedConnectors().single().port }
            listenerStop = { listener.stop(LISTENER_GRACE_MILLIS, LISTENER_TIMEOUT_MILLIS) }
            boundPort = actualPort
            actualPort
        } catch (failure: Throwable) {
            runCatching { listener.stop(0, LISTENER_TIMEOUT_MILLIS) }
            throw failure
        }
    }

    override fun close() {
        val stopListener = synchronized(lifecycleLock) {
            if (closed) return
            closed = true
            listenerStop.also {
                listenerStop = null
                boundPort = null
            }
        }

        var closeFailure: Throwable? = null
        try {
            stopListener?.invoke()
        } catch (failure: Throwable) {
            closeFailure = failure
        } finally {
            runBlocking {
                for (player in authenticatedPlayers) {
                    try {
                        player.server.close()
                    } catch (failure: Throwable) {
                        if (closeFailure == null) {
                            closeFailure = failure
                        } else {
                            closeFailure.addSuppressed(failure)
                        }
                    }
                }
            }
        }
        closeFailure?.let { throw it }
    }
}

private class AuthenticatedPlayer(
    val tokenHash: ByteArray,
    val server: Server,
)

private class PreparedPlayer(
    val tokenHash: ByteArray,
    val session: PlayerSession,
)

private fun prepareAuthenticatedPlayers(bindings: List<PlayerMcpBinding>): List<AuthenticatedPlayer> {
    require(bindings.size in 1..64) { "Between 1 and 64 player bindings are required" }
    val preparedPlayers = bindings.map { binding ->
        require(TOKEN_PATTERN.matches(binding.token)) { "Player credentials must be 256-bit Base64URL values" }
        PreparedPlayer(hashToken(binding.token), binding.session)
    }
    for (index in preparedPlayers.indices) {
        val player = preparedPlayers[index]
        require(preparedPlayers.take(index).none { MessageDigest.isEqual(it.tokenHash, player.tokenHash) }) {
            "Player credentials must be unique"
        }
        require(preparedPlayers.take(index).none { it.session === player.session }) {
            "Each player credential must use a distinct player session"
        }
    }
    return preparedPlayers.map { player ->
        AuthenticatedPlayer(
            tokenHash = player.tokenHash,
            server = createPlayerToolsServer(player.session),
        )
    }
}

private fun hashToken(token: String): ByteArray = MessageDigest.getInstance("SHA-256")
    .digest(token.toByteArray(StandardCharsets.UTF_8))

private fun Application.configurePlayerMcp(authenticatedPlayers: List<AuthenticatedPlayer>) {
    intercept(ApplicationCallPipeline.Plugins) {
        val authorizationHeaders = context.request.headers.getAll(HttpHeaders.Authorization)
        val token = authorizationHeaders
            ?.singleOrNull()
            ?.bearerTokenOrNull()
        val authenticatedPlayer = token?.let { presentedToken ->
            val presentedHash = hashToken(presentedToken)
            var match: AuthenticatedPlayer? = null
            for (candidate in authenticatedPlayers) {
                if (MessageDigest.isEqual(candidate.tokenHash, presentedHash)) match = candidate
            }
            match
        }
        if (authenticatedPlayer == null) {
            context.response.header(HttpHeaders.WWWAuthenticate, "Bearer")
            context.respondText(
                text = "Unauthorized",
                contentType = ContentType.Text.Plain,
                status = HttpStatusCode.Unauthorized,
            )
            finish()
            return@intercept
        }

        if (!context.request.headers.getAll(HttpHeaders.Origin).isNullOrEmpty()) {
            context.respondText(
                text = "Origin is not allowed",
                contentType = ContentType.Text.Plain,
                status = HttpStatusCode.Forbidden,
            )
            finish()
            return@intercept
        }

        if (!context.request.headers.getAll(MCP_SESSION_ID_HEADER).isNullOrEmpty()) {
            context.respondText(
                text = "MCP session IDs are not supported",
                contentType = ContentType.Text.Plain,
                status = HttpStatusCode.BadRequest,
            )
            finish()
            return@intercept
        }
        context.attributes.put(AUTHENTICATED_PLAYER_KEY, authenticatedPlayer)
    }

    mcpStatelessStreamableHttp(
        path = MCP_PATH,
        allowedHosts = listOf("127.0.0.1", "localhost"),
        allowedOrigins = emptyList(),
    ) {
        call.attributes[AUTHENTICATED_PLAYER_KEY].server
    }
}

private fun String.bearerTokenOrNull(): String? {
    val separator = indexOf(' ')
    if (separator <= 0 || !substring(0, separator).equals("Bearer", ignoreCase = true)) return null
    val credential = substring(separator + 1)
    return credential.takeIf { it.isNotEmpty() && it.none(Char::isWhitespace) }
}
