# Owlet Monitor

A personal Android app for the Owlet sock: live oxygen and heart rate, rolling averages,
configurable alarms (thresholds, delays, alarm types, active hours), and full history stored on the phone.

> Unofficial hobby project using Owlet's cloud through the open-source
> [pyowletapi](https://github.com/ryanbdclark/pyowletapi) library. **Not a medical device.**
> Keep the Owlet base station and official app alarms switched on.

## Install on your phone

1. Open the **Releases** page of this repository (or the latest **Actions** run).
2. Download `OwletMonitor-<number>.apk` and open it. Allow "install unknown apps" for your browser when asked.
3. Open **Owlet Monitor**, allow notifications, and turn **battery optimisation off** when asked.
4. Sign in with your Owlet account.

New builds are signed with the same key, so they install as updates and keep your settings and history.

## How it works

- `app/src/main/python/owlet_monitor.py`: the monitor engine (polling, averaging, alarms, history, web UI).
  The same file runs on its own in Termux: `python owlet_monitor.py` (or `--demo`).
- `app/src/main/python/android_main.py`: starts the engine inside the app and connects alarms to Android.
- `MonitorService.kt`: foreground service that keeps monitoring running with the screen off.
- `AlarmBridge.kt`: alarm notifications (full-screen for critical), looping alarm sound on the ALARM volume,
  vibration, spoken alarm.
- `MainActivity.kt`: shows the monitor page.

Python is bundled with [Chaquopy](https://chaquo.com/chaquopy/). Every push to `main` builds a new APK with GitHub Actions.
