package uncivai

import com.badlogic.gdx.Gdx
import com.unciv.models.metadata.BaseRuleset
import com.unciv.models.ruleset.RulesetCache
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class EngineDependencyTest {
    @Test
    fun `bundled rulesets load through core without a graphics backend`() {
        assertNull(Gdx.app)
        assertNull(Gdx.gl)

        val errors = RulesetCache.loadRulesets(consoleMode = true, noMods = true)

        assertTrue(errors.joinToString("\n"), errors.isEmpty())
        assertEquals(BaseRuleset.entries.map { it.fullName }.toSet(), RulesetCache.keys)
        for (baseRuleset in BaseRuleset.entries) {
            val ruleset = RulesetCache.getComplexRuleset(linkedSetOf(), baseRuleset.fullName)
            assertTrue("${baseRuleset.fullName}: Settler", ruleset.units.containsKey("Settler"))
            assertTrue("${baseRuleset.fullName}: Agriculture", ruleset.technologies.containsKey("Agriculture"))
            assertTrue("${baseRuleset.fullName}: Rome", ruleset.nations.containsKey("Rome"))
        }

        assertNull(Gdx.app)
        assertNull(Gdx.gl)
    }
}
