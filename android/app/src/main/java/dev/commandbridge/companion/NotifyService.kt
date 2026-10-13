package dev.commandbridge.companion

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import java.util.concurrent.TimeUnit

/**
 * Spec 036: the LIGHT connection, alive while the orb is off, that turns an agent's `say --notify`
 * into a notification. JJ: *"just as a notification, right? So that when I turn the orb on, I get to
 * hear it."* So it never plays, never marks anything played, and holds no microphone, no audio focus,
 * no wake lock and no keep-screen-on.
 *
 * WHY A FOREGROUND SERVICE: a plain background thread is killed minutes after he leaves the app, and
 * there is no cloud push here (no FCM, Conventions #6). Its type is `remoteMessaging`, Android 14's type
 * for relaying messages from another device, and its own notification is minimum importance (no sound,
 * no icon in the status bar). In Doze the network pauses between maintenance windows, so a heads-up can
 * arrive late while the phone lies untouched; nothing is lost, it arrives on the next window.
 */
class NotifyService : Service() {
    companion object {
        const val ACTION_STOP = "dev.commandbridge.companion.NOTIFY_STOP"
        const val EXTRA_LANE = "notice_lane"
        private const val TAG = "CBNotify"
        private const val CH_WATCH = "watch"
        private const val CH_NOTICES = "notices"
        private const val NOTIF_WATCH = 3
        private const val PREF_SEQ = "notify_seq"
        @Volatile var running = false
    }

    private val prefs by lazy { getSharedPreferences("cb", MODE_PRIVATE) }
    private val poll = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(Notices.WAIT_S + 20L, TimeUnit.SECONDS)
        .build()
    private var loop: Thread? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) { stop(); return START_NOT_STICKY }
        if (running) return START_NOT_STICKY
        createChannels()
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(NOTIF_WATCH, watching(), ServiceInfo.FOREGROUND_SERVICE_TYPE_REMOTE_MESSAGING)
        } else {
            startForeground(NOTIF_WATCH, watching())
        }
        running = true
        loop = Thread({ run() }, "cb-notify").apply { start() }
        // NOT sticky: a system restart arrives from the background, where Android 12+ may refuse
        // startForeground and crash the app. Opening the app starts it again (MainActivity.onResume).
        return START_NOT_STICKY
    }

    private fun run() {
        var backoff = 0L
        var pick: Pair<String, String>? = null   // kept while it works; re-picked only after a failure
        while (running) {
            val key = prefs.getString("device_key", null)
            if (pick == null) pick = Paths.pick(prefs.getString("lan", null)?.let { Protocol.lanOrigin(it) },
                                                prefs.getString("base", null), key)
            if (key == null || pick == null) { stopSelf(); running = false; return }
            val since = prefs.getInt(PREF_SEQ, 0)
            val ok = try {
                poll.newCall(Request.Builder()
                    .url("${pick.first}/notify/wait?since=$since&wait=${Notices.WAIT_S}")
                    .header(Protocol.DEVICE_HEADER, key).build()).execute().use { r ->
                    if (r.code != 200) false else {
                        val (items, seq) = Notices.parse(r.body?.string() ?: "{}")
                        for (n in items) show(n)
                        prefs.edit().putInt(PREF_SEQ, seq).apply()
                        true
                    }
                }
            } catch (e: Exception) { Log.w(TAG, "poll", e); false }
            if (ok) { backoff = 0; continue }
            // Failed: wait, then re-pick the path (home Wi-Fi may have come or gone).
            pick = null
            backoff = Notices.nextBackoff(backoff)
            try { Thread.sleep(backoff) } catch (_: InterruptedException) { return }
        }
    }

    private fun show(n: Notices.Notice) {
        val nm = getSystemService(NotificationManager::class.java)
        val id = Notices.notificationId(n.id)
        if (n.resolved) { nm.cancel(id); return }   // he heard it: the heads-up is done
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) return
        val open = PendingIntent.getActivity(this, id,
            Intent(this, MainActivity::class.java).putExtra(EXTRA_LANE, n.lane)
                .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val b = Notification.Builder(this, CH_NOTICES)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setVisibility(Notification.VISIBILITY_PRIVATE)
            .setContentTitle(n.lane.replaceFirstChar { it.uppercase() })
            .setContentText(n.text)
            .setStyle(Notification.BigTextStyle().bigText(n.text))
            .setAutoCancel(true)
            .setContentIntent(open)
        Notices.argb(n.color)?.let { b.setColor(it) }
        nm.notify(id, b.build())
    }

    private fun createChannels() {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CH_WATCH, "Watching for important messages",
            NotificationManager.IMPORTANCE_MIN))
        nm.createNotificationChannel(NotificationChannel(CH_NOTICES, "Important messages",
            NotificationManager.IMPORTANCE_HIGH))
    }

    private fun watching(): Notification {
        val stop = PendingIntent.getService(this, 0,
            Intent(this, NotifyService::class.java).setAction(ACTION_STOP), PendingIntent.FLAG_IMMUTABLE)
        return Notification.Builder(this, CH_WATCH)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentTitle("Command Bridge")
            .setContentText("Watching for important messages")
            .setOngoing(true)
            .addAction(Notification.Action.Builder(null, "Stop", stop).build())
            .build()
    }

    private fun stop() {
        running = false
        loop?.interrupt()
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    override fun onDestroy() {
        running = false
        loop?.interrupt()
        super.onDestroy()
    }
}
