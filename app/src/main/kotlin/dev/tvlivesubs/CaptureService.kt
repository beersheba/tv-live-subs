package dev.tvlivesubs

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioPlaybackCaptureConfiguration
import android.media.AudioRecord
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.IBinder
import android.util.Log
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

class CaptureService : Service() {

    enum class State { OFF, ACTIVE, PAUSED }

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private var projection: MediaProjection? = null
    private var activeJob: Job? = null
    private var overlay: SubtitleOverlay? = null
    private var showMeter = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> stopSelf()
            ACTION_TOGGLE -> if (state == State.ACTIVE) pause() else if (state == State.PAUSED) resume()
            ACTION_SUMMON -> if (state == State.ACTIVE) overlay?.summon()
            ACTION_PAUSE -> if (state == State.ACTIVE) pause()
            ACTION_RESUME -> if (state == State.PAUSED) resume()
            else -> if (state == State.OFF) startProjection(intent) else resume()
        }
        return START_NOT_STICKY
    }

    private fun startProjection(intent: Intent?) {
        // Must be in the foreground before getMediaProjection().
        startForeground(NOTIFICATION_ID, buildNotification("Capturing audio"))

        @Suppress("DEPRECATION")
        val data = intent?.getParcelableExtra<Intent>(EXTRA_DATA)
        val resultCode = intent?.getIntExtra(EXTRA_RESULT_CODE, 0) ?: 0
        val mpm = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        projection = data?.let { mpm.getMediaProjection(resultCode, it) }?.also {
            it.registerCallback(object : MediaProjection.Callback() {
                override fun onStop() = stopSelf()
            }, null)
        }
        if (projection == null) {
            stopSelf()
            return
        }
        showMeter = intent?.getBooleanExtra(EXTRA_METER, false) ?: false
        resume()
    }

    /** Starts recording, the server link and the overlay. The projection stays alive across pauses. */
    private fun resume() {
        val projection = projection ?: return
        state = State.ACTIVE
        notify("Capturing audio")
        activeJob = scope.launch {
            val overlay = SubtitleOverlay(this@CaptureService, showMeter, onPause = ::pause).also { it.show() }
            this@CaptureService.overlay = overlay
            val client = TranslationClient(BuildConfig.SERVER_URL, BuildConfig.SRC_LANG, BuildConfig.DST_LANG, this)
                .also { it.start() }
            try {
                val presenter = SubtitlePresenter(this, client.commits)
                launch {
                    combine(presenter.visible, client.link) { lines, link -> lines to link }
                        .collect { (lines, link) -> overlay.setSubtitle(lines, link) }
                }
                capture(projection, client, overlay)
            } finally {
                client.stop()
                overlay.hide()
                this@CaptureService.overlay = null
            }
        }
    }

    /** Releases everything but the projection, so resume() needs no consent dialog. */
    private fun pause() {
        activeJob?.cancel()
        activeJob = null
        state = State.PAUSED
        notify("Paused")
    }

    private suspend fun capture(
        projection: MediaProjection,
        client: TranslationClient,
        overlay: SubtitleOverlay,
    ) = withContext(Dispatchers.IO) {
        val config = AudioPlaybackCaptureConfiguration.Builder(projection)
            .addMatchingUsage(AudioAttributes.USAGE_MEDIA)
            .addMatchingUsage(AudioAttributes.USAGE_UNKNOWN)
            .addMatchingUsage(AudioAttributes.USAGE_GAME)
            .build()
        val format = AudioFormat.Builder()
            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
            .setSampleRate(SAMPLE_RATE)
            .setChannelMask(AudioFormat.CHANNEL_IN_MONO)
            .build()
        val chunkSamples = SAMPLE_RATE * CHUNK_MS / 1000
        val minBuf = AudioRecord.getMinBufferSize(
            SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
        )

        val record = try {
            AudioRecord.Builder()
                .setAudioPlaybackCaptureConfig(config)
                .setAudioFormat(format)
                .setBufferSizeInBytes(maxOf(minBuf, chunkSamples * 2 * 4))
                .build()
        } catch (e: Exception) {
            Log.e(TAG, "AudioRecord build failed", e)
            return@withContext
        }
        if (record.state != AudioRecord.STATE_INITIALIZED) {
            Log.e(TAG, "AudioRecord not initialized (state=${record.state})")
            record.release()
            return@withContext
        }

        val pcm = ShortArray(chunkSamples)
        val bytes = ByteArray(chunkSamples * 2)
        val shortView = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN).asShortBuffer()
        var level = AudioLevel()
        var lastLog = 0L
        record.startRecording()
        Log.i(TAG, "capture started, recordingState=${record.recordingState}")
        try {
            while (isActive) {
                val n = record.read(pcm, 0, pcm.size)
                if (n < 0) {
                    Log.e(TAG, "read error $n")
                    break
                }
                shortView.clear()
                shortView.put(pcm, 0, n)
                client.sendAudio(bytes, n * 2)
                level = AudioLevel.measure(pcm, n, level.silentMs, CHUNK_MS.toLong())
                withContext(Dispatchers.Main.immediate) { overlay.update(level) }

                val now = System.currentTimeMillis()
                if (now - lastLog >= 1000) {
                    lastLog = now
                    Log.i(TAG, "rms=%.1f peak=%.1f silentMs=%d".format(level.rmsDb, level.peakDb, level.silentMs))
                }
            }
        } finally {
            record.stop()
            record.release()
        }
    }

    override fun onDestroy() {
        state = State.OFF
        scope.cancel()
        projection?.stop()
        super.onDestroy()
    }

    private fun buildNotification(text: String): Notification {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "Capture", NotificationManager.IMPORTANCE_LOW),
        )
        return Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("TV Live Subs")
            .setContentText(text)
            .setSmallIcon(R.drawable.ic_launcher)
            .build()
    }

    private fun notify(text: String) =
        getSystemService(NotificationManager::class.java).notify(NOTIFICATION_ID, buildNotification(text))

    companion object {
        const val ACTION_STOP = "dev.tvlivesubs.STOP"
        const val ACTION_TOGGLE = "dev.tvlivesubs.TOGGLE"
        const val ACTION_PAUSE = "dev.tvlivesubs.PAUSE"
        const val ACTION_SUMMON = "dev.tvlivesubs.SUMMON"
        const val ACTION_RESUME = "dev.tvlivesubs.RESUME"
        const val EXTRA_RESULT_CODE = "resultCode"
        const val EXTRA_DATA = "data"
        const val EXTRA_METER = "meter"

        private const val TAG = "LevelMeter"
        private const val CHANNEL_ID = "capture"
        private const val NOTIFICATION_ID = 1
        private const val SAMPLE_RATE = 16_000
        private const val CHUNK_MS = 100

        @Volatile
        var state = State.OFF
            private set

        val running: Boolean get() = state != State.OFF
    }
}
