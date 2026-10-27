package dev.tvlivesubs

import android.accessibilityservice.AccessibilityService
import android.content.Intent
import android.os.SystemClock
import android.util.Log
import android.view.KeyEvent
import android.view.accessibility.AccessibilityEvent

/**
 * Receives remote-control keys system-wide (a plain service can't). A double press of DPAD-DOWN
 * drives the app while it runs: ACTIVE -> focus the pause button on the overlay; PAUSED -> resume.
 * The first press always reaches the TV app, so this is inert (and keys are untouched) when OFF.
 */
class KeyService : AccessibilityService() {

    private var lastDownAt = 0L
    private var swallowing = false

    override fun onKeyEvent(event: KeyEvent): Boolean {
        if (BuildConfig.LOG_KEYS) {
            Log.i(TAG, "${KeyEvent.keyCodeToString(event.keyCode)} (${event.keyCode}) action=${event.action} " +
                "repeat=${event.repeatCount}")
        }
        if (event.keyCode != KeyEvent.KEYCODE_DPAD_DOWN) return false

        // The second press of a gesture is swallowed whole (its repeats and its UP too), otherwise the
        // app would see a held key that never ends.
        if (swallowing) {
            if (event.action == KeyEvent.ACTION_UP) swallowing = false
            return true
        }
        // Subtitles off or the overlay holds the remote: behave like a normal key, never launch anything.
        if (CaptureService.state == CaptureService.State.OFF || SubtitleOverlay.interactive) return false
        if (event.action != KeyEvent.ACTION_DOWN || event.repeatCount > 0) return false

        val now = SystemClock.elapsedRealtime()
        if (now - lastDownAt > DOUBLE_PRESS_MS) {
            lastDownAt = now
            return false
        }
        lastDownAt = 0
        swallowing = true
        act()
        return true
    }

    private fun act() = when (CaptureService.state) {
        CaptureService.State.PAUSED -> command(CaptureService.ACTION_RESUME)
        CaptureService.State.ACTIVE -> command(CaptureService.ACTION_SUMMON)
        CaptureService.State.OFF -> Unit
    }

    private fun command(action: String) = startService(Intent(this, CaptureService::class.java).setAction(action))

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (event?.eventType != AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED) return
        val pkg = event.packageName?.toString() ?: return
        // Our own windows (overlay, launch trampoline), system UI and keyboards aren't "the app in front".
        if (pkg == packageName || pkg == "com.android.systemui" || "inputmethod" in pkg) return
        ForegroundApp.pkg.value = pkg
    }

    override fun onInterrupt() = Unit

    private companion object {
        const val TAG = "Keys"
        const val DOUBLE_PRESS_MS = 450L
    }
}
