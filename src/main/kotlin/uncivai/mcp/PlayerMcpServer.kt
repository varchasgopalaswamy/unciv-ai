package uncivai.mcp

import io.ktor.http.ContentType
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.server.application.Application
import io.ktor.server.application.ApplicationCallPipeline
import io.ktor.server.cio.CIO
import io.ktor.server.engine.embeddedServer
import io.ktor.server.engine.sslConnector
import io.ktor.server.netty.Netty
import io.ktor.server.request.path
import io.ktor.server.request.receiveChannel
import io.ktor.server.response.header
import io.ktor.server.response.respondText
import io.ktor.server.routing.post
import io.ktor.server.routing.routing
import io.ktor.util.AttributeKey
import io.ktor.utils.io.readRemaining
import io.modelcontextprotocol.kotlin.sdk.server.Server
import io.modelcontextprotocol.kotlin.sdk.server.mcpStatelessStreamableHttp
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.isActive
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import kotlinx.io.readByteArray
import kotlinx.serialization.SerializationException
import uncivai.HostTls
import uncivai.OperatorControl
import uncivai.OperatorFailure
import uncivai.OperatorRequest
import uncivai.RuntimeHaltedException
import uncivai.player.PlayerSession
import java.nio.charset.StandardCharsets
import java.security.MessageDigest

private const val DEFAULT_PORT = 8765
private const val LOOPBACK_HOST = "127.0.0.1"
private const val MCP_PATH = "/mcp"
private const val OPERATOR_PATH = "/operator"
private const val MCP_SESSION_ID_HEADER = "Mcp-Session-Id"
private const val LISTENER_GRACE_MILLIS = 1_000L
private const val LISTENER_TIMEOUT_MILLIS = 30_000L
private val AUTHENTICATED_PLAYER_KEY = AttributeKey<AuthenticatedPlayer>("authenticated-player")
private val REQUEST_SERVER_KEY = AttributeKey<Server>("request-server")
private val TOKEN_PATTERN = Regex("[A-Za-z0-9_-]{43}")

/** A trusted host binding. Its credential and player facade are not exposed as public properties. */
class PlayerMcpBinding(
    internal val token: String,
    internal val session: PlayerSession,
)

/** Operator credentials are never accepted as player credentials or exposed through MCP tools. */
class OperatorMcpBinding(internal val token: String, internal val control: OperatorControl)

/** Serves credential-bound player tools. Production CLI callers supply a TLS identity. */
class PlayerMcpServer(
    bindings: List<PlayerMcpBinding>,
    private val port: Int = DEFAULT_PORT,
    private val operator: OperatorMcpBinding? = null,
    private val tls: HostTls? = null,
) : AutoCloseable {
    private val lifecycleLock = Any()
    private val authenticatedPlayers: List<AuthenticatedPlayer>
    private var listenerStop: (() -> Unit)? = null
    private var boundPort: Int? = null
    private var closed = false

    init {
        require(port in 0..65535) { "Port must be between 0 and 65535" }
        if (operator != null) {
            require(TOKEN_PATTERN.matches(operator.token)) { "Operator credential must be a 256-bit Base64URL value" }
            require(bindings.none { MessageDigest.isEqual(hashToken(it.token), hashToken(operator.token)) }) { "Operator and player credentials must be distinct" }
        }
        authenticatedPlayers = prepareAuthenticatedPlayers(
            bindings.map { binding ->
                PlayerMcpBinding(binding.token, operator?.control?.manualSession(binding.session, binding.token) ?: binding.session)
            },
        )
    }

    /** Starts the listener and returns its actual port. Repeated calls return the same port. */
    fun start(): Int = synchronized(lifecycleLock) {
        check(!closed) { "Player MCP server is closed" }
        boundPort?.let { return@synchronized it }

        val allowedHosts = tls?.let { listOf(it.publicHost) } ?: listOf(LOOPBACK_HOST, "localhost")
        val module: suspend Application.() -> Unit = {
            configurePlayerMcp(authenticatedPlayers, operator, allowedHosts)
        }
        // Cleartext is retained only for embedded protocol fixtures. The CLI requires TLS.
        val listener = if (tls == null) {
            embeddedServer(CIO, host = LOOPBACK_HOST, port = port, module = module)
        } else {
            embeddedServer(Netty, configure = {
                connectionGroupSize = 1
                workerGroupSize = 2
                callGroupSize = 2
                // The MCP transport and authority validation use HTTP/1.1 Host headers.
                enableHttp2 = false
                requestReadTimeoutSeconds = 30
                sslConnector(tls.keyStore, tls.keyAlias, { tls.password.copyOf() }, { tls.password.copyOf() }) {
                    host = tls.listenHost
                    this.port = this@PlayerMcpServer.port
                    enabledProtocols = listOf("TLSv1.3", "TLSv1.2")
                }
            }, module = module)
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

        stopListener?.invoke()
    }
}

private class AuthenticatedPlayer(
    val tokenHash: ByteArray,
    val session: PlayerSession,
)

private fun prepareAuthenticatedPlayers(bindings: List<PlayerMcpBinding>): List<AuthenticatedPlayer> {
    require(bindings.size in 1..64) { "Between 1 and 64 player bindings are required" }
    val preparedPlayers = bindings.map { binding ->
        require(TOKEN_PATTERN.matches(binding.token)) { "Player credentials must be 256-bit Base64URL values" }
        AuthenticatedPlayer(hashToken(binding.token), binding.session)
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
    return preparedPlayers
}

private fun hashToken(token: String): ByteArray = MessageDigest.getInstance("SHA-256")
    .digest(token.toByteArray(StandardCharsets.UTF_8))

private fun Application.configurePlayerMcp(
    authenticatedPlayers: List<AuthenticatedPlayer>,
    operator: OperatorMcpBinding?,
    allowedHosts: List<String>,
) {
    intercept(ApplicationCallPipeline.Monitoring) {
        try {
            proceed()
        } catch (failure: Throwable) {
            // Exception messages may contain request bodies or credentials. Keep only types and frames.
            if (failure !is CancellationException || currentCoroutineContext().isActive) {
                val trace = generateSequence(failure) { it.cause }.take(4).joinToString("\nCaused by: ") {
                    it.javaClass.name + "\n" + it.stackTrace.take(20).joinToString("\n") { frame -> "  at $frame" }
                }
                java.util.logging.Logger.getLogger("uncivai.mcp").severe("MCP HTTP request failed:\n$trace")
            }
            throw failure
        } finally {
            // Stateless MCP sessions belong to one HTTP request. Closing a cached server while
            // another request is polling cancels its pending response, producing an HTTP 500.
            context.attributes.getOrNull(REQUEST_SERVER_KEY)?.let { server ->
                withContext(NonCancellable) { server.close() }
            }
        }
    }
    intercept(ApplicationCallPipeline.Plugins) {
        val authorizationHeaders = context.request.headers.getAll(HttpHeaders.Authorization)
        val token = authorizationHeaders
            ?.singleOrNull()
            ?.bearerTokenOrNull()
        val operatorRequest = context.request.path() == OPERATOR_PATH && operator != null
        val authenticatedOperator = operatorRequest && token != null && MessageDigest.isEqual(hashToken(token), hashToken(operator.token))
        val authenticatedPlayer = if (operatorRequest) {
            null
        } else {
            token?.let { presentedToken ->
                val presentedHash = hashToken(presentedToken)
                var match: AuthenticatedPlayer? = null
                for (candidate in authenticatedPlayers) {
                    if (MessageDigest.isEqual(candidate.tokenHash, presentedHash)) match = candidate
                }
                if (match == null && operator != null) {
                    // Resolve authority for every request, including long-lived communication grants.
                    val session = operator.control.activationSession(presentedToken)
                    if (session != null) {
                        match = AuthenticatedPlayer(presentedHash, session)
                    }
                }
                match
            }
        }
        if (authenticatedPlayer == null && !authenticatedOperator) {
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

        val host = context.request.headers.getAll(HttpHeaders.Host)?.singleOrNull()
        val hostname = host?.substringBefore(':')?.lowercase()
        if (host == null || hostname !in allowedHosts || !Regex("[A-Za-z0-9.-]+(?::[0-9]{1,5})?").matches(host)) {
            context.respondText("Host is not allowed", ContentType.Text.Plain, HttpStatusCode.Forbidden)
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
        if (authenticatedPlayer != null) context.attributes.put(AUTHENTICATED_PLAYER_KEY, authenticatedPlayer)
    }

    if (operator != null) {
        routing {
            post(OPERATOR_PATH) {
                try {
                    val bytes = call.receiveChannel().readRemaining(33_554_433).readByteArray()
                    if (bytes.size > 33_554_432) throw OperatorFailure("INVALID_ARGUMENT")
                    val text = bytes.decodeToString()
                    val request = OperatorControl.json.decodeFromString<OperatorRequest>(text)
                    val response = operator.control.handle(request)
                    call.respondText(OperatorControl.json.encodeToString(response), ContentType.Application.Json)
                } catch (_: SerializationException) {
                    call.respondText("{\"error\":\"INVALID_ARGUMENT\"}", ContentType.Application.Json, HttpStatusCode.BadRequest)
                } catch (failure: OperatorFailure) {
                    val status = when (failure.code) {
                        "INVALID_ARGUMENT" -> HttpStatusCode.BadRequest
                        "GAME_HALTED" -> HttpStatusCode.ServiceUnavailable
                        else -> HttpStatusCode.Conflict
                    }
                    call.respondText("{\"error\":\"${failure.code}\"}", ContentType.Application.Json, status)
                } catch (_: RuntimeHaltedException) {
                    call.respondText("{\"error\":\"GAME_HALTED\"}", ContentType.Application.Json, HttpStatusCode.ServiceUnavailable)
                }
            }
        }
    }

    mcpStatelessStreamableHttp(
        path = MCP_PATH,
        allowedHosts = allowedHosts,
        allowedOrigins = emptyList(),
    ) {
        createPlayerToolsServer(call.attributes[AUTHENTICATED_PLAYER_KEY].session).also {
            call.attributes.put(REQUEST_SERVER_KEY, it)
        }
    }
}

private fun String.bearerTokenOrNull(): String? {
    val separator = indexOf(' ')
    if (separator <= 0 || !substring(0, separator).equals("Bearer", ignoreCase = true)) return null
    val credential = substring(separator + 1)
    return credential.takeIf { it.isNotEmpty() && it.none(Char::isWhitespace) }
}
