package dev.tvlivesubs

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.PixelFormat
import android.graphics.Paint
import android.graphics.Typeface
import android.view.Gravity
import android.view.View
import android.view.WindowManager

/** Opaque bottom band with a dBFS bar; stage-3 stand-in for the subtitle overlay. */
class LevelOverlay(private val context: Context) {

    private val wm = context.getSystemService(Context.WINDOW_SERVICE) as WindowManager
    private val view = LevelView(context)
    private var attached = false

    fun show() {
        if (attached) return
        val density = context.resources.displayMetrics.density
        val lp = WindowManager.LayoutParams(
            WindowManager.LayoutParams.MATCH_PARENT,
            (110 * density).toInt(),
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or
                WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
            PixelFormat.OPAQUE,
        ).apply { gravity = Gravity.BOTTOM }
        wm.addView(view, lp)
        attached = true
    }

    fun update(level: AudioLevel) = view.set(level)

    fun hide() {
        if (!attached) return
        wm.removeView(view)
        attached = false
    }

    private class LevelView(context: Context) : View(context) {
        private val d = context.resources.displayMetrics.density
        private var level = AudioLevel()

        private val bg = Paint().apply { color = Color.BLACK }
        private val track = Paint().apply { color = 0xFF333333.toInt() }
        private val bar = Paint()
        private val text = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = Color.WHITE
            textSize = 22 * d
            typeface = Typeface.MONOSPACE
        }

        fun set(l: AudioLevel) {
            level = l
            postInvalidate()
        }

        override fun onDraw(c: Canvas) {
            c.drawRect(0f, 0f, width.toFloat(), height.toFloat(), bg)
            val pad = 24 * d
            val barTop = 52 * d
            val barH = 32 * d
            val barW = width - 2 * pad
            c.drawRect(pad, barTop, pad + barW, barTop + barH, track)

            val frac = ((level.rmsDb - RANGE_MIN) / -RANGE_MIN).coerceIn(0f, 1f)
            bar.color = when {
                level.peakDb > -3f -> Color.RED
                level.peakDb > -12f -> Color.YELLOW
                else -> 0xFF4CAF50.toInt()
            }
            c.drawRect(pad, barTop, pad + barW * frac, barTop + barH, bar)
            val peakFrac = ((level.peakDb - RANGE_MIN) / -RANGE_MIN).coerceIn(0f, 1f)
            c.drawRect(pad + barW * peakFrac - 3 * d, barTop, pad + barW * peakFrac, barTop + barH, text)

            val status = if (level.silentMs >= 2000) "NO SIGNAL %.0fs".format(level.silentMs / 1000f) else "SIGNAL"
            c.drawText(
                "%s   rms %.0f dBFS   peak %.0f dBFS".format(status, level.rmsDb, level.peakDb),
                pad, 38 * d, text,
            )
        }

        private companion object {
            const val RANGE_MIN = -60f
        }
    }
}
