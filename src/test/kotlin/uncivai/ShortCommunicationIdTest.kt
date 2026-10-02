package uncivai

import org.junit.Assert.assertEquals
import org.junit.Test
import java.security.SecureRandom

class ShortCommunicationIdTest {
    @Test
    fun `short references retry collisions instead of reusing an existing record`() {
        val numbers = ArrayDeque(listOf(0, 0, 1, 60_466_175))
        val random = object : SecureRandom() {
            override fun nextInt(bound: Int): Int {
                assertEquals(60_466_176, bound)
                return numbers.removeFirst()
            }
        }
        val used = mutableSetOf<String>()
        repeat(3) { used.add(shortCommunicationId("p", random, used::contains)) }
        assertEquals(setOf("p00000", "p00001", "pzzzzz"), used)
    }
}
