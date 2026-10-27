package dev.tvlivesubs

import kotlinx.coroutines.flow.MutableStateFlow

/**
 * Where the band sits, as fractions of the screen height: [bottomFrac] is the gap above the bottom edge,
 * [heightFrac] the band's height. Apps draw their own captions at different places, so the band is
 * placed per app to cover those captions without hiding the broadcast's own graphics.
 */
data class Placement(val bottomFrac: Float, val heightFrac: Float)

object Placements {
    val DEFAULT = Placement(bottomFrac = 0f, heightFrac = 0.14f)

    // Measured on photos of Kan 11 in Next TV: captions sit at ~62-79% of the height from the top
    // (21-38% from the bottom); the channel's lower-third graphic starts at ~19% from the bottom.
    private val byPackage = mapOf(
        "il.net.hot.nexttv" to Placement(bottomFrac = 0.205f, heightFrac = 0.185f),
    )

    fun forPackage(pkg: String?): Placement = byPackage[pkg] ?: DEFAULT
}

/** The app in front, as last reported by [KeyService]'s window events (our own windows excluded). */
object ForegroundApp {
    val pkg = MutableStateFlow<String?>(null)
}
