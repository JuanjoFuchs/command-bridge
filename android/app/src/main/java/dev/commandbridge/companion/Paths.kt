package dev.commandbridge.companion

import okhttp3.OkHttpClient
import okhttp3.Request
import java.util.concurrent.TimeUnit

/**
 * Spec 034 FR5: is the home Wi-Fi path usable right now? One authenticated `/status` with a short
 * timeout: on another network the private address simply doesn't answer, and 1.5 s is the most he
 * waits before the tunnel is used instead. Blocking: call it off the main thread.
 */
object Paths {
    private const val PROBE_TIMEOUT_MS = 1500L

    private val probe = OkHttpClient.Builder()
        .connectTimeout(PROBE_TIMEOUT_MS, TimeUnit.MILLISECONDS)
        .readTimeout(PROBE_TIMEOUT_MS, TimeUnit.MILLISECONDS)
        .callTimeout(PROBE_TIMEOUT_MS * 2, TimeUnit.MILLISECONDS)
        .build()

    fun lanAnswers(lan: String?, deviceKey: String?): Boolean {
        if (lan == null || deviceKey == null || Protocol.lanOrigin(lan) == null) return false
        return try {
            probe.newCall(Request.Builder().url("$lan/status").header(Protocol.DEVICE_HEADER, deviceKey).build())
                .execute().use { it.code == 200 }
        } catch (_: Exception) { false }
    }

    /** (base, "home" | "tunnel") for this moment, probing the LAN first. */
    fun pick(lan: String?, tunnel: String?, deviceKey: String?): Pair<String, String>? =
        Protocol.choosePath(lan, tunnel, lanAnswers(lan, deviceKey))
}
