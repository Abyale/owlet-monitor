package com.abyale.owletmonitor

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.app.AlarmManager
import android.app.PendingIntent
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import android.view.WindowManager
import android.webkit.JavascriptInterface
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast

/** Shows the monitor's page (served by the Python engine on 127.0.0.1) full screen. */
class MainActivity : Activity() {
    private lateinit var web: WebView
    private val main = Handler(Looper.getMainLooper())
    private val url get() = "http://127.0.0.1:${OwletApp.PORT}/"

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        MonitorService.start(this)

        web = WebView(this).apply {
            setBackgroundColor(Color.parseColor("#0B1416"))
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.mediaPlaybackRequiresUserGesture = false
            addJavascriptInterface(JsBridge(), "OwletAndroid")
            webViewClient = object : WebViewClient() {
                override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                    // The engine takes a few seconds to start: keep retrying.
                    if (request.isForMainFrame) main.postDelayed({ view.loadUrl(url) }, 1000)
                }
                override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                    val u = request.url
                    if (u.host == "127.0.0.1" || u.host == "localhost") {
                        if (u.path?.endsWith(".csv") == true) {   // CSV export -> open in browser to download
                            startActivity(Intent(Intent.ACTION_VIEW, u)); return true
                        }
                        return false
                    }
                    startActivity(Intent(Intent.ACTION_VIEW, u)); return true
                }
            }
        }
        setContentView(web)
        web.loadUrl(url)
        askPermissions()
    }

    private fun askPermissions() {
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }
        val prefs = OwletApp.prefs(this)
        val pm = getSystemService(PowerManager::class.java)
        if (!pm.isIgnoringBatteryOptimizations(packageName) && !prefs.getBoolean("asked_battery", false)) {
            prefs.edit().putBoolean("asked_battery", true).apply()
            main.postDelayed({ openBatterySettings() }, 1500)
        }
    }

    @SuppressLint("BatteryLife")
    private fun openBatterySettings() {
        val pm = getSystemService(PowerManager::class.java)
        try {
            if (!pm.isIgnoringBatteryOptimizations(packageName)) {
                startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")))
            } else {
                Toast.makeText(this, "Battery optimisation is already off for Owlet Monitor", Toast.LENGTH_LONG).show()
                startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
            }
        } catch (_: Throwable) {
            startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$packageName")))
        }
    }

    private fun restartApp() {
        val pi = PendingIntent.getActivity(
            this, 99, Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_CANCEL_CURRENT
        )
        getSystemService(AlarmManager::class.java).set(AlarmManager.RTC, System.currentTimeMillis() + 600, pi)
        AlarmBridge.stopAll()
        main.postDelayed({ android.os.Process.killProcess(android.os.Process.myPid()) }, 200)
    }

    override fun onResume() {
        super.onResume()
        MonitorService.start(this)
    }

    @Deprecated("Back button: keep the app running in the background")
    override fun onBackPressed() {
        if (web.canGoBack()) web.goBack() else moveTaskToBack(true)
    }

    inner class JsBridge {
        @JavascriptInterface fun isApp(): Boolean = true

        @JavascriptInterface fun keepScreenOn(on: Boolean) = runOnUiThread {
            if (on) window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            else window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        }

        @JavascriptInterface fun setDemo(on: Boolean) {
            OwletApp.prefs(this@MainActivity).edit().putBoolean("demo", on).commit()
            runOnUiThread { restartApp() }
        }

        @JavascriptInterface fun openBatterySettings() = runOnUiThread { this@MainActivity.openBatterySettings() }

        @JavascriptInterface fun openNotificationSettings() = runOnUiThread {
            startActivity(Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                .putExtra(Settings.EXTRA_APP_PACKAGE, packageName))
        }
    }
}
