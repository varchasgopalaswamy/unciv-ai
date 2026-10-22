package uncivai

import org.junit.Assert.*
import org.junit.Test

class ComputingCapacityTest {
    private val main = CommunicationJob("Rome", "gameplay", 100)
    private val economy = CommunicationJob("Rome", "economy", 100, parentJob = "main")
    private fun available(jobs: Map<String, CommunicationJob>, id: String, purpose: String, parent: String? = null, global: Int = 2, perCiv: Int = 2) =
        computingCapacityAvailable(jobs, id, "Rome", purpose, parent, 1, global, perCiv)

    @Test fun `main reserves its partner against both global and civilization limits`() {
        assertFalse(available(emptyMap(), "main", "gameplay", global = 1))
        assertFalse(available(emptyMap(), "main", "gameplay", perCiv = 1))
        assertTrue(available(emptyMap(), "main", "gameplay"))
        val jobs = mapOf("main" to main)
        assertFalse(available(jobs, "chat", "communication"))
        assertTrue(available(jobs, "economy", "economy", "main"))
    }

    @Test fun `suspended roles free capacity and cannot resume over either limit`() {
        val jobs = mapOf("main" to main.copy(computing = false), "economy" to economy.copy(computing = false))
        assertTrue(available(jobs, "chat", "communication"))
        val full = jobs + mapOf("chat" to CommunicationJob("Rome", "communication", 100),
            "other" to CommunicationJob("Greece", "communication", 100))
        assertFalse(available(full, "main", "gameplay"))
        assertFalse(available(full, "economy", "economy", "main"))
        assertFalse(available(full - "other", "main", "gameplay", global = 4, perCiv = 1))
    }

    @Test fun `retry of a suspended parent does not consume a nonexistent reserved slot`() {
        val jobs = mapOf("main" to main.copy(computing = false), "chat" to CommunicationJob("Greece", "communication", 100))
        assertFalse(available(jobs, "retry", "economy", "main", global = 1))
        assertTrue(available(jobs, "retry", "economy", "main", global = 2))
        assertFalse(available(jobs + ("old-economy" to economy.copy(parentJob = "previous", computing = false)), "new-main", "gameplay"))
    }

    @Test fun `invocation budget admits both partners together and charges retries separately`() {
        assertFalse(invocationCapacityAvailable(emptyMap(), "gameplay", null, 1, 0, 1))
        assertTrue(invocationCapacityAvailable(emptyMap(), "gameplay", null, 1, 0, 2))
        assertFalse(invocationCapacityAvailable(mapOf("main" to main), "communication", null, 1, 1, 2))
        assertTrue(invocationCapacityAvailable(mapOf("main" to main), "economy", "main", 1, 1, 2))
        assertFalse(invocationCapacityAvailable(mapOf("main" to main, "economy" to economy.copy(expires = 0)), "economy", "main", 1, 2, 2))
    }
}
