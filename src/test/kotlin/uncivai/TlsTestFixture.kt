package uncivai

import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.attribute.PosixFileAttributeView
import java.nio.file.attribute.PosixFilePermissions
import java.security.KeyStore
import java.util.UUID
import java.util.concurrent.TimeUnit
import javax.net.ssl.SSLContext
import javax.net.ssl.TrustManagerFactory

internal class TlsTestFixture(root: Path) {
    val config: Path = root.resolve("tls.json")
    val tls: HostTls
    val context: SSLContext

    init {
        val keyStore = root.resolve("host.p12")
        val passwordFile = privateFile(root.resolve("password"), UUID.randomUUID().toString())
        val log = root.resolve("keytool.log")
        val generator = ProcessBuilder(
            Path.of(System.getProperty("java.home"), "bin", "keytool").toString(),
            "-genkeypair", "-alias", "host", "-keyalg", "RSA", "-keysize", "2048", "-validity", "2",
            "-storetype", "PKCS12", "-keystore", keyStore.toString(),
            "-storepass:file", passwordFile.toString(), "-dname", "CN=localhost",
            "-ext", "SAN=dns:localhost", "-noprompt",
        ).redirectErrorStream(true).redirectOutput(log.toFile()).start()
        try {
            check(generator.waitFor(20, TimeUnit.SECONDS)) { "Certificate generation timed out" }
            check(generator.exitValue() == 0) { "Certificate generation failed" }
        } finally {
            if (generator.isAlive) generator.destroyForcibly()
        }
        makePrivate(keyStore)
        privateFile(
            config,
            buildJsonObject {
                put("keyStorePath", keyStore.toString())
                put("keyStorePasswordFile", passwordFile.toString())
                put("keyAlias", "host")
                put("listenHost", "127.0.0.1")
                put("publicHost", "localhost")
            }.toString(),
        )
        tls = loadHostTls(config)
        val trustStore = KeyStore.getInstance("PKCS12").apply {
            load(null, null)
            setCertificateEntry("test-ca", tls.keyStore.getCertificate("host"))
        }
        val trust = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm()).apply { init(trustStore) }
        context = SSLContext.getInstance("TLS").apply { init(null, trust.trustManagers, null) }
    }

    private fun privateFile(path: Path, content: String): Path {
        Files.createFile(path)
        makePrivate(path)
        return Files.writeString(path, content)
    }

    private fun makePrivate(path: Path) {
        if (Files.getFileAttributeView(path, PosixFileAttributeView::class.java) != null) {
            Files.setPosixFilePermissions(path, PosixFilePermissions.fromString("rw-------"))
        }
    }
}
