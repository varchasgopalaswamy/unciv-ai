package uncivai

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class CommunicationChangesTest {
    @Test
    fun `commits notify recipient streams and global negotiation transitions only`() {
        val changes = CommunicationChanges()
        val rome = changes.forPlayer("Rome")
        val greece = changes.forPlayer("Greece")
        val state = CommunicationState()
        changes.committed(state)
        val baseline = rome.value
        state.streams["Greece"] = mutableListOf(CommunicationEvent(1, "message", obj("text" to "Private"), true))
        changes.committed(state)
        assertEquals(baseline, rome.value)
        assertTrue(greece.value > baseline)
        val greekBaseline = greece.value
        changes.committed(state)
        assertEquals(greekBaseline, greece.value)
        state.trades.activeProposal = "d1"
        changes.committed(state)
        assertTrue(rome.value > baseline)
        val negotiating = rome.value
        state.trades.activeProposal = null
        changes.committed(state)
        assertTrue(rome.value > negotiating)
    }
}
