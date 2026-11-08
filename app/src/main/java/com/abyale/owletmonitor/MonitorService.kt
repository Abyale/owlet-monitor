package com.abyale.owletmonitor

import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import androidx.core.app.NotificationCompat
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import kotlin.concurrent.thread

/** Keeps the Python monitor running in the background with a permanent notification. */
class MonitorService : Service() {
    companion object {
        private const val TAG = "OwletMonitor"
        const val ACTION_QUIT = "com.abyale.owletmonitor.QUIT"
        @Volatile var pythonStarted = false
        @Volatile var lastError: String? = null

        fun start(ctx: Context) {
            ctx.startForegroundService(Intent(ctx, MonitorService::class.java))
        }
    }

    private var wakeLock: PowerManager.WakeLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        AlarmBridge.init(applicationContext)
        goForeground()
        val pm = getSystemService(PowerManager::class.java)
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "OwletMonitor::monitor").apply {
            setReferenceCounted(false)
            acquire()
        }
        startPython()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_QUIT) {
            AlarmBridge.stopAll()
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf()
            android.os.Process.killProcess(android.os.Process.myPid())
            return START_NOT_STICKY
        }
        return START_STICKY
    }

    override fun onDestroy() {
        wakeLock?.let { if (it.isHeld) it.release() }
        super.onDestroy()
    }

    private fun goForeground() {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val quit = PendingIntent.getService(
            this, 1, Intent(this, MonitorService::class.java).setAction(ACTION_QUIT),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val demo = OwletApp.prefs(this).getBoolean("demo", false)
        val n = NotificationCompat.Builder(this, OwletApp.CH_SERVICE)
            .setSmallIcon(R.drawable.ic_stat)
            .setContentTitle(if (demo) "Owlet Monitor (demo) is running" else "Owlet Monitor is watching")
            .setContentText("Tap to open. Alarms work with the screen off.")
            .setOngoing(true)
            .setContentIntent(open)
            .addAction(0, "Quit", quit)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(1, n)
        }
    }

    private fun startPython() {
        synchronized(MonitorService::class.java) {
            if (pythonStarted) return
            pythonStarted = true
        }
        val demo = OwletApp.prefs(this).getBoolean("demo", false)
        val dir = filesDir.absolutePath
        thread(name = "owlet-python", isDaemon = true) {
            try {
                if (!Python.isStarted()) Python.start(AndroidPlatform(applicationContext))
                Python.getInstance().getModule("android_main")
                    .callAttr("run", dir, OwletApp.PORT, demo)
            } catch (t: Throwable) {
                Log.e(TAG, "Python monitor stopped", t)
                lastError = t.toString()
            } finally {
                pythonStarted = false
            }
        }
    }
}
