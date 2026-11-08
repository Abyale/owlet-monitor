package com.abyale.owletmonitor

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.content.SharedPreferences

class OwletApp : Application() {
    companion object {
        const val PORT = 8765
        const val CH_SERVICE = "monitor_service"
        const val CH_CRITICAL = "alarm_critical"
        const val CH_WARNING = "alarm_warning"
        const val CH_INFO = "alarm_info"

        fun prefs(ctx: Context): SharedPreferences =
            ctx.getSharedPreferences("owlet", Context.MODE_PRIVATE)
    }

    override fun onCreate() {
        super.onCreate()
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CH_SERVICE, "Monitoring running", NotificationManager.IMPORTANCE_LOW).apply {
                description = "Shown while Owlet Monitor is watching the sock"
                setShowBadge(false)
            }
        )
        // Critical alarms: the app plays a looping alarm sound itself (alarm volume), so the channel is silent.
        nm.createNotificationChannel(
            NotificationChannel(CH_CRITICAL, "Critical alarms", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "Low oxygen, heart rate and Owlet's own alerts"
                setSound(null, null)
                enableVibration(false)
                setBypassDnd(true)
                lockscreenVisibility = android.app.Notification.VISIBILITY_PUBLIC
            }
        )
        nm.createNotificationChannel(
            NotificationChannel(CH_WARNING, "Warnings", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "Sock off, no data, base offline"
                lockscreenVisibility = android.app.Notification.VISIBILITY_PUBLIC
            }
        )
        nm.createNotificationChannel(
            NotificationChannel(CH_INFO, "Information", NotificationManager.IMPORTANCE_DEFAULT).apply {
                description = "Low battery and other notices"
            }
        )
        AlarmBridge.init(this)
    }
}
