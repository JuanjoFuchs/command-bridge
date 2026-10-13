package dev.commandbridge.companion

import java.net.URI
import kotlin.math.sqrt

/**
 * Pure logic shared with the page (web/shell.html), kept free of Android types so it is unit-tested
 * on the JVM. Spec 030: the app is a second client of the SAME socket, so every constant here
 * mirrors the page's, and a change to one is a change to both.
 */
object Protocol {
    /** The rate the app captures and announces in `hello`; the server resamples anything else. */
    const val SAMPLE_RATE = 16000

    /** ~40 ms frames, the page's batching (shell.html: `batchSize = sampleRate * 0.04`). */
    const val FRAME_SAMPLES = SAMPLE_RATE / 25

    /** The page's speaking thresholds (shell.html: SPEAKING_RMS / MIN_RUN / HANGOVER). */
    const val SPEAKING_RMS = 0.045
    const val SPEAKING_MIN_RUN_MS = 120L
    const val SPEAKING_HANGOVER_MS = 700L

    /** Reconnect backoff, the page's RECONNECT_MIN / RECONNECT_MAX. */
    const val RECONNECT_MIN_MS = 500L
    const val RECONNECT_MAX_MS = 8000L

    /**
     * How the app reaches the server. `token` for the pasted-URL fallback (spec 030), `deviceKey` once
     * paired (spec 031): the key rides as the `X-CB-Device` header so ngrok's policy lets the request
     * past the browser sign-in and the server alone judges it.
     */
    data class Endpoint(
        val socketUrl: String, val pageUrl: String, val spectateUrl: String,
        val token: String? = null, val deviceKey: String? = null,
    )

    const val DEVICE_HEADER = "X-CB-Device"

    /** `base` is the https tunnel, `lan` the home-Wi-Fi address (spec 034); at least one is set. */
    data class PairLink(val base: String?, val lan: String?, val code: String) {
        /** Where the one-time code is traded: the LAN when the link has one (it works with the tunnel down). */
        val exchangeBase: String get() = lan ?: base!!
    }

    /**
     * `commandbridge://pair?base=<https origin>&lan=<http://private-ip:port>&code=<code>` → where to
     * exchange and the one-time code. A `base` must be https: the code and the key it buys never cross
     * the internet in clear text. A `lan` must be http to a PRIVATE IPv4 address with a port: the only
     * clear text the app ever sends, and only on his own network (spec 034 FR4/FR6). A link is the one
     * input an attacker could hand him, so anything else is refused.
     */
    fun parsePairLink(link: String): PairLink? {
        val uri = try { URI(link.trim()) } catch (e: Exception) { return null }
        if (uri.scheme?.lowercase() != "commandbridge") return null
        if ((uri.host ?: uri.rawAuthority) != "pair") return null
        fun param(name: String) = queryParam(uri.rawQuery, name)?.let { java.net.URLDecoder.decode(it, "UTF-8") }
        val code = param("code") ?: return null
        if (code.isEmpty()) return null
        val base = param("base")?.let { httpsOrigin(it) ?: return null }
        val lan = param("lan")?.let { lanOrigin(it) ?: return null }
        if (base == null && lan == null) return null
        return PairLink(base, lan, code)
    }

    private fun httpsOrigin(s: String): String? {
        val b = try { URI(s) } catch (e: Exception) { return null }
        if (b.scheme?.lowercase() != "https" || b.rawAuthority.isNullOrEmpty()) return null
        return "https://${b.rawAuthority}"
    }

    /** `http://<RFC 1918 IPv4>:<port>` and nothing else (spec 034 FR6). */
    fun lanOrigin(s: String): String? {
        val b = try { URI(s.trim()) } catch (e: Exception) { return null }
        if (b.scheme?.lowercase() != "http") return null
        val host = b.host ?: return null
        if (b.port <= 0 || !isPrivateV4(host)) return null
        return "http://$host:${b.port}"
    }

    /** RFC 1918 only, as a dotted-quad literal (no names: a name could resolve anywhere). */
    fun isPrivateV4(host: String): Boolean {
        val p = host.split(".")
        if (p.size != 4) return false
        val o = p.map { it.toIntOrNull()?.takeIf { n -> n in 0..255 && it == n.toString() } ?: return false }
        return o[0] == 10 || (o[0] == 172 && o[1] in 16..31) || (o[0] == 192 && o[1] == 168)
    }

    /**
     * Which path to use (spec 034 FR5): the home Wi-Fi when it answered, else the tunnel, else the
     * home address anyway (nothing better is known). Returns (base, "home" | "tunnel"), or null.
     */
    fun choosePath(lan: String?, tunnel: String?, lanAnswered: Boolean): Pair<String, String>? = when {
        lan != null && lanAnswered -> lan to "home"
        tunnel != null -> tunnel to "tunnel"
        lan != null -> lan to "home"
        else -> null
    }

    /** A paired device's endpoint: socket with no token in the URL; the key goes in the header. */
    fun pairedEndpoint(base: String, deviceKey: String): Endpoint {
        val b = URI(base)
        val host = b.rawAuthority
        val secure = b.scheme?.lowercase() != "http"
        val ws = if (secure) "wss" else "ws"
        val http = if (secure) "https" else "http"
        return Endpoint(
            socketUrl = "$ws://$host/ws",
            pageUrl = "$http://$host/",
            spectateUrl = "$http://$host/?spectate=1",
            deviceKey = deviceKey,
        )
    }

    /**
     * One pasted page URL (`https://host/?token=…`) → everything the app needs. Returns null when it
     * is not a usable http(s) URL with a token, so the UI can say so instead of connecting blindly.
     */
    fun parse(pageUrl: String): Endpoint? {
        val trimmed = pageUrl.trim()
        val uri = try { URI(trimmed) } catch (e: Exception) { return null }
        val scheme = uri.scheme?.lowercase() ?: return null
        if (scheme != "https" && scheme != "http") return null
        val host = uri.rawAuthority ?: return null
        val token = queryParam(uri.rawQuery, "token")
        if (token.isNullOrEmpty()) return null
        val ws = if (scheme == "https") "wss" else "ws"
        val base = "$scheme://$host"
        return Endpoint(
            socketUrl = "$ws://$host/ws?token=$token",
            pageUrl = "$base/?token=$token",
            spectateUrl = "$base/?token=$token&spectate=1",
            token = token,
        )
    }

    private fun queryParam(rawQuery: String?, name: String): String? {
        if (rawQuery.isNullOrEmpty()) return null
        for (pair in rawQuery.split("&")) {
            val i = pair.indexOf('=')
            val key = if (i >= 0) pair.substring(0, i) else pair
            if (key == name) return if (i >= 0) pair.substring(i + 1) else ""
        }
        return null
    }

    /** Little-endian PCM16 bytes, exactly what the page's `floatToPCM16(...).buffer` puts on the wire. */
    fun shortsToBytes(samples: ShortArray, count: Int = samples.size): ByteArray {
        val out = ByteArray(count * 2)
        for (i in 0 until count) {
            val v = samples[i].toInt()
            out[2 * i] = (v and 0xff).toByte()
            out[2 * i + 1] = ((v shr 8) and 0xff).toByte()
        }
        return out
    }

    /** RMS on the page's scale (samples normalised to [-1, 1]). */
    fun rms(samples: ShortArray, count: Int = samples.size): Double {
        if (count == 0) return 0.0
        var sum = 0.0
        for (i in 0 until count) {
            val f = samples[i] / 32768.0
            sum += f * f
        }
        return sqrt(sum / count)
    }

    /** The level the page's meter and lane wave follow (shell.html onChunk: `min(1, r / 0.15)`). */
    fun pageLevel(rms: Double): Double = minOf(1.0, rms / 0.15)

    fun nextBackoff(current: Long): Long =
        if (current <= 0) RECONNECT_MIN_MS else minOf(RECONNECT_MAX_MS, current * 2)
}

/**
 * Where the agent's voice goes (spec 032 FR1). VOICE_COMMUNICATION (needed for the echo canceller)
 * puts Android in call mode, whose default output is the EARPIECE — JJ, 2026-10-09: "now it's using
 * the headset as if I were speaking on the phone. But I was using the speaker." So the service picks:
 * a headset if one is connected, else the loudspeaker; never the earpiece.
 * Values are android.media.AudioDeviceInfo.TYPE_* (literals so this stays JVM-testable).
 */
object AudioRoute {
    const val TYPE_BUILTIN_EARPIECE = 1
    const val TYPE_BUILTIN_SPEAKER = 2
    const val TYPE_WIRED_HEADSET = 3
    const val TYPE_WIRED_HEADPHONES = 4
    const val TYPE_BLUETOOTH_SCO = 7
    const val TYPE_USB_HEADSET = 22
    const val TYPE_BLE_HEADSET = 26

    private val PREFERENCE = listOf(
        TYPE_BLUETOOTH_SCO, TYPE_BLE_HEADSET, TYPE_WIRED_HEADSET, TYPE_USB_HEADSET,
        TYPE_WIRED_HEADPHONES, TYPE_BUILTIN_SPEAKER,
    )

    /**
     * The type to route to among the available communication devices, or null if none fits. A
     * `remembered` choice he made on the mic caret (spec 033 FR3) wins whenever that output is present;
     * otherwise the automatic rule: a headset, else the loudspeaker.
     */
    fun choose(available: List<Int>, remembered: Int? = null): Int? =
        if (remembered != null && remembered in available) remembered
        else PREFERENCE.firstOrNull { it in available }

    /** The outputs he can pick on the caret, in a stable order, with the names the page shows. */
    private val LABELS = linkedMapOf(
        TYPE_BUILTIN_SPEAKER to "Speaker",
        TYPE_BUILTIN_EARPIECE to "Phone (earpiece)",
        TYPE_BLUETOOTH_SCO to "Bluetooth",
        TYPE_BLE_HEADSET to "Bluetooth LE",
        TYPE_WIRED_HEADSET to "Wired headset",
        TYPE_WIRED_HEADPHONES to "Wired headphones",
        TYPE_USB_HEADSET to "USB headset",
    )

    /** The pickable outputs among `available`, each once, as (type, label). */
    fun choices(available: List<Int>): List<Pair<Int, String>> =
        LABELS.filterKeys { it in available }.map { it.key to it.value }

    /** For Android < 12: whether to turn the speakerphone on (true unless a headset is present). */
    fun speakerphone(available: List<Int>, remembered: Int? = null): Boolean =
        choose(available, remembered).let { it == null || it == TYPE_BUILTIN_SPEAKER }
}

/**
 * The session switches the app ADOPTS from the server rather than asserting (spec 030 review MUST 2).
 * `muted` is the session's mute: a laptop page muting must stop the phone's audio being transcribed
 * too, and the app connecting must never UNMUTE a session someone muted. So the app never sends
 * `muted` on connect; it only follows the server's `muted` and `channel` broadcasts.
 */
class SessionFlags {
    @Volatile var muted = false
        private set
    @Volatile var channelOpen = true
        private set

    /** Apply a server control message; returns true when it was one of ours. */
    fun apply(type: String, value: Boolean): Boolean = when (type) {
        "muted" -> { muted = value; true }
        "channel" -> { channelOpen = value; true }
        else -> false
    }

    /** May a captured frame leave the phone? The page's onChunk gates on exactly these. */
    fun shouldSend(connected: Boolean): Boolean = connected && channelOpen && !muted

    /**
     * What the app re-asserts on every (re)connect, in order. `muted` is deliberately absent.
     * `capturing` follows the channel (spec 043): with the orb off the microphone is released, so
     * claiming to capture would be a lie.
     */
    fun onConnect(): List<Pair<String, Boolean>> =
        listOf("capturing" to channelOpen, "channel" to channelOpen, "speaking" to false)
}

/**
 * Spec 043: what the voice service holds per channel state. A VOICE_COMMUNICATION recorder and
 * MODE_IN_COMMUNICATION with a communication device are, to Android and to a car over Bluetooth
 * HFP, an active CALL — JJ: "the car Bluetooth infotainment system still thinks I'm on a call". So
 * the call is held only while the channel is open, and with the orb off everything is let go.
 */
object CallHold {
    enum class Step { ROUTE, OPEN_MIC, RELEASE_MIC, UNROUTE }

    /** The call resources are wanted only while the service runs AND the channel is open. */
    fun wanted(running: Boolean, channelOpen: Boolean): Boolean = running && channelOpen

    /**
     * The steps that bring what is held (`micHeld`, `routed`) to what is wanted, in order: routing
     * before the mic on the way up (the start path's order), the mic before routing on the way
     * down. Empty when nothing needs to change, so applying it twice is a no-op.
     */
    fun plan(want: Boolean, micHeld: Boolean, routed: Boolean): List<Step> =
        plan(wantMic = want, wantCall = want, micHeld = micHeld, routed = routed)

    /**
     * Spec 044: the server's `audio` directive names the mic and the call separately. Releases come
     * first (mic, then route), then acquisitions (route, then mic), so a mic is never opened on a
     * call that is about to be dropped.
     */
    fun plan(wantMic: Boolean, wantCall: Boolean, micHeld: Boolean, routed: Boolean): List<Step> = buildList {
        if (!wantMic && micHeld) add(Step.RELEASE_MIC)
        if (!wantCall && routed) add(Step.UNROUTE)
        if (wantCall && !routed) add(Step.ROUTE)
        if (wantMic && !micHeld) add(Step.OPEN_MIC)
    }
}

/**
 * The page's `reportSpeaking`, as a state machine: asserts only after a minimum run above the level,
 * releases after the hangover. `update` returns the new signal when it CHANGES, else null.
 */
class SpeakingDetector(
    private val level: Double = Protocol.SPEAKING_RMS,
    private val minRunMs: Long = Protocol.SPEAKING_MIN_RUN_MS,
    private val hangoverMs: Long = Protocol.SPEAKING_HANGOVER_MS,
) {
    var speaking = false
        private set
    // -1 = no run in progress. Not 0: a clock can legitimately read 0, and a 0 sentinel then
    // silently restarts the run on the next sample (the unit test caught exactly that).
    private var runStart = -1L
    private var lastLoud = -1L

    fun update(rms: Double, nowMs: Long): Boolean? {
        if (rms >= level) {
            if (runStart < 0) runStart = nowMs
            lastLoud = nowMs
            if (!speaking && nowMs - runStart >= minRunMs) {
                speaking = true
                return true
            }
            return null
        }
        runStart = -1L
        if (speaking && nowMs - lastLoud > hangoverMs) {
            speaking = false
            return false
        }
        return null
    }

    fun reset() { speaking = false; runStart = -1L; lastLoud = -1L }
}
