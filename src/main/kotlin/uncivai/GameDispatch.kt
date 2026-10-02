package uncivai

/** Runs game access on its owning thread. Headless games use the executor's monitor. */
internal interface GameDispatch {
    fun <T> call(block: () -> T): T
}

internal object DirectGameDispatch : GameDispatch {
    override fun <T> call(block: () -> T): T = block()
}
