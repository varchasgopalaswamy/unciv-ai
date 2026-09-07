package uncivai

/** Opens an exported inspection save without issuing any game commands. */
object InspectionUiLauncher {
    @JvmStatic
    fun main(args: Array<String>) {
        DesktopSaveUiLauncher.launch(args, DesktopSaveUiLauncher.Mode.INSPECTION)
    }
}
