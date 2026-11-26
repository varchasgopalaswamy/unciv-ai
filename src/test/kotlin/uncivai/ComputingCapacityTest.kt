package uncivai

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ComputingCapacityTest {
    @Test fun `role wait releases compute capacity without ending the job`() {
        assertFalse(computingJobPhase("WAITING_ROLE"))
        assertFalse(computingJobPhase("WAITING_NEGOTIATION"))
        assertFalse(computingJobPhase("ROLE_FINISHED"))
        assertTrue(computingJobPhase("RUNNING"))
        assertTrue(computingJobPhase("STARTING"))
        val jobs = mapOf("main" to main.copy(computing = computingJobPhase("WAITING_ROLE")), "economy" to economy)
        assertTrue(available(jobs, "chat", "communication"))
        assertFalse(available(jobs + ("chat" to CommunicationJob("Rome", "communication", 100)), "main", "gameplay"))
    }

    private val main = CommunicationJob("Rome", "gameplay", 100)
    private val economy = CommunicationJob("Rome", "economy", 100, parentJob = "main")
    private fun available(jobs: Map<String, CommunicationJob>, id: String, purpose: String, parent: String? = null, global: Int = 2, perCiv: Int = 2) = computingCapacityAvailable(jobs, id, "Rome", purpose, parent, 1, global, perCiv)

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
        val full = jobs + mapOf(
            "chat" to CommunicationJob("Rome", "communication", 100),
            "other" to CommunicationJob("Greece", "communication", 100),
        )
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

    @Test fun `zero lifetime invocation limit allows work after arbitrary prior usage`() {
        val jobs = mapOf("main" to main.copy(militaryPartnerRequired = true))
        for (used in listOf(0L, 1000L, 10000L, 1000000L)) {
            assertTrue(invocationCapacityAvailable(jobs, "communication", null, 1, used, 0))
            assertTrue(invocationCapacityAvailable(jobs, "military", "main", 1, used, 0, true))
        }
    }

    @Test fun `military partner reserves three roles before the partner starts`() {
        assertFalse(computingCapacityAvailable(emptyMap(), "main", "Rome", "gameplay", null, 1, 2, 4, true))
        assertTrue(computingCapacityAvailable(emptyMap(), "main", "Rome", "gameplay", null, 1, 3, 3, true))
        val jobs = mapOf("main" to main.copy(militaryPartnerRequired = true))
        assertFalse(computingCapacityAvailable(jobs, "chat", "Rome", "communication", null, 1, 3, 3))
        assertTrue(computingCapacityAvailable(jobs, "military", "Rome", "military", "main", 1, 3, 3))
        assertFalse(invocationCapacityAvailable(emptyMap(), "gameplay", null, 1, 0, 2, true))
        assertTrue(invocationCapacityAvailable(emptyMap(), "gameplay", null, 1, 0, 3, true))
        assertTrue(invocationCapacityAvailable(jobs, "military", "main", 1, 1, 3, true))
    }

    @Test fun `suspended war roles leave room for sequential consultations`() {
        val jobs = mapOf(
            "main" to main.copy(computing = false, militaryPartnerRequired = true),
            "economy" to economy.copy(computing = false),
            "military" to CommunicationJob("Rome", "military", 100, parentJob = "main", computing = false),
        )
        assertTrue(computingCapacityAvailable(jobs, "consult", "Rome", "consultation", null, 1, 1, 1))
    }
}
