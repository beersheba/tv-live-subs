package dev.tvlivesubs

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.PixelFormat
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.StateListDrawable
import android.os.Handler
import android.os.Looper
import android.util.TypedValue
import android.text.Layout
import android.text.StaticLayout
import android.text.TextPaint
import android.text.TextUtils
import android.view.Gravity
import android.view.KeyEvent
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.widget.Button
import android.widget.FrameLayout

/**
 * Opaque band placed over the app's own captions showing the newest translated lines.
 * [showMeter] adds a diagnostic dBFS strip on top.
 *
 * The window is normally non-focusable so the TV app keeps the remote. [summon] makes it focusable
 * for a few seconds with a pause button focused; OK presses it, any other key gives focus back.
 */
class SubtitleOverlay(
    private val context: Context,
    private val showMeter: Boolean,
    private var placement: Placement,
    private val onPause: () -> Unit,
) {
    private val wm = context.getSystemService(Context.WINDOW_SERVICE) as WindowManager
    private val view = SubtitleView(context, showMeter)
    private val root = OverlayRoot(context)
    private val button = Button(context)
    private val handler = Handler(Looper.getMainLooper())
    private val release = Runnable { setInteractive(false) }
    private var params: WindowManager.LayoutParams? = null
    private var attached = false

    fun show() {
        if (attached) return
        val d = context.resources.displayMetrics.density

        root.addView(view, ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT)
        button.apply {
            text = "Пауза   (OK)"
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 22f)
            setTextColor(Color.WHITE)
            isFocusable = true
            background = StateListDrawable().apply {
                addState(intArrayOf(android.R.attr.state_focused), pill(0xFF1E88E5.toInt(), Color.WHITE, d))
                addState(intArrayOf(), pill(0xFF424242.toInt(), 0xFF757575.toInt(), d))
            }
            visibility = View.GONE
            setOnClickListener {
                setInteractive(false)
                onPause()
            }
        }
        root.addView(
            button,
            FrameLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply {
                gravity = Gravity.END or Gravity.TOP
                setMargins(0, (10 * d).toInt(), (24 * d).toInt(), 0)
            },
        )

        params = WindowManager.LayoutParams(
            WindowManager.LayoutParams.MATCH_PARENT,
            heightPx(),
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
            flags(interactive = false),
            PixelFormat.OPAQUE,
        ).apply {
            gravity = Gravity.BOTTOM
            y = bottomPx()
        }
        wm.addView(root, params)
        attached = true
    }

    private fun screenH() = context.resources.displayMetrics.heightPixels

    private fun bottomPx() = (placement.bottomFrac * screenH()).toInt()

    private fun heightPx() = (placement.heightFrac * screenH()).toInt() +
        if (showMeter) (METER_DP * context.resources.displayMetrics.density).toInt() else 0

    /** Moves the band, e.g. when another app comes to the front. */
    fun setPlacement(p: Placement) {
        if (p == placement) return
        placement = p
        val lp = params ?: return
        if (!attached) return
        lp.height = heightPx()
        lp.y = bottomPx()
        wm.updateViewLayout(root, lp)
    }

    fun update(level: AudioLevel) = view.setLevel(level)

    fun setSubtitle(lines: List<String>, link: Link) = view.setSubtitle(lines, link)

    /** Gives the pause button the remote focus for a few seconds. */
    fun summon() = setInteractive(true)

    fun hide() {
        if (!attached) return
        handler.removeCallbacks(release)
        interactive = false
        wm.removeView(root)
        attached = false
    }

    private fun flags(interactive: Boolean) =
        WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or
            WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN or
            if (interactive) 0 else WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE

    private fun setInteractive(on: Boolean) {
        val p = params ?: return
        if (!attached || interactive == on) return
        interactive = on
        p.flags = flags(on)
        button.visibility = if (on) View.VISIBLE else View.GONE
        wm.updateViewLayout(root, p)
        handler.removeCallbacks(release)
        if (on) {
            button.requestFocus()
            handler.postDelayed(release, INTERACTIVE_MS)
        }
    }

    private fun pill(fill: Int, stroke: Int, d: Float) = GradientDrawable().apply {
        setColor(fill)
        setStroke((3 * d).toInt(), stroke)
        cornerRadius = 12 * d
        setPadding((20 * d).toInt(), (8 * d).toInt(), (20 * d).toInt(), (8 * d).toInt())
    }

    private inner class OverlayRoot(context: Context) : FrameLayout(context) {
        override fun dispatchKeyEvent(e: KeyEvent): Boolean {
            if (!interactive) return super.dispatchKeyEvent(e)
            val ok = e.keyCode == KeyEvent.KEYCODE_DPAD_CENTER || e.keyCode == KeyEvent.KEYCODE_ENTER ||
                e.keyCode == KeyEvent.KEYCODE_NUMPAD_ENTER
            if (ok) {
                handler.removeCallbacks(release)
                handler.postDelayed(release, INTERACTIVE_MS)
                return super.dispatchKeyEvent(e)
            }
            if (e.action == KeyEvent.ACTION_DOWN) setInteractive(false) // any other key: hand the remote back
            return true
        }
    }

    private class SubtitleView(context: Context, private val showMeter: Boolean) : View(context) {
        private val d = context.resources.displayMetrics.density
        private var level = AudioLevel()
        private var lines: List<String> = emptyList()
        private var link = Link.DOWN

        private val bg = Paint().apply { color = Color.BLACK }
        private val track = Paint().apply { color = 0xFF333333.toInt() }
        private val bar = Paint()
        private val mono = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = Color.WHITE
            textSize = 22 * d
            typeface = Typeface.MONOSPACE
        }
        private val warn = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = 0xFFFFB300.toInt()
            textSize = 14 * d
        }
        private val subPaint = TextPaint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.WHITE }

        fun setLevel(l: AudioLevel) {
            level = l
            if (showMeter) postInvalidate()
        }

        fun setSubtitle(l: List<String>, lk: Link) {
            lines = l
            link = lk
            postInvalidate()
        }

        override fun onDraw(c: Canvas) {
            c.drawRect(0f, 0f, width.toFloat(), height.toFloat(), bg)
            val pad = 24 * d
            val textTop = if (showMeter) METER_DP * d else 0f
            if (showMeter) drawMeter(c, pad)
            if (link != Link.READY) c.drawText("● нет связи с сервером", pad, textTop + 18 * d, warn)

            val layout = fit((width - 2 * pad).toInt()) ?: return
            val areaH = height - textTop
            c.save()
            c.translate(pad, textTop + (areaH - layout.height) / 2f)
            layout.draw(c)
            c.restore()
        }

        /** As many of the newest lines as fit in [MAX_LINES] at [TEXT_SP]; shrinks only for one long line. */
        private fun fit(width: Int): StaticLayout? {
            if (lines.isEmpty()) return null
            var k = 1
            while (k < lines.size && layoutFor(join(k + 1), TEXT_SP, width).lineCount <= MAX_LINES) k++
            var size = TEXT_SP
            var layout = layoutFor(join(k), size, width)
            while (layout.lineCount > MAX_LINES && size > MIN_SP) {
                size -= 2f
                layout = layoutFor(join(k), size, width)
            }
            // Still too long at the minimum size: keep the end of the text.
            return if (layout.lineCount > MAX_LINES) layoutFor(join(k), size, width, clip = true) else layout
        }

        private fun join(k: Int) = lines.takeLast(k).joinToString(" ")

        private fun layoutFor(text: String, sp: Float, width: Int, clip: Boolean = false): StaticLayout {
            subPaint.textSize = sp * d
            val b = StaticLayout.Builder.obtain(text, 0, text.length, subPaint, width)
                .setAlignment(Layout.Alignment.ALIGN_CENTER)
            if (clip) b.setMaxLines(MAX_LINES).setEllipsize(TextUtils.TruncateAt.START)
            return b.build()
        }

        private fun drawMeter(c: Canvas, pad: Float) {
            val barTop = 40 * d
            val barH = 24 * d
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
            c.drawRect(pad + barW * peakFrac - 3 * d, barTop, pad + barW * peakFrac, barTop + barH, mono)
            val status = if (level.silentMs >= 2000) "NO SIGNAL %.0fs".format(level.silentMs / 1000f) else "SIGNAL"
            c.drawText("%s  rms %.0f  peak %.0f dBFS".format(status, level.rmsDb, level.peakDb), pad, 30 * d, mono)
        }

        private companion object {
            const val RANGE_MIN = -60f
        }
    }

    companion object {
        /** True while the overlay holds the remote focus; KeyService must not react then. */
        @Volatile
        var interactive = false
            private set

        private const val INTERACTIVE_MS = 4_000L
        private const val METER_DP = 72f
        private const val TEXT_SP = 30f
        private const val MIN_SP = 22f
        private const val MAX_LINES = 2
    }
}
