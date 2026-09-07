package uncivai

/** Opens an S04 scenario and optionally verifies that its great-person picker appears. */
object S04UiLauncher {
    @JvmStatic
    fun main(args: Array<String>) {
        DesktopSaveUiLauncher.launch(args, DesktopSaveUiLauncher.Mode.S04)
    }
}
