package dev.tvlivesubs

import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume
import kotlinx.coroutines.suspendCancellableCoroutine

enum class Link { CONNECTING, READY, DOWN }

/**
 * WebSocket client for the STT/translation server (protocol: server/tvsubs_server/__main__.py).
 * Reconnects with backoff; audio sent while the link isn't ready is dropped, not queued.
 */
class TranslationClient(
    private val url: String,
    private val src: String,
    private val dst: String,
    private val scope: CoroutineScope,
) {
    private val http = OkHttpClient.Builder()
        .pingInterval(15, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .build()

    private val _link = MutableStateFlow(Link.DOWN)
    val link: StateFlow<Link> = _link

    private val _commits = MutableSharedFlow<String>(extraBufferCapacity = 64)

    /** Translated chunks in speaking order (immutable once sent by the server). */
    val commits: SharedFlow<String> = _commits

    @Volatile
    private var socket: WebSocket? = null
    private var job: Job? = null

    fun start() {
        job = scope.launch {
            var backoff = 1_000L
            while (isActive) {
                val reached = connectOnce()
                backoff = if (reached) 1_000L else minOf(backoff * 2, 10_000L)
                delay(backoff)
            }
        }
    }

    fun stop() {
        job?.cancel()
        socket?.cancel()
        socket = null
        _link.value = Link.DOWN
        http.dispatcher.executorService.shutdown()
    }

    /** Drops the frame when the link isn't ready or the send queue is backed up (~4 s of audio). */
    fun sendAudio(pcm: ByteArray, length: Int) {
        val ws = socket ?: return
        if (_link.value != Link.READY || ws.queueSize() > MAX_QUEUE_BYTES) return
        ws.send(pcm.toByteString(0, length))
    }

    /** Runs one connection until it closes; returns true if the server said "ready". */
    private suspend fun connectOnce(): Boolean = suspendCancellableCoroutine { cont ->
        var reached = false
        _link.value = Link.CONNECTING
        val ws = http.newWebSocket(Request.Builder().url(url).build(), object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                webSocket.send(JSONObject().put("type", "config").put("src", src).put("dst", dst).toString())
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                val m = JSONObject(text)
                when (val type = m.getString("type")) {
                    "ready" -> {
                        reached = true
                        _link.value = Link.READY
                    }
                    "commit" -> {
                        val translation = m.getString("translation")
                        Log.i(TAG, "#${m.getInt("id")}: ${m.getString("text")} -> $translation")
                        _commits.tryEmit(translation)
                    }
                }
            }

            override fun onMessage(webSocket: WebSocket, bytes: ByteString) = Unit

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) = finish()

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                Log.w(TAG, "link failed: ${t.message}")
                finish()
            }

            private fun finish() {
                _link.value = Link.DOWN
                socket = null
                if (cont.isActive) cont.resume(reached)
            }
        })
        socket = ws
        cont.invokeOnCancellation { ws.cancel() }
    }

    private companion object {
        const val TAG = "TvSubs"
        const val MAX_QUEUE_BYTES = 128_000L
    }
}
