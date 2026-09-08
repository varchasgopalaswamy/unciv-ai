package uncivai.player

import com.unciv.logic.civilization.PlayerOperations
import com.unciv.logic.civilization.PopupAlert
import com.unciv.models.translations.tr
import kotlinx.serialization.Serializable
import java.util.Collections

/** Text shown by the corresponding desktop popup, in display order. */
@Serializable
data class AlertContent(
    val title: String,
    val paragraphs: List<String>,
    val quote: String? = null,
    val acknowledgement: String,
)

internal fun observeAlertContent(operations: PlayerOperations, alert: PopupAlert): AlertContent? = operations.informationalPopupContent(alert)?.let { content ->
    AlertContent(
        title = content.title.tr(),
        paragraphs = Collections.unmodifiableList(content.paragraphs.map { it.tr() }),
        quote = content.quote?.tr(),
        acknowledgement = content.acknowledgement.tr(),
    )
}
