package dev.tvlivesubs

import kotlin.math.abs
import kotlin.math.log10
import kotlin.math.sqrt

data class AudioLevel(
    val rmsDb: Float = FLOOR_DB,
    val peakDb: Float = FLOOR_DB,
    /** How long the captured stream has been below [SILENCE_DB]. */
    val silentMs: Long = 0,
) {
    val isSilent: Boolean get() = peakDb < SILENCE_DB

    companion object {
        const val FLOOR_DB = -90f
        const val SILENCE_DB = -70f

        fun measure(pcm: ShortArray, count: Int, prevSilentMs: Long, chunkMs: Long): AudioLevel {
            if (count <= 0) return AudioLevel(silentMs = prevSilentMs + chunkMs)
            var sumSq = 0.0
            var peak = 0
            for (i in 0 until count) {
                val s = pcm[i].toInt()
                sumSq += s.toDouble() * s
                peak = maxOf(peak, abs(s))
            }
            val rms = sqrt(sumSq / count) / Short.MAX_VALUE
            val peakDb = toDb(peak / Short.MAX_VALUE.toDouble())
            return AudioLevel(
                rmsDb = toDb(rms),
                peakDb = peakDb,
                silentMs = if (peakDb < SILENCE_DB) prevSilentMs + chunkMs else 0,
            )
        }

        private fun toDb(linear: Double): Float =
            if (linear <= 0.0) FLOOR_DB else maxOf(FLOOR_DB, (20 * log10(linear)).toFloat())
    }
}
