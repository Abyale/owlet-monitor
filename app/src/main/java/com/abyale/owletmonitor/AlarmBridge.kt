package com.abyale.owletmonitor

import android.Manifest
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.media.AudioAttributes
import android.media.AudioManager
import android.media.Ringtone
import android.media.RingtoneManager
import android.os.Handler
import android.os.Looper
import android.os.VibrationEffect
import android.os.Vibrator
import android.speech.tts.TextToSpeech
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat

/**
 * Called from Python (android_main.py). Shows alarm notifications, plays the alarm sound on the
 * phone's ALARM volume, vibrates and speaks the alarm.
 */
object AlarmBridge {
    private const val TAG = "OwletAlarm"
    private lateinit var ctx: Context
    private val main = Handler(Looper.getMainLooper())
    private var tts: TextToSpeech? = null
    private var ttsReady = false
    private var ringtone: Ringtone? = null
    private val activeCritical = mutableSetOf<String>()

    private val alarmAttrs: AudioAttributes = AudioAttributes.Builder()
        .setUsage(AudioAttributes.USAGE_ALARM)
        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
        .build()

    fun init(c: Context) {
        if (::ctx.isInitialized) return
        ctx = c.applicationContext
        main.post {
            tts = TextToSpeech(ctx) { status ->
                ttsReady = status == TextToSpeech.SUCCESS
                if (ttsReady) tts?.setAudioAttributes(alarmAttrs)
            }
        }
    }

    private fun vibrator(): Vibrator? = ctx.getSystemService(Vibrator::class.java)

    @JvmStatic
    fun fire(key: String, label: String, message: String, level: String,
             notify: Boolean, vibrate: Boolean, speak: Boolean, maxVolume: Boolean) {
        main.post {
            try {
                if (notify) showNotification(key, label, message, level)
                val isTest = key.startsWith("test_")
                when (level) {
                    "critical" -> {
                        if (maxVolume) setAlarmVolumeMax()
                        if (!isTest) activeCritical.add(key)
                        startRingtone()
                        if (vibrate) vibrator()?.vibrate(
                            VibrationEffect.createWaveform(longArrayOf(0, 800, 400, 800, 400), if (isTest) -1 else 0),
                            alarmAttrs)
                        if (isTest) main.postDelayed({ if (activeCritical.isEmpty()) stopRingtone() }, 5000)
                    }
                    "warning" -> {
                        if (vibrate) vibrator()?.vibrate(
                            VibrationEffect.createWaveform(longArrayOf(0, 500, 300, 500), -1), alarmAttrs)
                    }
                }
                if (speak && level != "notify" && ttsReady) {
                    tts?.speak(label, TextToSpeech.QUEUE_FLUSH, null, key)
                }
            } catch (t: Throwable) {
                Log.e(TAG, "fire failed", t)
            }
        }
    }

    /** Called every few seconds while a critical alarm is unacknowledged. */
    @JvmStatic
    fun repeat(level: String, vibrate: Boolean) {
        main.post {
            if (level == "critical" && activeCritical.isNotEmpty()) {
                if (ringtone?.isPlaying != true) startRingtone()
            }
        }
    }

    @JvmStatic
    fun clear(key: String) {
        main.post {
            try {
                NotificationManagerCompat.from(ctx).cancel(key.hashCode())
                activeCritical.remove(key)
                if (activeCritical.isEmpty()) {
                    stopRingtone()
                    vibrator()?.cancel()
                    tts?.stop()
                }
            } catch (t: Throwable) {
                Log.e(TAG, "clear failed", t)
            }
        }
    }

    fun stopAll() {
        activeCritical.clear()
        stopRingtone()
        vibrator()?.cancel()
    }

    private fun showNotification(key: String, label: String, message: String, level: String) {
        if (android.os.Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(ctx, Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED) {
            Log.w(TAG, "notification permission not granted")
            return
        }
        val channel = when (level) {
            "critical" -> OwletApp.CH_CRITICAL
            "warning" -> OwletApp.CH_WARNING
            else -> OwletApp.CH_INFO
        }
        val open = PendingIntent.getActivity(
            ctx, 10, Intent(ctx, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        fun action(kind: String, code: Int): PendingIntent = PendingIntent.getBroadcast(
            ctx, key.hashCode() * 4 + code,
            Intent(ctx, ActionReceiver::class.java).putExtra("key", key).putExtra("kind", kind),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val b = NotificationCompat.Builder(ctx, channel)
            .setSmallIcon(R.drawable.ic_stat)
            .setContentTitle("Owlet: $label")
            .setContentText(message)
            .setStyle(NotificationCompat.BigTextStyle().bigText(message))
            .setContentIntent(open)
            .setAutoCancel(level == "notify")
            .addAction(0, "Acknowledge", action("ack", 1))
            .addAction(0, "Snooze 10 min", action("snooze", 2))
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
        when (level) {
            "critical" -> b.setPriority(NotificationCompat.PRIORITY_MAX)
                .setCategory(NotificationCompat.CATEGORY_ALARM)
                .setOngoing(true)
                .setColor(0xFFFF4D4F.toInt())
                .setFullScreenIntent(open, true)
            "warning" -> b.setPriority(NotificationCompat.PRIORITY_HIGH).setColor(0xFFF2B33D.toInt())
            else -> b.setPriority(NotificationCompat.PRIORITY_DEFAULT)
        }
        NotificationManagerCompat.from(ctx).notify(key.hashCode(), b.build())
    }

    private fun setAlarmVolumeMax() {
        try {
            val am = ctx.getSystemService(AudioManager::class.java)
            am.setStreamVolume(AudioManager.STREAM_ALARM, am.getStreamMaxVolume(AudioManager.STREAM_ALARM), 0)
        } catch (t: Throwable) {
            Log.w(TAG, "could not raise alarm volume", t)
        }
    }

    private fun startRingtone() {
        try {
            if (ringtone?.isPlaying == true) return
            val uri = RingtoneManager.getActualDefaultRingtoneUri(ctx, RingtoneManager.TYPE_ALARM)
                ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)
                ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION)
            ringtone = RingtoneManager.getRingtone(ctx, uri)?.apply {
                audioAttributes = alarmAttrs
                if (android.os.Build.VERSION.SDK_INT >= 28) isLooping = true
                play()
            }
        } catch (t: Throwable) {
            Log.e(TAG, "ringtone failed", t)
        }
    }

    private fun stopRingtone() {
        try { ringtone?.stop() } catch (_: Throwable) {}
        ringtone = null
    }
}
