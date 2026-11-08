package com.abyale.owletmonitor

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import java.net.HttpURLConnection
import java.net.URL
import kotlin.concurrent.thread

/** Acknowledge / Snooze buttons on alarm notifications. */
class ActionReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val key = intent.getStringExtra("key") ?: return
        val kind = intent.getStringExtra("kind") ?: "ack"
        AlarmBridge.init(context)
        AlarmBridge.clear(key)  // silence immediately
        val pending = goAsync()
        thread {
            try {
                val path = if (kind == "snooze") "/api/snooze/$key?minutes=10" else "/api/ack/$key"
                val c = URL("http://127.0.0.1:${OwletApp.PORT}$path").openConnection() as HttpURLConnection
                c.requestMethod = "POST"
                c.connectTimeout = 3000
                c.readTimeout = 3000
                c.doOutput = true
                c.outputStream.use { it.write("{}".toByteArray()) }
                c.responseCode
                c.disconnect()
            } catch (_: Throwable) {
            } finally {
                pending.finish()
            }
        }
    }
}
