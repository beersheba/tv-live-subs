package dev.tvlivesubs

import android.Manifest
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.media.projection.MediaProjectionManager
import android.os.Bundle
import android.provider.Settings
import android.util.Log
import android.widget.TextView

/**
 * Launching the app from the remote toggles capture: start (asks for MediaProjection consent)
 * or stop when already running. Has no UI of its own beyond a hint when permissions are missing.
 */
class MainActivity : Activity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        toggle()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        toggle()
    }

    private fun toggle() {
        if (CaptureService.running) {
            startService(Intent(this, CaptureService::class.java).setAction(CaptureService.ACTION_STOP))
            finish()
            return
        }
        val missing = buildList {
            if (!Settings.canDrawOverlays(this@MainActivity)) {
                add("adb shell appops set $packageName SYSTEM_ALERT_WINDOW allow")
            }
            if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                add("adb shell pm grant $packageName android.permission.RECORD_AUDIO")
            }
        }
        if (missing.isNotEmpty()) {
            setContentView(TextView(this).apply {
                text = "Missing permissions. Run:\n\n" + missing.joinToString("\n")
                textSize = 24f
                setPadding(64, 64, 64, 64)
            })
            return
        }
        val mpm = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        @Suppress("DEPRECATION")
        startActivityForResult(mpm.createScreenCaptureIntent(), REQUEST_PROJECTION)
    }

    @Suppress("OVERRIDE_DEPRECATION")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == REQUEST_PROJECTION && resultCode == RESULT_OK && data != null) {
            startForegroundService(
                Intent(this, CaptureService::class.java)
                    .putExtra(CaptureService.EXTRA_RESULT_CODE, resultCode)
                    .putExtra(CaptureService.EXTRA_DATA, data),
            )
        } else {
            Log.w("LevelMeter", "projection consent denied (resultCode=$resultCode)")
        }
        finish()
    }

    private companion object {
        const val REQUEST_PROJECTION = 1
    }
}
