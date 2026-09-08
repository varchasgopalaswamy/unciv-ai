package uncivai

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import java.nio.file.Path
import java.security.KeyStore
import java.security.cert.X509Certificate

/** Loaded TLS identity; deliberately has no generated string representation of its secrets. */
class HostTls internal constructor(
    internal val keyStore: KeyStore,
    internal val password: CharArray,
    internal val keyAlias: String,
    val listenHost: String,
    val publicHost: String,
)

@Serializable
private data class TlsConfiguration(
    val keyStorePath: String,
    val keyStorePasswordFile: String,
    val keyAlias: String,
    val listenHost: String,
    val publicHost: String,
)

internal fun loadHostTls(path: Path): HostTls {
    // Neither malformed JSON nor provider exceptions may echo password material to CLI logs.
    try {
        val config = Json.decodeFromString<TlsConfiguration>(privateConfigurationBytes(path).decodeToString(throwOnInvalidSequence = true))
        val hostname = Regex("[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
        require(hostname.matches(config.publicHost) && !config.publicHost.contains(".."))
        require(hostname.matches(config.listenHost) && !config.listenHost.contains(".."))
        require(config.publicHost != "0.0.0.0" && config.keyAlias.isNotBlank())
        val password = privateConfigurationBytes(Path.of(config.keyStorePasswordFile), 4096)
            .decodeToString(throwOnInvalidSequence = true).trimEnd('\r', '\n').toCharArray()
        require(password.isNotEmpty())
        val bytes = privateConfigurationBytes(Path.of(config.keyStorePath), 1_048_576)
        val keyStore = KeyStore.getInstance("PKCS12").apply { bytes.inputStream().use { load(it, password) } }
        require(keyStore.isKeyEntry(config.keyAlias))
        require(keyStore.getKey(config.keyAlias, password) != null)
        val certificate = keyStore.getCertificate(config.keyAlias) as X509Certificate
        certificate.checkValidity()
        return HostTls(keyStore, password, config.keyAlias, config.listenHost, config.publicHost.lowercase())
    } catch (_: Exception) {
        throw IllegalArgumentException("Invalid TLS configuration, private key, or certificate")
    }
}
