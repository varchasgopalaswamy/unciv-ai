package uncivai

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.file.Files

class HostTlsTest {
    @Test
    fun `TLS identity requires a private valid key and never quotes a password on failure`() {
        val root = Files.createTempDirectory("host-tls-")
        try {
            val fixture = TlsTestFixture(root)
            val password = Files.readString(root.resolve("password"))
            assertEquals("localhost", fixture.tls.publicHost)
            assertFalse(fixture.tls.toString().contains(password))
            val valid = Files.readString(fixture.config)
            for (invalid in listOf(valid.replace("localhost", "example.com/path"), valid.dropLast(1) + ",\"password\":\"$password\"}", valid.replace("host.p12", "missing.p12"))) {
                Files.writeString(fixture.config, invalid)
                val failure = runCatching { loadHostTls(fixture.config) }.exceptionOrNull()
                assertEquals("Invalid TLS configuration, private key, or certificate", failure?.message)
                assertNull(failure?.cause)
            }
            Files.writeString(fixture.config, valid)
            Files.writeString(root.resolve("password"), "wrong-$password")
            assertTrue(runCatching { loadHostTls(fixture.config) }.isFailure)
        } finally {
            deleteTreeWithoutFollowingLinks(root)
        }
    }
}
