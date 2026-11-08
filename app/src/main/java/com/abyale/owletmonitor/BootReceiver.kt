package com.abyale.owletmonitor

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Starts monitoring again after the phone restarts or the app is updated. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val ok = intent.action == Intent.ACTION_BOOT_COMPLETED || intent.action == Intent.ACTION_MY_PACKAGE_REPLACED
        if (ok && OwletApp.prefs(context).getBoolean("auto_start", true)) {
            try { MonitorService.start(context) } catch (_: Throwable) {}
        }
    }
}
