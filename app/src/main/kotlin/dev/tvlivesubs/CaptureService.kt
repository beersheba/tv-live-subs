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
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

class CaptureService : Service() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private var projection: MediaProjection? = null
    private var captureJob: Job? = null
    private var overlay: LevelOverlay? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            stopSelf()
            return START_NOT_STICKY
        }
        if (running) return START_NOT_STICKY

        // Must be in the foreground before getMediaProjection().
        startForeground(NOTIFICATION_ID, buildNotification())

        @Suppress("DEPRECATION")
        val data = intent?.getParcelableExtra<Intent>(EXTRA_DATA)
        val resultCode = intent?.getIntExtra(EXTRA_RESULT_CODE, 0) ?: 0
        if (data == null) {
            stopSelf()
            return START_NOT_STICKY
        }

        val mpm = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        projection = mpm.getMediaProjection(resultCode, data)?.also {
            it.registerCallback(object : MediaProjection.Callback() {
                override fun onStop() = stopSelf()
            }, null)
        }
        if (projection == null) {
            stopSelf()
            return START_NOT_STICKY
        }

        running = true
        overlay = LevelOverlay(this).also { it.show() }
        captureJob = scope.launch { capture(projection!!) }
        return START_NOT_STICKY
    }

    private suspend fun capture(projection: MediaProjection) = withContext(Dispatchers.IO) {
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
                level = AudioLevel.measure(pcm, n, level.silentMs, CHUNK_MS.toLong())
                withContext(Dispatchers.Main.immediate) { overlay?.update(level) }

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
        running = false
        scope.cancel()
        overlay?.hide()
        projection?.stop()
        super.onDestroy()
    }

    private fun buildNotification(): Notification {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "Capture", NotificationManager.IMPORTANCE_LOW),
        )
        return Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("TV Live Subs")
            .setContentText("Capturing audio")
            .setSmallIcon(R.drawable.ic_launcher)
            .build()
    }

    companion object {
        const val ACTION_STOP = "dev.tvlivesubs.STOP"
        const val EXTRA_RESULT_CODE = "resultCode"
        const val EXTRA_DATA = "data"

        private const val TAG = "LevelMeter"
        private const val CHANNEL_ID = "capture"
        private const val NOTIFICATION_ID = 1
        private const val SAMPLE_RATE = 16_000
        private const val CHUNK_MS = 100

        @Volatile
        var running = false
            private set
    }
}
