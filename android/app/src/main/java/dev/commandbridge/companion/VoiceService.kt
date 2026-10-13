package dev.commandbridge.companion

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Spec 030: the foreground service that OWNS the audio, so voice keeps working with another app in
 * front or the screen off. It is a second client of the page's socket (web/shell.html), speaking the
 * same messages: `hello`, the `muted`/`capturing`/`channel`/`speaking` controls, binary PCM16 frames
 * up, `audio_header` + bytes (or a streamed reply ending in `audio_end`) down, and a `played` receipt
 * per clip.
 */
class VoiceService : Service() {

    companion object {
        const val ACTION_START = "dev.commandbridge.companion.START"
        const val ACTION_STOP = "dev.commandbridge.companion.STOP"
        const val EXTRA_URL = "url"
        const val EXTRA_BASE = "base"
        /** The home-Wi-Fi address (spec 034), tried before the tunnel on every (re)connect. */
        const val EXTRA_LAN = "lan"
        const val EXTRA_KEY = "device_key"
        private const val TAG = "CBVoice"
        private const val CH_SESSION = "session"
        private const val CH_SPEAK = "speak"
        private const val NOTIF_SESSION = 1
        private const val NOTIF_SPEAK = 2
        /** Spec 040: ONE notification for "heard you", updated per turn, so a burst never stacks. */
        private const val NOTIF_HEARD = 4
        private const val CH_HEARD = "heard"

        /** Read by the activity to paint status; written only by the service. */
        @Volatile var status: String = "stopped"
        @Volatile var running = false
        /** True while the activity is visible: a heads-up would only duplicate what is on screen. */
        @Volatile var appInFront = false
            set(v) {
                field = v
                // Spec 044: the server decides what to notify, so it has to know.
                instance?.let { s -> s.main.post { s.report() } }
            }
        /** The running service, so the activity's foreground changes reach its report. */
        @Volatile private var instance: VoiceService? = null
        /**
         * Spec 044: true once the server's `hello_ack` said it drives the primitive protocol. Then
         * every decision is the server's; until then (an older server) the frozen 0.7.1 fallback runs.
         */
        @Volatile var driven = false
        /** The server's `awake` directive: keep the screen on while the app is in front. */
        @Volatile var awakeWanted = false
        @Volatile var channelOpenNow = false
        /**
         * The page in the app's WebView paints from these (spec 032): `state` (running/channel) and
         * `clip` (start/end with the header). Set by the activity, called on the main thread.
         */
        @Volatile var listener: ((kind: String, json: String) -> Unit)? = null
        const val ACTION_TOGGLE_CHANNEL = "dev.commandbridge.companion.TOGGLE_CHANNEL"
        /** Re-apply the output route after he picked one on the mic caret (spec 033 FR3). */
        const val ACTION_APPLY_ROUTE = "dev.commandbridge.companion.APPLY_ROUTE"
        /** SharedPreferences ("cb") key for the output he picked; absent = the automatic rule. */
        const val PREF_ROUTE = "route"
        /** The AudioDeviceInfo type the voice is routed to right now, or null when not running. */
        @Volatile var currentRoute: Int? = null

        fun stateJson(): String = JSONObject().put("running", running).put("channelOpen", running && channelOpenNow)
            .put("phase", if (!running) "idle" else "live").toString()
    }

    private val main = Handler(Looper.getMainLooper())
    private val http = OkHttpClient.Builder().pingInterval(20, TimeUnit.SECONDS).build()
    private var endpoint: Protocol.Endpoint? = null
    private var socket: WebSocket? = null
    private val connected = AtomicBoolean(false)
    // Not `flags`: Service.onStartCommand has a `flags: Int` parameter that would shadow it there.
    private val session = SessionFlags()
    /** Per-lane hand counts, so a heads-up fires only when a count goes UP (review NICE 6). */
    private val handCounts = HashMap<String, Int>()
    @Volatile private var stopping = false
    private var backoff = 0L

    private var record: AudioRecord? = null
    private var captureThread: Thread? = null
    private val speaking = SpeakingDetector()

    // --- playback: one clip at a time, in order (the page's drainClips) --------------------------
    private sealed class Item {
        abstract val header: JSONObject
        class Whole(override val header: JSONObject, val bytes: ByteArray) : Item()
        class Stream(override val header: JSONObject) : Item() {
            val parts = LinkedBlockingQueue<ByteArray>()
            @Volatile var done = false
            @Volatile var cut: String? = null
        }
    }
    private val queue = LinkedBlockingQueue<Item>()
    private var player: Thread? = null
    @Volatile private var currentTrack: AudioTrack? = null
    @Volatile private var currentCut: String? = null
    private var pendingHeader: JSONObject? = null
    private var streaming: Item.Stream? = null
    private val END = ByteArray(0)

    private var wakeLock: PowerManager.WakeLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            shutdown()
            return START_NOT_STICKY
        }
        if (intent?.action == ACTION_TOGGLE_CHANNEL) {
            // The page's orb while the service runs: open/close the channel, the page's own semantics.
            // Spec 043: OFF also lets go of the call (mic, call mode, communication device), so the
            // phone and the car drop the call and his music resumes; ON takes it back. Main thread,
            // like every syncAudio, so two quick taps apply in order.
            if (running && driven) {
                // Spec 044: the page's socket was down, so its tap came here. Say it to the server on
                // ours; the server's `audio` directive does the rest. Nothing is decided here.
                val next = !session.channelOpen
                session.apply("channel", next)
                if (!next) cutPlayback("channel_off")
                control("channel", next)
                publishState()
            } else if (running) {
                // The frozen 0.7.1 fallback for an older server (spec 044 FR8).
                val next = !session.channelOpen
                session.apply("channel", next)
                if (!next) cutPlayback("channel_off")
                syncAudio()
                // The page's order on its own tap: the mic's state, then the channel.
                control("capturing", next && record != null)
                control("channel", next)
                if (!next && speaking.speaking) { speaking.reset(); control("speaking", false) }
                setStatus(when {
                    !next -> "channel off"
                    !connected.get() -> "reconnecting…"
                    micError != null -> "connected — $micError"
                    else -> "connected"
                })
            }
            return START_NOT_STICKY
        }
        if (intent?.action == ACTION_APPLY_ROUTE) {
            applyRoute()
            if (!running) stopSelf()
            return START_NOT_STICKY
        }
        // NOT sticky (review SHOULD 5): a system restart arrives with a null intent and no Start tap,
        // and Android 14 forbids a microphone service started from the background anyway.
        // Paired (spec 031): base + device key. Fallback (spec 030): a pasted page URL with its token.
        val base = intent?.getStringExtra(EXTRA_BASE)
        val lan = intent?.getStringExtra(EXTRA_LAN)?.let { Protocol.lanOrigin(it) }
        val key = intent?.getStringExtra(EXTRA_KEY)
        val url = intent?.getStringExtra(EXTRA_URL)
        val paired = key != null && (base != null || lan != null)
        val ep = when {
            // Paired: the path is picked on every connect (spec 034), so only a placeholder here.
            paired -> Protocol.pairedEndpoint((base ?: lan)!!, key!!)
            url != null -> Protocol.parse(url)
            else -> { if (!running) stopSelf(); return START_NOT_STICKY }
        }
        if (ep == null) { status = "bad URL"; stopSelf(); return START_NOT_STICKY }
        if (running) return START_NOT_STICKY
        endpoint = ep
        if (paired) { tunnelBase = base; lanBase = lan; deviceKey = key } else { tunnelBase = null; lanBase = null; deviceKey = null }
        createChannels()
        val notif = sessionNotification("connecting…")
        // Android 14+: a microphone FGS must be started while the app is visible (the Start tap).
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(NOTIF_SESSION, notif,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE or ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK)
        } else {
            startForeground(NOTIF_SESSION, notif)
        }
        running = true
        stopping = false
        instance = this
        driven = false
        lastReport = null
        // Outputs coming and going are REPORTED (spec 044), with the orb off too, so the server's
        // route choice and the page's caret always see what the phone has.
        try { audioManager.registerAudioDeviceCallback(reportCallback, main) } catch (_: Exception) {}
        session.apply("channel", true)   // his Start tap opens the channel, as the page's first tap does
        wakeLock = (getSystemService(Context.POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "cb:voice").apply { acquire() }
        startPlayer()
        syncAudio()   // the channel is open: routing, then the mic (spec 043)
        if (record == null) {
            // Keep the service up for playback; the status already names why the mic is missing.
            Log.w(TAG, "capture unavailable: $status")
        }
        watchNetwork()
        connect()
        return START_NOT_STICKY
    }

    // --- the path: home Wi-Fi or the tunnel (spec 034) --------------------------------------------
    private var tunnelBase: String? = null
    private var lanBase: String? = null
    private var deviceKey: String? = null
    private var lastNetwork: android.net.Network? = null
    private var networkSeen = false
    private var networkRegistered = false
    private val connectivity by lazy { getSystemService(Context.CONNECTIVITY_SERVICE) as android.net.ConnectivityManager }
    private val networkCallback = object : android.net.ConnectivityManager.NetworkCallback() {
        // Only a NEW default network acts. A lost one needs nothing: its socket fails on its own and
        // the reconnect re-picks; acting on both would start two reconnects.
        override fun onAvailable(network: android.net.Network) { main.post { networkChanged(network) } }
    }

    private fun watchNetwork() {
        if (lanBase == null || networkRegistered) return   // one path only: nothing to move between
        try {
            connectivity.registerDefaultNetworkCallback(networkCallback)
            networkRegistered = true
        } catch (e: Exception) { Log.w(TAG, "network", e) }
    }

    /**
     * Wi-Fi ⇄ cellular (FR5): the socket on the old network is dead or on the wrong path, so drop it
     * and let the reconnect re-pick. The first callback after registering is the current network:
     * remembered, not acted on.
     */
    private fun networkChanged(network: android.net.Network) {
        if (!networkSeen) { networkSeen = true; lastNetwork = network; return }
        if (network == lastNetwork) return
        lastNetwork = network
        if (stopping || !running) return
        backoff = 0
        val s = socket
        if (s != null) s.cancel()   // its onFailure -> dropped -> connect(), which picks again
        else connect()
    }

    // --- the socket -------------------------------------------------------------------------------
    private fun connect() {
        val key = deviceKey
        if (key != null && (lanBase != null || tunnelBase != null)) {
            // Probing blocks (up to ~1.5 s), so pick off the main thread, then connect on it.
            setStatus("connecting…")
            Thread({
                val pick = Paths.pick(lanBase, tunnelBase, key)
                main.post {
                    if (stopping) return@post
                    if (pick == null) { dropped("no path"); return@post }
                    endpoint = Protocol.pairedEndpoint(pick.first, key)
                    open()
                }
            }, "cb-path").start()
            return
        }
        open()
    }

    private fun open() {
        val ep = endpoint ?: return
        setStatus("connecting…")
        val req = Request.Builder().url(ep.socketUrl).apply {
            ep.deviceKey?.let { header(Protocol.DEVICE_HEADER, it) }
        }.build()
        socket = http.newWebSocket(req, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                connected.set(true)
                backoff = 0
                // Not driven until THIS server's ack says so (an old server never will), and the
                // first report after the ack goes out whatever was sent on the last socket.
                driven = false
                main.post { lastReport = null }
                send(JSONObject().put("type", "hello").put("sample_rate", Protocol.SAMPLE_RATE)
                    .put("streams", true)
                    .put("mic", JSONObject().put("device", "android-companion").put("echoCancellation", true))
                    // Spec 044: the primitives this app can carry out.
                    .put("native", Primitives.hello(appVersion())))
                // Re-assert on every connect, as the page does (FR7) — but never `muted`: the app
                // adopts the session's mute, it does not undo one (review MUST 2).
                speaking.reset()
                for ((type, value) in session.onConnect()) control(type, value)
                setStatus(if (micError != null) "connected — $micError" else "connected")
            }
            override fun onMessage(webSocket: WebSocket, text: String) = onText(text)
            override fun onMessage(webSocket: WebSocket, bytes: ByteString) = onBytes(bytes.toByteArray())
            // Only the CURRENT socket's end counts: a socket dropped on a network change ends after
            // its replacement may already be connecting (spec 034).
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                if (webSocket === socket) dropped("closed: $reason")
            }
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                if (webSocket === socket) dropped(t.message ?: "failure")
            }
        })
    }

    private fun dropped(why: String) {
        connected.set(false)
        if (stopping) return
        backoff = Protocol.nextBackoff(backoff)
        setStatus("reconnecting… ($why)")
        main.postDelayed({ if (!stopping && !connected.get()) connect() }, backoff)
    }

    private fun send(o: JSONObject) { socket?.send(o.toString()) }
    private fun control(type: String, value: Boolean) = send(JSONObject().put("type", type).put("value", value))

    private fun onText(text: String) {
        val msg = try { JSONObject(text) } catch (e: Exception) { return }
        when (msg.optString("type")) {
            "hello_ack" -> {
                driven = Primitives.driven(msg)
                if (driven) main.post { report() }
            }
            "native" -> {
                // Spec 044: a directive from the server. Applied on the main thread, in order, with
                // the taps; an op this build does not know is ignored.
                val d = Primitives.parse(msg) ?: return
                main.post { apply(d) }
            }
            "audio_header" -> {
                // THE ORB IS OFF: PLAY NOTHING (spec 036). The server holds clips while the channel is
                // closed; this is the second lock on the same door, after a reconnect flush once let a
                // held clip through (JJ: "I had the orb turned off and you still spoke to me"). No
                // receipt either, so the server still counts it unheard and holds it.
                if (!session.channelOpen) { pendingHeader = null; streaming = null; return }
                if (msg.optBoolean("streamed")) {
                    val s = Item.Stream(msg)
                    streaming = s
                    queue.put(s)
                } else {
                    pendingHeader = msg
                }
                notifySpeech(msg)
            }
            "audio_end" -> { streaming?.let { it.done = true; it.parts.put(END) }; streaming = null }
            "stop_playback" -> cutPlayback("barge_in")
            "channel" -> {
                session.apply("channel", msg.optBoolean("value", session.channelOpen))
                if (!session.channelOpen) cutPlayback("channel_off")
                // Spec 043: unlike the page (which keeps its mic on a broadcast close), the app lets
                // the call go however the channel closed — with the channel shut its mic sends
                // nothing anyway, so holding it only keeps the car on a call. No echo back to the
                // server: adopting a broadcast never replies to it. On the main thread with the taps.
                // Driven (spec 044), the server's `audio` directive does this instead.
                if (!driven) main.post {
                    syncAudio()
                    if (!session.channelOpen && speaking.speaking) speaking.reset()
                }
                setStatus(if (session.channelOpen) "connected" else "channel off")
            }
            "muted" -> {
                session.apply("muted", msg.optBoolean("value", session.muted))
                if (session.muted && speaking.speaking) { speaking.reset(); control("speaking", false) }
                setStatus(if (session.muted) "muted" else "connected")
            }
            "clip_played" -> {
                // Heard on another device (spec 032): drop our queued copy so it never plays twice.
                val id = msg.optString("id")
                queue.removeIf { it.header.optString("id") == id }
            }
            "turn" -> {
                // SPEC 040 (JJ: "whenever you do recognize me when I say something, I would like a
                // notification with what has been transcribed from me"). The server sends `turn`
                // only for a FINAL turn; partials are `partial` and never get here.
                val text = msg.optString("text", "")
                // Driven (spec 044), the server sends this as a `notify` directive instead.
                if (!driven && Notices.heardYou("turn", msg.optBoolean("addressed"), text, appInFront)) {
                    notifyHeard(msg.optString("lane", ""), text)
                }
            }
            "lane_waiting" -> {
                if (driven) return   // spec 044: the server's `notify` directive
                val lane = msg.optString("lane", "an agent")
                val now = msg.optInt("waiting", 0)
                val before = handCounts.put(lane, now) ?: 0
                if (now > before) notifyHand(lane)
            }
        }
    }

    private fun onBytes(bytes: ByteArray) {
        val s = streaming
        if (s != null) { if (s.cut == null) s.parts.put(bytes); return }
        val h = pendingHeader ?: return
        pendingHeader = null
        queue.put(Item.Whole(h, bytes))
    }

    // --- capture ----------------------------------------------------------------------------------
    @Volatile private var micError: String? = null

    private fun micFailed(why: String) {
        micError = why
        setStatus("mic error: $why")
    }

    /** Returns false (and says why on the notification) when the microphone cannot be opened. */
    private fun startCapture(): Boolean {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            micFailed("permission missing"); return false
        }
        val min = AudioRecord.getMinBufferSize(Protocol.SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val rec = try {
            AudioRecord(MediaRecorder.AudioSource.VOICE_COMMUNICATION, Protocol.SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, maxOf(min, Protocol.FRAME_SAMPLES * 4))
        } catch (e: Exception) { micFailed(e.message ?: "cannot open"); return false }
        // Review SHOULD 4: a mic held by another app yields an UNINITIALIZED recorder, and
        // startRecording() on it throws and kills the service.
        if (rec.state != AudioRecord.STATE_INITIALIZED) {
            rec.release(); micFailed("microphone busy or unavailable"); return false
        }
        try { rec.startRecording() } catch (e: Exception) {
            rec.release(); micFailed(e.message ?: "cannot start"); return false
        }
        record = rec
        micError = null
        // This recorder's own stop flag (spec 043): the orb going off ends THIS loop, and a later
        // reopen starts a new one, so an old thread can never read from a newer recorder.
        val released = AtomicBoolean(false)
        captureReleased = released
        captureThread = Thread({
            val buf = ShortArray(Protocol.FRAME_SAMPLES)
            loop@ while (!stopping && !released.get()) {
                var n = 0
                while (n < buf.size && !stopping && !released.get()) {
                    val r = rec.read(buf, n, buf.size - n)
                    // A negative read is an error code, not "no data yet": stop rather than spin
                    // forever on it while holding the wake lock (review SHOULD 4). A read cut short
                    // by our own release is not an error.
                    if (r < 0) { if (!released.get() && !stopping) micFailed("read error $r"); break@loop }
                    if (r == 0) break
                    n += r
                }
                if (n <= 0) continue
                // Muted or a closed channel sends nothing; the page's onChunk does the same, and its
                // meter drops to zero.
                if (!session.shouldSend(connected.get())) { showLevel(0.0); continue }
                val level = Protocol.rms(buf, n)
                speaking.update(level, SystemClock.elapsedRealtime())?.let { control("speaking", it) }
                showLevel(Protocol.pageLevel(level))
                socket?.send(Protocol.shortsToBytes(buf, n).toByteString())
            }
        }, "cb-capture").apply { start() }
        return true
    }

    @Volatile private var captureReleased: AtomicBoolean? = null

    /**
     * Spec 043: stop and RELEASE the recorder, so no VOICE_COMMUNICATION capture is left holding
     * the call open. stop() unblocks the read; the thread is joined before release() so the native
     * recorder is never freed under a read in progress. Idempotent.
     */
    private fun stopCapture() {
        val rec = record ?: return
        record = null
        captureReleased?.set(true)
        captureReleased = null
        try { rec.stop() } catch (_: Exception) {}
        captureThread?.let { try { it.join(500) } catch (_: InterruptedException) {} }
        captureThread = null
        try { rec.release() } catch (_: Exception) {}
        showLevel(0.0)
    }

    /**
     * Spec 043: bring the held call resources in line with the channel. MAIN THREAD ONLY — that is
     * what makes quick toggles safe: each call sees the effect of the one before and applies only
     * the difference (CallHold.plan), so a repeat is a no-op.
     */
    private fun syncAudio() {
        val want = CallHold.wanted(running && !stopping, session.channelOpen)
        applying = true
        try {
            for (step in CallHold.plan(want, micHeld = record != null, routed = routed)) when (step) {
                CallHold.Step.ROUTE -> startRouting()
                CallHold.Step.OPEN_MIC -> startCapture()
                CallHold.Step.RELEASE_MIC -> stopCapture()
                CallHold.Step.UNROUTE -> try { stopRouting() } catch (e: Exception) { Log.w(TAG, "unroute", e) }
            }
        } finally { applying = false }
        emit("routes", "{}")
        report()
    }

    /**
     * His level and speech state, for the page's meter and his lane's wave (spec 033 FR1). Only while
     * the app is in front (nobody is looking otherwise), and a run of zeros is sent once, not 25 times
     * a second.
     */
    private var lastLevelSent = -1.0
    private fun showLevel(level: Double) {
        if (!appInFront || listener == null) { lastLevelSent = -1.0; return }
        if (level == 0.0 && lastLevelSent == 0.0) return
        lastLevelSent = level
        emit("level", JSONObject().put("level", Math.round(level * 1000) / 1000.0)
            .put("speaking", level > 0.0 && speaking.speaking).toString())
    }

    // --- playback ---------------------------------------------------------------------------------
    private fun startPlayer() {
        player = Thread({
            while (!stopping) {
                val item = try { queue.take() } catch (e: InterruptedException) { break }
                currentCut = null
                val rate = item.header.optInt("sample_rate", 24000)
                val track = AudioTrack.Builder()
                    // VOICE_COMMUNICATION, matching the capture source, so the platform's echo canceller
                    // has the agent's voice as its reference and cancels it (review SHOULD 3).
                    .setAudioAttributes(AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                    .setAudioFormat(AudioFormat.Builder().setSampleRate(rate)
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build())
                    .setTransferMode(AudioTrack.MODE_STREAM)
                    .setBufferSizeInBytes(maxOf(AudioTrack.getMinBufferSize(rate,
                        AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT), rate / 5))
                    .build()
                currentTrack = track
                var written = 0
                emit("clip", JSONObject().put("kind", "start").put("header", item.header).toString())
                try {
                    track.play()
                    when (item) {
                        is Item.Whole -> written += writeAll(track, item.bytes)
                        is Item.Stream -> while (currentCut == null) {
                            val part = item.parts.poll(30, TimeUnit.SECONDS) ?: break
                            if (part === END) break
                            written += writeAll(track, part)
                        }
                    }
                    // Let the buffered tail play out before the receipt says it was heard.
                    val frames = written / 2
                    val deadline = SystemClock.elapsedRealtime() + 5000
                    while (currentCut == null && track.playbackHeadPosition < frames &&
                        SystemClock.elapsedRealtime() < deadline) Thread.sleep(20)
                } catch (e: Exception) {
                    Log.w(TAG, "playback", e)
                    currentCut = currentCut ?: "error"
                } finally {
                    val heard = track.playbackHeadPosition.toDouble() / rate
                    try { track.stop() } catch (_: Exception) {}
                    track.release()
                    currentTrack = null
                    receipt(item.header, currentCut, heard, written / 2.0 / rate)
                    emit("clip", JSONObject().put("kind", "end").put("header", item.header).toString())
                }
            }
        }, "cb-player").apply { start() }
    }

    private fun writeAll(track: AudioTrack, bytes: ByteArray): Int {
        var off = 0
        while (off < bytes.size && currentCut == null) {
            val w = track.write(bytes, off, bytes.size - off)
            if (w <= 0) break
            off += w
        }
        return off
    }

    private fun receipt(header: JSONObject, cut: String?, heard: Double, duration: Double) {
        val cue = header.optString("cue", "")
        // A cue gets no receipt except the shutdown notice, as on the page.
        if (cue.isNotEmpty() && cue != "shutdown") return
        val o = JSONObject().put("type", "played").put("id", header.optString("id"))
        if (cut != null) {
            o.put("cut", cut).put("heard_s", Math.round(heard * 10) / 10.0)
                .put("duration_s", Math.round(duration * 10) / 10.0)
        }
        send(o)
    }

    private fun cutPlayback(why: String) {
        currentCut = why
        currentTrack?.let { try { it.pause(); it.flush() } catch (_: Exception) {} }
        streaming?.let { it.cut = why; it.parts.put(END) }
        // Anything still waiting was part of the same interruption (the page clears its queue too).
        // NO receipt for those, exactly like the page: the server hands unplayed clips back to their
        // lanes' holds, and a `played` here would retire replies he never heard (spec 032).
        val dropped = ArrayList<Item>()
        queue.drainTo(dropped)
        for (d in dropped) { if (d is Item.Stream) d.cut = why }
    }

    // --- notifications (local only, no FCM) ------------------------------------------------------
    private fun createChannels() {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CH_SESSION, "Voice session", NotificationManager.IMPORTANCE_LOW))
        nm.createNotificationChannel(NotificationChannel(CH_SPEAK, "Agent messages", NotificationManager.IMPORTANCE_HIGH))
        // Spec 040: what he said, heard. Its OWN channel, kept quiet: it peeks so he sees it from
        // another app, but makes no sound or buzz — he just spoke, and he will hear the answer.
        nm.createNotificationChannel(NotificationChannel(CH_HEARD, "What you said", NotificationManager.IMPORTANCE_HIGH)
            .apply { setSound(null, null); enableVibration(false) })
    }

    private fun notifyHeard(lane: String, text: String) {
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) return
        val to = lane.replaceFirstChar { it.uppercase() }
        val n = Notification.Builder(this, CH_HEARD)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setVisibility(Notification.VISIBILITY_PRIVATE)
            .setContentTitle(if (to.isNotEmpty()) "Heard you → $to" else "Heard you")
            .setContentText(text)
            .setStyle(Notification.BigTextStyle().bigText(text))
            .setAutoCancel(true)
            .setContentIntent(openAppIntent())
            .build()
        getSystemService(NotificationManager::class.java).notify(NOTIF_HEARD, n)
    }

    private fun openAppIntent(): PendingIntent = PendingIntent.getActivity(this, 0,
        Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
        PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)

    private fun sessionNotification(text: String): Notification {
        val stop = PendingIntent.getService(this, 1,
            Intent(this, VoiceService::class.java).setAction(ACTION_STOP), PendingIntent.FLAG_IMMUTABLE)
        return Notification.Builder(this, CH_SESSION)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setVisibility(Notification.VISIBILITY_PRIVATE)
            .setContentTitle("Command Bridge")
            .setContentText(text)
            .setOngoing(true)
            .setContentIntent(openAppIntent())
            .addAction(Notification.Action.Builder(null, "Stop", stop).build())
            .build()
    }

    private fun notifySpeech(header: JSONObject) {
        if (driven) return   // spec 044: the server's `notify` directive
        if (appInFront || header.optString("cue", "").isNotEmpty()) return
        val text = header.optString("text", "")
        if (text.isEmpty()) return
        val who = header.optString("lane", header.optString("agent", "Agent"))
        post(who.replaceFirstChar { it.uppercase() }, text)
    }

    private fun notifyHand(lane: String) {
        if (appInFront) return
        post("${lane.replaceFirstChar { it.uppercase() }} raised a hand", "Switch to that lane to hear it.")
    }

    private fun post(title: String, text: String) {
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) return
        // PRIVATE (review NICE 6): what an agent said is his work; the lock screen shows only that
        // something arrived.
        val n = Notification.Builder(this, CH_SPEAK)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setVisibility(Notification.VISIBILITY_PRIVATE)
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(Notification.BigTextStyle().bigText(text))
            .setAutoCancel(true)
            .setContentIntent(openAppIntent())
            .build()
        getSystemService(NotificationManager::class.java).notify(NOTIF_SPEAK, n)
    }

    private fun setStatus(s: String) {
        status = s
        if (running) getSystemService(NotificationManager::class.java).notify(NOTIF_SESSION, sessionNotification(s))
        publishState()
    }

    private fun publishState() {
        channelOpenNow = session.channelOpen
        emit("state", stateJson())
    }

    private fun emit(kind: String, json: String) {
        val l = listener ?: return
        main.post { l(kind, json) }
    }

    // --- the output route (spec 032 FR1) ---------------------------------------------------------
    private val audioManager by lazy { getSystemService(Context.AUDIO_SERVICE) as android.media.AudioManager }
    private val deviceCallback = object : android.media.AudioDeviceCallback() {
        override fun onAudioDevicesAdded(added: Array<out android.media.AudioDeviceInfo>) = applyRoute()
        override fun onAudioDevicesRemoved(removed: Array<out android.media.AudioDeviceInfo>) = applyRoute()
    }

    /** True while the service holds call mode + a communication device (spec 043). Main thread. */
    @Volatile private var routed = false

    private fun startRouting() {
        routed = true
        audioManager.mode = android.media.AudioManager.MODE_IN_COMMUNICATION
        applyRoute()
        audioManager.registerAudioDeviceCallback(deviceCallback, main)
    }

    private fun rememberedRoute(): Int? =
        getSharedPreferences("cb", MODE_PRIVATE).getInt(PREF_ROUTE, -1).takeIf { it >= 0 }

    private fun applyRoute() {
        // Not routed = the orb is off (spec 043): picking an output on the caret then must NOT set a
        // communication device, or the car is back on a call. The pick is remembered (PREF_ROUTE)
        // and applied when the channel reopens.
        if (!running || !routed) return
        val remembered = rememberedRoute()
        if (Build.VERSION.SDK_INT >= 31) {
            val devices = audioManager.availableCommunicationDevices
            // Driven (spec 044): the server named the output; null = it has not, leave the route.
            val want = if (driven) drivenDevice else AudioRoute.choose(devices.map { it.type }, remembered)
            val pick = devices.firstOrNull { it.type == want }
            if (pick != null && audioManager.setCommunicationDevice(pick)) currentRoute = pick.type
        } else {
            val types = audioManager.getDevices(android.media.AudioManager.GET_DEVICES_OUTPUTS).map { it.type }
            val speaker = if (driven) drivenDevice == null || drivenDevice == AudioRoute.TYPE_BUILTIN_SPEAKER
                else AudioRoute.speakerphone(types, remembered)
            @Suppress("DEPRECATION")
            audioManager.isSpeakerphoneOn = speaker
            currentRoute = if (speaker) AudioRoute.TYPE_BUILTIN_SPEAKER
                else if (driven) drivenDevice else AudioRoute.choose(types, remembered)
        }
        // The page's caret paints the route actually applied, not the one asked for.
        emit("routes", "{}")
        report()
    }

    // --- spec 044: the primitives the server drives ----------------------------------------------
    /** The output the server's last `audio` directive named. */
    @Volatile private var drivenDevice: Int? = null
    private var lastReport: Primitives.Report? = null
    private var applying = false

    private val reportCallback = object : android.media.AudioDeviceCallback() {
        override fun onAudioDevicesAdded(added: Array<out android.media.AudioDeviceInfo>) = report()
        override fun onAudioDevicesRemoved(removed: Array<out android.media.AudioDeviceInfo>) = report()
    }

    private fun appVersion(): String = try {
        packageManager.getPackageInfo(packageName, 0).versionName ?: ""
    } catch (_: Exception) { "" }

    /** The output types the phone has now, as the server's route rule needs them. */
    private fun deviceTypes(): List<Int> =
        if (Build.VERSION.SDK_INT >= 31) audioManager.availableCommunicationDevices.map { it.type }.distinct()
        else (listOf(AudioRoute.TYPE_BUILTIN_SPEAKER, AudioRoute.TYPE_BUILTIN_EARPIECE) +
            audioManager.getDevices(android.media.AudioManager.GET_DEVICES_OUTPUTS).map { it.type }).distinct()

    /** FR2: what the app holds now, to the server, only when it changed. Main thread. */
    private fun report() {
        // Mid-directive the state is half-applied (call up, mic not yet): report once, at the end.
        if (!driven || !connected.get() || !running || applying) return
        val device = if (!routed) null
            else if (Build.VERSION.SDK_INT >= 31) audioManager.communicationDevice?.type ?: currentRoute
            else currentRoute
        val r = Primitives.Report(appInFront, deviceTypes(), record != null, routed, device, micError)
        if (r == lastReport) return
        lastReport = r
        send(r.toJson())
    }

    /** One directive from the server, carried out. Main thread. Decides nothing. */
    private fun apply(d: Primitives.Directive) {
        if (!running || stopping) return
        when (d) {
            is Primitives.Directive.Audio -> {
                drivenDevice = d.device
                applying = true
                try {
                    for (step in CallHold.plan(wantMic = d.mic, wantCall = d.call, micHeld = record != null, routed = routed)) when (step) {
                        CallHold.Step.ROUTE -> startRouting()
                        CallHold.Step.OPEN_MIC -> startCapture()
                        CallHold.Step.RELEASE_MIC -> stopCapture()
                        CallHold.Step.UNROUTE -> try { stopRouting() } catch (e: Exception) { Log.w(TAG, "unroute", e) }
                    }
                    if (routed) applyRoute()   // a new output on a call already held
                } finally { applying = false }
                if (record == null && speaking.speaking) { speaking.reset(); control("speaking", false) }
                emit("routes", "{}")
                report()
            }
            is Primitives.Directive.Notify -> {
                if (Build.VERSION.SDK_INT >= 33 &&
                    checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) return
                val n = Notification.Builder(this, if (d.quiet) CH_HEARD else CH_SPEAK)
                    .setSmallIcon(android.R.drawable.ic_btn_speak_now)
                    .setVisibility(Notification.VISIBILITY_PRIVATE)
                    .setContentTitle(d.title)
                    .setContentText(d.text)
                    .setStyle(Notification.BigTextStyle().bigText(d.text))
                    .setAutoCancel(true)
                    .setContentIntent(openAppIntent())
                    .build()
                getSystemService(NotificationManager::class.java).notify(Primitives.notificationId(d.key), n)
            }
            is Primitives.Directive.NotifyCancel ->
                getSystemService(NotificationManager::class.java).cancel(Primitives.notificationId(d.key))
            is Primitives.Directive.Awake -> { awakeWanted = d.on; publishState() }
        }
    }

    private fun stopRouting() {
        routed = false
        try { audioManager.unregisterAudioDeviceCallback(deviceCallback) } catch (_: Exception) {}
        if (Build.VERSION.SDK_INT >= 31) audioManager.clearCommunicationDevice()
        else @Suppress("DEPRECATION") { audioManager.isSpeakerphoneOn = false }
        audioManager.mode = android.media.AudioManager.MODE_NORMAL
        currentRoute = null
    }

    // --- shutdown ---------------------------------------------------------------------------------
    private fun shutdown() {
        stopping = true
        if (connected.get()) {
            control("capturing", false)
            control("channel", false)
            control("speaking", false)
        }
        cutPlayback("stopped")
        try { socket?.close(1000, "stopped") } catch (_: Exception) {}
        stopCapture()
        player?.interrupt()
        wakeLock?.let { if (it.isHeld) it.release() }
        try { stopRouting() } catch (_: Exception) {}
        try { audioManager.unregisterAudioDeviceCallback(reportCallback) } catch (_: Exception) {}
        instance = null
        driven = false
        awakeWanted = false
        if (networkRegistered) {
            try { connectivity.unregisterNetworkCallback(networkCallback) } catch (_: Exception) {}
            networkRegistered = false
        }
        networkSeen = false
        lastNetwork = null
        running = false
        status = "stopped"
        publishState()
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    override fun onDestroy() {
        if (running) shutdown()
        super.onDestroy()
    }
}
