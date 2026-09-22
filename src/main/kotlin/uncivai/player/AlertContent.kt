package uncivai.player

import com.unciv.logic.civilization.InformationalPopupContent
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
    val additionalAcknowledgements: List<String> = emptyList(),
    val kind: String? = null,
    val civilizationId: String? = null,
    val gameplayParagraphs: List<String>? = null,
)

internal fun observeAlertContent(operations: PlayerOperations, alert: PopupAlert): AlertContent? = operations.informationalPopupContent(alert)?.toPlayerContent()?.copy(kind = alert.type.name)

internal fun InformationalPopupContent.toPlayerContent(): AlertContent = let { content ->
    AlertContent(
        title = content.title.tr(),
        paragraphs = Collections.unmodifiableList(content.paragraphs.map { it.tr() }),
        quote = content.quote?.tr(),
        acknowledgement = content.acknowledgement.tr(),
        additionalAcknowledgements = Collections.unmodifiableList(content.additionalAcknowledgements.map { it.tr() }),
    )
}
