package dev.commandbridge.companion

import org.json.JSONArray
import org.json.JSONObject

/**
 * Spec 044: the app as a dumb wrapper. These are the primitives the SERVER drives — what Android
 * forces to be native — and the wire format for them. No decision is made here: WHEN to hold the mic
 * or the call, which output to use, what to notify, all live in the server (command_bridge/native.py),
 * so changing them ships without an APK. Pure, so it is unit-tested on the JVM.
 */
object Primitives {
    /** The primitive protocol this app speaks. Bump only when an op's meaning changes. */
    const val PROTOCOL = 1

    /** The ops this app can carry out. The server never sends one that is not listed. */
    val OPS = listOf("audio", "notify", "notify_cancel", "awake")

    sealed class Directive {
        /** Hold the mic or not, call mode or not, on this output type (null = leave the route). */
        data class Audio(val mic: Boolean, val call: Boolean, val device: Int?) : Directive()
        /** One notification per key; `quiet` peeks without sound, `alert` rings. */
        data class Notify(val key: String, val quiet: Boolean, val title: String, val text: String) : Directive()
        data class NotifyCancel(val key: String) : Directive()
        /** Keep the screen on while the app is in front. */
        data class Awake(val on: Boolean) : Directive()
    }

    /** The `native` block of the app's `hello`. */
    fun hello(app: String): JSONObject =
        JSONObject().put("protocol", PROTOCOL).put("ops", JSONArray(OPS)).put("app", app)

    /** Does the server drive us? Only when its `hello_ack` answers with a protocol. */
    fun driven(ack: JSONObject): Boolean = (ack.optJSONObject("native")?.optInt("protocol", 0) ?: 0) >= 1

    /** A `{"type":"native","op":…}` message → a directive; null for anything else or an unknown op. */
    fun parse(msg: JSONObject): Directive? {
        if (msg.optString("type") != "native") return null
        return when (msg.optString("op")) {
            "audio" -> Directive.Audio(
                mic = msg.optBoolean("mic"), call = msg.optBoolean("call"),
                device = if (msg.has("device") && !msg.isNull("device")) msg.optInt("device") else null)
            "notify" -> {
                val key = msg.optString("key")
                if (key.isEmpty()) null
                else Directive.Notify(key, msg.optString("style") == "quiet",
                    msg.optString("title"), msg.optString("text"))
            }
            "notify_cancel" -> msg.optString("key").takeIf { it.isNotEmpty() }?.let { Directive.NotifyCancel(it) }
            "awake" -> Directive.Awake(msg.optBoolean("on"))
            else -> null
        }
    }

    /**
     * A stable Android notification id per key, so a repeat replaces rather than stacks. The two
     * keys the server uses today keep the ids the app always used (2 spoken, 4 heard).
     */
    fun notificationId(key: String): Int = when (key) {
        "speak" -> 2
        "heard" -> 4
        else -> 3000 + (key.hashCode() and 0xfff)
    }

    /** What the app holds now (FR2). Sent only when it differs from the last one sent. */
    data class Report(
        val foreground: Boolean, val devices: List<Int>, val mic: Boolean, val call: Boolean,
        val device: Int?, val micError: String?,
    ) {
        fun toJson(): JSONObject = JSONObject().put("type", "native_state").put("foreground", foreground)
            .put("devices", JSONArray(devices)).put("mic", mic).put("call", call)
            .put("device", device ?: JSONObject.NULL).put("mic_error", micError ?: JSONObject.NULL)
    }
}
