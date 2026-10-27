package dev.tvlivesubs

import android.os.SystemClock
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull

/**
 * Paces incoming cues for reading. A new cue replaces the oldest visible text as soon as the previous
 * one has had a reading-speed minimum; if cues pile up the minimum shrinks so the display keeps up
 * with the speaker instead of lagging. After the last cue the text clears itself.
 *
 * Must run on a single-threaded scope (the service's main dispatcher): the queue is unsynchronized.
 */
class SubtitlePresenter(scope: CoroutineScope, cues: Flow<String>) {

    private val _visible = MutableStateFlow<List<String>>(emptyList())

    /** Newest cues, oldest first; the overlay shows as many from the end as fit its line budget. */
    val visible: StateFlow<List<String>> = _visible

    private val queue = ArrayDeque<String>()
    private val wake = Channel<Unit>(Channel.CONFLATED)

    init {
        scope.launch {
            cues.collect {
                queue.addLast(it)
                wake.trySend(Unit)
            }
        }
        scope.launch {
            var shownAt = 0L
            var shownLen = 0
            while (isActive) {
                val now = SystemClock.elapsedRealtime()
                if (queue.isEmpty()) {
                    if (_visible.value.isEmpty()) {
                        wake.receive()
                    } else {
                        val wait = shownAt + clearAfterMs(shownLen) - now
                        if (wait <= 0) _visible.value = emptyList() else withTimeoutOrNull(wait) { wake.receive() }
                    }
                    continue
                }
                val wait = shownAt + holdMs(shownLen, queue.size) - now
                if (wait > 0) {
                    withTimeoutOrNull(wait) { wake.receive() } // re-evaluate: a new arrival shortens the hold
                    continue
                }
                val cue = queue.removeFirst()
                _visible.value = (_visible.value + cue).takeLast(KEEP)
                shownAt = SystemClock.elapsedRealtime()
                shownLen = cue.length
            }
        }
    }

    /** Minimum time the previous cue stays before it may be pushed out. */
    private fun holdMs(len: Int, backlog: Int): Long {
        val base = (600L + 45L * len).coerceAtMost(2_500L)
        return when {
            backlog >= 4 -> base * 35 / 100
            backlog >= 2 -> base * 60 / 100
            else -> base
        }.coerceAtLeast(350L)
    }

    private fun clearAfterMs(len: Int) = (1_200L + 55L * len).coerceAtMost(5_000L)

    private companion object {
        const val KEEP = 3
    }
}
