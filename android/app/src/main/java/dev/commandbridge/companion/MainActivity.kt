package dev.commandbridge.companion

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.res.Configuration
import android.graphics.Color
import android.media.AudioDeviceCallback
import android.media.AudioDeviceInfo
import android.media.AudioManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.InputType
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.webkit.CookieManager
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject

/**
 * Spec 030/031/032/033. Paired: the WHOLE Command Bridge page in a WebView (`?native=1`), filling the
 * app below the status bar with the page's own title only, authenticated by the device key as a
 * cookie, while the native service owns every sound. The page's orb and output caret are forwarded
 * here over `CBNative`; the service tells the page what is playing, his level and the route through
 * `cbNative`. There is no native settings UI (spec 033 FR6): the only native screen is the one shown
 * when there is no pairing or the paired page cannot be loaded.
 */
class MainActivity : Activity() {
    private val prefs by lazy { getSharedPreferences("cb", MODE_PRIVATE) }
    private val main = Handler(Looper.getMainLooper())
    private val audio by lazy { getSystemService(Context.AUDIO_SERVICE) as AudioManager }
    private lateinit var web: WebView
    private lateinit var panel: ScrollView
    private lateinit var url: EditText
    private lateinit var status: TextView
    private var loadedBase: String? = null
    private val devices = object : AudioDeviceCallback() {
        override fun onAudioDevicesAdded(added: Array<out AudioDeviceInfo>) = pushRoutes()
        override fun onAudioDevicesRemoved(removed: Array<out AudioDeviceInfo>) = pushRoutes()
    }

    private val night get() =
        (resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK) == Configuration.UI_MODE_NIGHT_YES

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val root = FrameLayout(this)
        web = WebView(this).apply {
            // The page's own background, so nothing flashes the other colour while it loads (FR2).
            setBackgroundColor(if (night) Color.parseColor("#080b11") else Color.parseColor("#e7e9e7"))
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            webViewClient = object : WebViewClient() {
                override fun shouldOverrideUrlLoading(view: WebView, req: WebResourceRequest): Boolean =
                    route(req.url)
                override fun onPageFinished(view: WebView, u: String) { pushState(); pushRoutes() }
                override fun onReceivedError(view: WebView, req: WebResourceRequest, err: WebResourceError) {
                    // The paired page did not load (a new tunnel address, the server down): say so,
                    // with the way back, instead of a blank app.
                    if (req.isForMainFrame) pageFailed(err.description.toString())
                }
            }
        }
        root.addView(web, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        panel = ScrollView(this).apply {
            setBackgroundColor(if (night) Color.parseColor("#080b11") else Color.parseColor("#e7e9e7"))
            addView(buildPanel())
        }
        root.addView(panel, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        setContentView(root)

        VoiceService.listener = { kind, json -> deliver(kind, json) }
        askPermissions()
        showPageOrSetup()
        handlePairIntent(intent)
        openOnLane(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handlePairIntent(intent)
        openOnLane(intent)
    }

    /**
     * A tapped heads-up opens the app ON THAT LANE (spec 036 FR4): the server's live lane moves there,
     * so turning the orb on plays that agent's held clip.
     */
    private fun openOnLane(intent: Intent?) {
        val lane = intent?.getStringExtra(NotifyService.EXTRA_LANE) ?: return
        intent.removeExtra(NotifyService.EXTRA_LANE)
        val key = key() ?: return
        val tunnel = tunnel(); val lan = lan()
        Thread({
            val pick = Paths.pick(lan, tunnel, key) ?: return@Thread
            try {
                val body = JSONObject().put("action", "switch").put("name", lane).toString()
                    .toRequestBody("application/json".toMediaType())
                OkHttpClient().newCall(Request.Builder().url("${pick.first}/lane")
                    .header(Protocol.DEVICE_HEADER, key).post(body).build()).execute().close()
            } catch (_: Exception) {}
        }, "cb-open-lane").start()
    }

    /** The light orb-off connection (spec 036): on whenever the app is paired and has been opened. */
    private fun startNotices() {
        if (!paired() || NotifyService.running) return
        try { startForegroundService(Intent(this, NotifyService::class.java)) } catch (_: Exception) {}
    }

    override fun onResume() {
        super.onResume()
        VoiceService.appInFront = true
        audio.registerAudioDeviceCallback(devices, main)
        lastNetwork = null
        try { connectivity.registerDefaultNetworkCallback(networkCallback) } catch (_: Exception) {}
        startNotices()
        pushState(); pushRoutes()
    }

    override fun onPause() {
        super.onPause()
        VoiceService.appInFront = false
        try { audio.unregisterAudioDeviceCallback(devices) } catch (_: Exception) {}
        try { connectivity.unregisterNetworkCallback(networkCallback) } catch (_: Exception) {}
    }

    override fun onDestroy() {
        // Leaving the app does NOT stop voice: the service keeps running (spec 032 FR4).
        if (VoiceService.listener != null) VoiceService.listener = null
        super.onDestroy()
    }

    @Deprecated("Back goes back in the page first")
    override fun onBackPressed() {
        if (web.canGoBack()) web.goBack() else @Suppress("DEPRECATION") super.onBackPressed()
    }

    // --- the page ---------------------------------------------------------------------------------
    private fun appVersion(): String = try {
        packageManager.getPackageInfo(packageName, 0).versionName ?: ""
    } catch (_: Exception) { "" }

    // THE TWO PATHS (spec 034): `base` is the https tunnel, `lan` the home Wi-Fi; either may be absent.
    private fun tunnel() = prefs.getString("base", null)
    private fun lan() = prefs.getString("lan", null)?.let { Protocol.lanOrigin(it) }
    private fun key() = prefs.getString("device_key", null)
    private fun paired() = key() != null && (tunnel() != null || lan() != null)

    /** Probe the LAN, pick a path off the main thread, then load the page on it (spec 034 FR5). */
    private fun showPageOrSetup(force: Boolean = false, avoidLan: Boolean = false) {
        if (!paired()) { panel.visibility = View.VISIBLE; return }
        val key = key()!!; val tunnel = tunnel(); val lan = lan()
        Thread({
            val pick = if (avoidLan) Protocol.choosePath(null, tunnel, false) else Paths.pick(lan, tunnel, key)
            main.post { if (pick != null) load(pick.first, pick.second, key, force) else unreachable("No way to reach the PC") }
        }, "cb-page-path").start()
    }

    private fun load(base: String, path: String, key: String, force: Boolean) {
        val cm = CookieManager.getInstance()
        cm.setAcceptCookie(true)
        // The key as a cookie: every request the page makes (the UI frame, the canvas, the socket)
        // carries it, which a header cannot (spec 032 FR3). Not `Secure` on the LAN's http origin,
        // where a Secure cookie would never be sent (spec 034).
        val secure = if (base.startsWith("https://")) "Secure; " else ""
        cm.setCookie(base, "cb_device=$key; ${secure}HttpOnly; SameSite=Lax; Path=/")
        cm.flush()
        if (force || loadedBase != base) {
            listen(setOfNotNull(tunnel(), lan()))
            loadedBase = base
            loadedPath = path
            // `app=` is the version the page shows beside its title (spec 033 FR8), `path=` which way
            // it came (spec 034 FR7).
            // `prims=` (spec 044): this app is driven by the server's directives, so the page sends the
            // orb and the caret to the server. A server that predates it ignores the parameter.
            web.loadUrl("$base/?native=1&app=${Uri.encode(appVersion())}&path=$path&prims=${Primitives.PROTOCOL}")
        }
        panel.visibility = View.GONE
        keepAwake()
    }

    private var loadedPath: String? = null

    /** A home page that fails to load falls back to the tunnel; otherwise say so (spec 034 FR5). */
    private fun pageFailed(why: String) {
        if (loadedPath == "home" && tunnel() != null) { showPageOrSetup(force = true, avoidLan = true); return }
        unreachable("Can't reach ${loadedBase ?: "the server"}: $why")
    }

    // Wi-Fi ⇄ cellular while the app is open: pick again, and move the page if the path changed.
    private var lastNetwork: android.net.Network? = null
    private val connectivity by lazy { getSystemService(Context.CONNECTIVITY_SERVICE) as android.net.ConnectivityManager }
    private val networkCallback = object : android.net.ConnectivityManager.NetworkCallback() {
        override fun onAvailable(network: android.net.Network) {
            main.post {
                val had = lastNetwork
                lastNetwork = network
                if (had != null && had != network && lan() != null) showPageOrSetup()
            }
        }
    }

    private fun unreachable(why: String) {
        status.text = why + "\n\nIf the address changed, pair again from the browser page."
        panel.visibility = View.VISIBLE
        keepAwake()
    }

    /** Same origin stays in the page; pairing links pair; everything else opens in the browser. */
    private fun route(u: Uri): Boolean {
        val s = u.toString()
        if (u.scheme == "commandbridge" || s.startsWith("intent://pair")) {
            val link = if (u.scheme == "intent") s.replaceFirst("intent://", "commandbridge://").substringBefore("#Intent") else s
            handlePairLink(link); return true
        }
        // Either of his paths is the bridge itself (spec 034); anything else is the browser's.
        val ours = setOfNotNull(tunnel(), lan()).map { Uri.parse(it) }
        if (ours.any { it.scheme == u.scheme && it.host == u.host && it.port == u.port }) return false
        try { startActivity(Intent(Intent.ACTION_VIEW, u)) } catch (_: Exception) {}
        return true
    }

    private fun deliver(kind: String, json: String) {
        if (!::web.isInitialized) return
        val js = when (kind) {
            "state" -> { keepAwake(); "window.cbNative && window.cbNative.state($json)" }
            "clip" -> "(function(j){window.cbNative && window.cbNative.clip(j.kind, j.header)})($json)"
            "level" -> "(function(j){window.cbNative && window.cbNative.level(j.level, j.speaking)})($json)"
            "routes" -> "window.cbNative && window.cbNative.routes(${routesJson()})"
            else -> return
        }
        web.evaluateJavascript(js, null)
    }

    private fun pushState() = deliver("state", VoiceService.stateJson())
    private fun pushRoutes() = deliver("routes", "{}")

    /**
     * The screen stays on while the conversation is on, and sleeps normally once he turns the orb
     * off (spec 033 FR7, JJ: "keep the screen awake. Unless I turn the orb off").
     */
    private fun keepAwake() {
        // Only with the page on screen: it has the orb that turns this off. The fallback screen has
        // no orb, so voice started there never pins the screen on (spec 033 review).
        // Driven (spec 044), the server's `awake` directive decides; else the 0.7.x rule.
        val wanted = if (VoiceService.driven) VoiceService.awakeWanted else VoiceService.channelOpenNow
        val on = VoiceService.running && wanted && panel.visibility != View.VISIBLE
        if (on) window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        else window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
    }

    /** The pickable output types the phone has now. */
    private fun availableTypes(): List<Int> {
        val types = if (Build.VERSION.SDK_INT >= 31) audio.availableCommunicationDevices.map { it.type }
            // Android < 12 can only switch the loudspeaker on or off (spec 033 scope); a plugged-in
            // headset is listed so the caret can show it while it is the route.
            else listOf(AudioRoute.TYPE_BUILTIN_SPEAKER, AudioRoute.TYPE_BUILTIN_EARPIECE) +
                audio.getDevices(AudioManager.GET_DEVICES_OUTPUTS).map { it.type }.filter {
                    it == AudioRoute.TYPE_WIRED_HEADSET || it == AudioRoute.TYPE_WIRED_HEADPHONES ||
                        it == AudioRoute.TYPE_USB_HEADSET
                }
        return AudioRoute.choices(types).map { it.first }
    }

    /** The outputs the phone has now, and the one in use (or that would be), for the mic caret. */
    private fun routesJson(): String {
        val types = availableTypes()
        val remembered = prefs.getInt(VoiceService.PREF_ROUTE, -1).takeIf { it >= 0 }
        val current = VoiceService.currentRoute ?: AudioRoute.choose(types, remembered)
        val list = JSONArray()
        for ((type, label) in AudioRoute.choices(types)) list.put(JSONObject().put("type", type).put("label", label))
        return JSONObject().put("available", list).put("current", current ?: JSONObject.NULL).toString()
    }

    /**
     * The page's orb and output caret (spec 032 FR2, spec 033 FR3), as `CBNative.postMessage(json)`.
     * Not a JavaScript interface: that is injected into EVERY frame, and the canvas frames render
     * agent-authored HTML on the same origin, so one could start his mic or move his audio to the
     * loudspeaker (spec 033 review). A web message listener says which frame posted; only the top
     * frame (the shell) is obeyed, and only from his paired origins (the tunnel and the home Wi-Fi).
     */
    private fun listen(origins: Set<String>) {
        if (origins.isEmpty() || !WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) return
        try { WebViewCompat.removeWebMessageListener(web, "CBNative") } catch (_: Exception) {}
        WebViewCompat.addWebMessageListener(web, "CBNative", origins) { _, message, _, isMainFrame, _ ->
            if (!isMainFrame) return@addWebMessageListener
            val j = try { JSONObject(message.data ?: return@addWebMessageListener) } catch (_: Exception) { return@addWebMessageListener }
            when (j.optString("op")) {
                "orbTapped" -> orbTapped()
                "setRoute" -> setRoute(j.optInt("type", -1))
            }
        }
    }

    private fun orbTapped() {
        if (!VoiceService.running) start()
        else startService(Intent(this, VoiceService::class.java).setAction(VoiceService.ACTION_TOGGLE_CHANNEL))
    }

    private fun setRoute(type: Int) {
        // Only an output the phone has now: the caret lists exactly these.
        if (!availableTypes().contains(type)) return
        prefs.edit().putInt(VoiceService.PREF_ROUTE, type).apply()
        if (VoiceService.running) {
            startService(Intent(this, VoiceService::class.java).setAction(VoiceService.ACTION_APPLY_ROUTE))
        } else pushRoutes()
    }

    // --- the screen shown with no pairing, or when the page can't be reached ----------------------
    private fun buildPanel(): View {
        val pad = (16 * resources.displayMetrics.density).toInt()
        val fg = if (night) Color.WHITE else Color.BLACK
        val col = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(pad, pad * 3, pad, pad) }
        fun label(t: String, size: Float = 15f) = TextView(this).apply { text = t; textSize = size; setTextColor(fg) }
        col.addView(label("Command Bridge ${appVersion()}", 22f))
        status = label("", 16f)
        col.addView(status)
        col.addView(Button(this).apply { text = "Try again"; setOnClickListener { showPageOrSetup(force = true) } })
        col.addView(label("\nTo pair: open the pairing link (…/pair?token=…, ask for it) in this phone's " +
            "browser, then tap \"Open in Command Bridge app\". A new pairing replaces the old one.\n\n" +
            "Use voice in the app OR the browser, not both: a browser page with its orb on is a second " +
            "microphone.\n\nFallback, voice only: paste the page URL (with ?token=):"))
        url = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
            setTextColor(fg)
            setText(prefs.getString("url", ""))
        }
        col.addView(url)
        col.addView(Button(this).apply { text = "Start voice"; setOnClickListener { start() } })
        col.addView(Button(this).apply {
            text = "Stop voice"
            setOnClickListener { startService(Intent(this@MainActivity, VoiceService::class.java).setAction(VoiceService.ACTION_STOP)) }
        })
        return col
    }

    private fun handlePairIntent(intent: Intent?) {
        val data = intent?.data?.toString() ?: return
        handlePairLink(data)
        // A consumed link must not be replayed by a rotation or relaunch.
        setIntent(Intent(this, MainActivity::class.java))
    }

    /** The one-time handshake (spec 031 FR7): trade the link's code for this phone's device key. */
    private fun handlePairLink(data: String) {
        val link = Protocol.parsePairLink(data)
        if (link == null) { status.text = "That pairing link isn't valid"; panel.visibility = View.VISIBLE; return }
        status.text = "Pairing…"
        Thread {
            val result = try {
                val body = JSONObject().put("code", link.code).put("name", Build.MODEL).toString()
                    .toRequestBody("application/json".toMediaType())
                // At the home address when the link has one: it works with the tunnel down (spec 034).
                OkHttpClient().newCall(Request.Builder().url("${link.exchangeBase}/pair/exchange").post(body).build())
                    .execute().use { r ->
                        val j = JSONObject(r.body?.string() ?: "{}")
                        if (r.isSuccessful && j.has("device_key")) {
                            // A link without a tunnel keeps the tunnel already stored, and vice versa.
                            val e = prefs.edit().putString("device_key", j.getString("device_key"))
                                .putString("device_id", j.optString("device_id"))
                            link.base?.let { e.putString("base", it) }
                            link.lan?.let { e.putString("lan", it) }
                            e.apply()
                            null
                        } else "Pairing failed: ${j.optString("error", "HTTP ${r.code}")}"
                    }
            } catch (e: Exception) { "Pairing failed: ${e.message}" }
            main.post {
                if (result == null) { loadedBase = null; showPageOrSetup() }
                else { status.text = result; panel.visibility = View.VISIBLE }
            }
        }.start()
    }

    private fun askPermissions() {
        val want = mutableListOf(Manifest.permission.RECORD_AUDIO)
        // Notifications are asked for ONCE (spec 036 FR5): a "no" is respected, not nagged.
        if (Build.VERSION.SDK_INT >= 33 && !prefs.getBoolean("asked_notifications", false)) {
            want += Manifest.permission.POST_NOTIFICATIONS
            prefs.edit().putBoolean("asked_notifications", true).apply()
        }
        val missing = want.filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
        if (missing.isNotEmpty()) requestPermissions(missing.toTypedArray(), 1)
    }

    private fun start() {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            askPermissions(); status.text = "Allow the microphone, then tap again"; return
        }
        val svc = Intent(this, VoiceService::class.java).setAction(VoiceService.ACTION_START)
        if (paired()) {
            // Both paths; the service picks on every (re)connect (spec 034 FR5).
            svc.putExtra(VoiceService.EXTRA_BASE, tunnel()).putExtra(VoiceService.EXTRA_LAN, lan())
                .putExtra(VoiceService.EXTRA_KEY, key())
        } else {
            val text = url.text.toString()
            if (Protocol.parse(text) == null) { status.text = "Pair first, or paste https://…/?token=…"; panel.visibility = View.VISIBLE; return }
            prefs.edit().putString("url", text).apply()
            svc.putExtra(VoiceService.EXTRA_URL, text)
        }
        // Started from his tap while visible: the Android 14 rule for a microphone FGS.
        startForegroundService(svc)
    }
}
