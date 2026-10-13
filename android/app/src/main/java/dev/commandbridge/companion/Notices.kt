package dev.commandbridge.companion

import org.json.JSONObject

/**
 * Spec 036: the pure half of the orb-off heads-up — what `/notify/wait` returned, the poll's backoff,
 * and a lane colour — kept free of Android types so it is unit-tested on the JVM.
 */
object Notices {
    /** One change to a notice. `resolved` means he heard the clip: cancel its notification. */
    data class Notice(val id: Int, val seq: Int, val clip: String, val lane: String, val text: String,
                      val color: String?, val resolved: Boolean)

    /** The long poll's one wait, in seconds; the server caps it at 50. */
    const val WAIT_S = 50

    const val BACKOFF_MIN_MS = 1_000L
    const val BACKOFF_MAX_MS = 60_000L

    fun nextBackoff(current: Long): Long =
        if (current <= 0) BACKOFF_MIN_MS else minOf(BACKOFF_MAX_MS, current * 2)

    /** `{items:[…], seq}` → the changes, oldest first as the server sends them, and the latest seq. */
    fun parse(body: String): Pair<List<Notice>, Int> {
        val j = JSONObject(body)
        val arr = j.optJSONArray("items")
        val out = ArrayList<Notice>()
        if (arr != null) for (k in 0 until arr.length()) {
            val o = arr.optJSONObject(k) ?: continue
            out += Notice(
                id = o.optInt("id"), seq = o.optInt("seq"), clip = o.optString("clip"),
                lane = o.optString("lane", "agent"), text = o.optString("text"),
                color = o.optString("color").takeIf { it.isNotEmpty() && it != "null" },
                resolved = o.optBoolean("resolved"),
            )
        }
        return out to j.optInt("seq")
    }

    /** `#rrggbb` → an ARGB int, or null for anything else (a colour is cosmetic, never an error). */
    fun argb(hex: String?): Int? {
        val h = hex?.trim()?.removePrefix("#") ?: return null
        if (h.length != 6) return null
        val rgb = h.toLongOrNull(16) ?: return null
        return (0xFF000000L or rgb).toInt()
    }

    /** The Android notification id for a notice: stable, so a re-sent notice replaces, never stacks. */
    fun notificationId(noticeId: Int): Int = 1000 + noticeId

    /**
     * Spec 040: should a socket message become a "heard you" notification? Only a FINAL turn (the
     * server sends `turn` for those; partials are `partial`), only one ADDRESSED to an agent, only
     * with words in it, and only while the app is not in front (in front, the transcript shows it).
     */
    fun heardYou(type: String, addressed: Boolean, text: String, appInFront: Boolean): Boolean =
        type == "turn" && addressed && text.isNotBlank() && !appInFront
}
