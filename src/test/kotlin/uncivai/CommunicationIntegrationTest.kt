package uncivai

import org.junit.Assert.assertEquals
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.TimeUnit

class CommunicationIntegrationTest {
    @Test
    fun `peacetime military planning reserves partner capacity and charges only claimed jobs`() {
        runProbe("uncivai.MilitaryCapacityProbe")
    }

    @Test
    fun `private groups validate audiences preserve privacy and survive queued restart and rewind`() {
        runProbe("uncivai.GroupChatProbe")
    }

    @Test
    fun `human review chat queues privately survives restart and delivers once after review`() {
        runProbe("uncivai.HumanChatReviewProbe")
    }

    @Test
    fun `all trade items share canonical terms and native effects across human and agent controllers`() {
        runProbe("uncivai.UnifiedTradeProbe")
    }

    @Test
    fun `human trade has no response deadline and preserves exact consent across restart`() {
        runProbe("uncivai.HumanNegotiationProbe")
    }

    @Test
    fun `shared review admits all reviewers and fences gameplay and communication`() {
        runProbe("uncivai.ReviewPhaseProbe")
    }

    @Test
    fun `quiet communication polls survive concurrent HTTPS calls`() {
        runProbe("uncivai.CommunicationHttpProbe")
    }

    @Test
    fun `mixed diplomacy routes native AI requests without pausing and survives restart`() {
        runProbe("uncivai.MixedDiplomacyProbe")
    }

    @Test
    fun `live agreement scenario expires trades and interrupts resources through ordinary commands`() {
        runProbe("uncivai.AgreementLifecycleScenarioProbe")
    }

    @Test
    fun `enacted agreements track native durations endings privacy and recovery`() {
        runProbe("uncivai.AgreementLifecycleProbe")
    }

    @Test
    fun `third party wars preserve native effects private consent and recovery`() {
        runProbe("uncivai.ThirdPartyTradeProbe")
    }

    @Test
    fun `complete trade scenario settles every supported item and terminal path across restart`() {
        runProbe("uncivai.TradeCycleProbe")
    }

    @Test
    fun `communication waits release the executor survive races cancel and fail closed`() {
        runProbe("uncivai.CommunicationWaitProbe")
    }

    @Test
    fun `negotiation freezes gameplay bounds counters and expires durably across restart`() {
        runProbe("uncivai.NegotiationProbe")
    }

    @Test
    fun `bilateral agreements and city transfers require exact consent and survive off turn recovery`() {
        runProbe("uncivai.BilateralTradeProbe")
    }

    @Test
    fun `peace consent shares trade transactions invalidates attacks and rolls back atomically`() {
        runProbe("uncivai.PeaceTradeProbe")
    }

    @Test
    fun `economic consent applies exact terms out of turn and survives recovery`() {
        runProbe("uncivai.EngineTradeProbe")
    }

    @Test
    fun `chat privacy consent and complete round recovery`() {
        runProbe("uncivai.CommunicationProbe")
    }

    @Test
    fun `social pact and authoritative declaration survive restart`() {
        runProbe("uncivai.VerbalBetrayalProbe")
    }

    private fun runProbe(probe: String) {
        val root = Files.createTempDirectory("communication-")
        val log = root.resolve("probe.log")
        val process = ProcessBuilder(
            Path.of(System.getProperty("java.home"), "bin", "java").toString(),
            "-Xmx1g",
            "-Djava.awt.headless=true",
            "-Duncivai.assetsDir=${System.getProperty("uncivai.assetsDir")}",
            "-cp",
            System.getProperty("uncivai.testClasspath"),
            probe,
            root.toString(),
        ).redirectErrorStream(true).redirectOutput(log.toFile()).start()
        try {
            check(process.waitFor(120, TimeUnit.SECONDS)) { "Communication probe timed out: ${Files.readString(log)}" }
            assertEquals(Files.readString(log), 0, process.exitValue())
        } finally {
            if (process.isAlive) process.destroyForcibly().waitFor(5, TimeUnit.SECONDS)
            deleteTreeWithoutFollowingLinks(root)
        }
    }
}
