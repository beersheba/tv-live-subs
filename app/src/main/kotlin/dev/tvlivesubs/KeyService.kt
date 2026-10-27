package dev.tvlivesubs

import android.accessibilityservice.AccessibilityService
import android.content.Intent
import android.os.SystemClock
import android.util.Log
import android.view.KeyEvent
import android.view.accessibility.AccessibilityEvent

/**
 * Receives remote-control keys system-wide (a plain service can't). A double press of DPAD-DOWN
 * drives the app: ACTIVE -> focus the pause button on the overlay; PAUSED -> resume; OFF -> start
 * (no consent dialog once PROJECT_MEDIA is allowed). The first press always reaches the TV app.
 */
class KeyService : AccessibilityService() {

    private var lastDownAt = 0L
    private var swallowUp = false

    override fun onKeyEvent(event: KeyEvent): Boolean {
        if (BuildConfig.LOG_KEYS) {
            Log.i(TAG, "${KeyEvent.keyCodeToString(event.keyCode)} (${event.keyCode}) action=${event.action} " +
                "repeat=${event.repeatCount}")
        }
        if (event.keyCode != KeyEvent.KEYCODE_DPAD_DOWN || SubtitleOverlay.interactive) return false
        if (event.action == KeyEvent.ACTION_UP) return swallowUp.also { swallowUp = false }
        if (event.repeatCount > 0) return false // key held down: not a double press

        val now = SystemClock.elapsedRealtime()
        if (now - lastDownAt > DOUBLE_PRESS_MS) {
            lastDownAt = now
            return false
        }
        lastDownAt = 0
        swallowUp = true
        act()
        return true
    }

    private fun act() = when (CaptureService.state) {
        CaptureService.State.OFF ->
            startActivity(Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        CaptureService.State.PAUSED -> command(CaptureService.ACTION_RESUME)
        CaptureService.State.ACTIVE -> command(CaptureService.ACTION_SUMMON)
    }

    private fun command(action: String) = startService(Intent(this, CaptureService::class.java).setAction(action))

    override fun onAccessibilityEvent(event: AccessibilityEvent?) = Unit

    override fun onInterrupt() = Unit

    private companion object {
        const val TAG = "Keys"
        const val DOUBLE_PRESS_MS = 450L
    }
}
