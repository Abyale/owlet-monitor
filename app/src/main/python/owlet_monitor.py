#!/usr/bin/env python3
"""
Owlet Monitor - a personal dashboard + alarm app for the Owlet sock, made to run
in Termux on Android (also works on any PC with Python 3.10+).

Start:      python owlet_monitor.py            then open http://localhost:8080
Demo mode:  python owlet_monitor.py --demo     (fake data, no account needed)
Options:    --port 8080   --host 127.0.0.1 (use 0.0.0.0 to view from other devices on your Wi-Fi)

Needs:      pip install pyowletapi
Optional:   Termux:API app + `pkg install termux-api` for alarms when the browser is closed
            (notifications, vibration, spoken alarm on the ALARM volume channel).

IMPORTANT: this is an unofficial hobby app built on an unofficial API. It is NOT a medical
device. Keep the Owlet base station / official app alarms switched on.
"""

import argparse
import asyncio
import copy
import json
import math
import os
import random
import shutil
import sqlite3
import sys
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs

APP_VERSION = "1.1"
APP_DIR = Path(os.environ.get("OWLET_HOME") or str(Path.home() / ".owlet_monitor"))
SETTINGS_FILE = APP_DIR / "settings.json"
CREDS_FILE = APP_DIR / "account.json"
EVENTS_FILE = APP_DIR / "events.jsonl"
HISTORY_DB = APP_DIR / "history.db"

# --------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------

DEFAULT_ALARMS = {
    "low_oxygen": {
        "label": "Low oxygen", "enabled": True, "threshold": 88, "unit": "%", "delay": 10,
        "level": "critical", "sound": "siren", "schedule": "",
        "help": "Oxygen below the threshold for longer than the delay.",
    },
    "high_heart_rate": {
        "label": "High heart rate", "enabled": True, "threshold": 200, "unit": "bpm", "delay": 15,
        "level": "critical", "sound": "siren", "schedule": "",
        "help": "Heart rate above the threshold for longer than the delay.",
    },
    "low_heart_rate": {
        "label": "Low heart rate", "enabled": True, "threshold": 80, "unit": "bpm", "delay": 15,
        "level": "critical", "sound": "siren", "schedule": "",
        "help": "Heart rate below the threshold for longer than the delay.",
    },
    "owlet_alerts": {
        "label": "Owlet's own alerts", "enabled": True, "threshold": None, "unit": "", "delay": 0,
        "level": "critical", "sound": "siren", "schedule": "",
        "help": "Repeats any oxygen / heart-rate / power alert raised by the base station itself.",
    },
    "sock_off": {
        "label": "Sock off or disconnected", "enabled": True, "threshold": None, "unit": "", "delay": 30,
        "level": "warning", "sound": "beep", "schedule": "",
        "help": "Sock fell off or lost contact with the base while monitoring (ignored while charging).",
    },
    "no_data": {
        "label": "No new data", "enabled": True, "threshold": 60, "unit": "s", "delay": 0,
        "level": "warning", "sound": "beep", "schedule": "",
        "help": "No new reading for this many seconds (base offline, Wi-Fi or phone internet down).",
    },
    "base_offline": {
        "label": "Base offline / power lost", "enabled": True, "threshold": None, "unit": "", "delay": 30,
        "level": "warning", "sound": "beep", "schedule": "",
        "help": "Owlet's cloud reports the base offline, or the base lost power.",
    },
    "low_battery": {
        "label": "Low sock battery", "enabled": True, "threshold": 20, "unit": "%", "delay": 0,
        "level": "notify", "sound": "chime", "schedule": "",
        "help": "Sock battery below the threshold (ignored while charging).",
    },
}

DEFAULT_SETTINGS = {
    "region": "europe",
    "poll_seconds": 1.0,          # how often we ask the cloud for the latest vitals
    "full_refresh_seconds": 15,   # full property refresh (alert flags etc.)
    "device_refresh_seconds": 60, # device list refresh (online/offline status)
    "activate_seconds": 20,       # how often we tell the base "an app is watching" (APP_ACTIVE)
    "avg_window": 5,              # number of distinct readings in the rolling average
    "alarms_use_average": True,
    "only_while_monitoring": True,  # suppress vitals/sock alarms while the base is switched off
    "history_days": 0,            # 0 = keep every reading forever
    "repeat_seconds": 6,          # how often a critical alarm repeats until acknowledged
    "phone": {"notification": True, "vibrate": True, "speak": True, "max_volume": True},
    "alarms": DEFAULT_ALARMS,
}

# Values published by Owlet for its own base-station alerts (Dream Sock):
# red below 80% oxygen, below 50 or above 220 bpm; yellow after 60 s sock off / out of range.
OWLET_PRESET = {
    "low_oxygen": {"enabled": True, "threshold": 80, "delay": 0, "level": "critical"},
    "high_heart_rate": {"enabled": True, "threshold": 220, "delay": 0, "level": "critical"},
    "low_heart_rate": {"enabled": True, "threshold": 50, "delay": 0, "level": "critical"},
    "owlet_alerts": {"enabled": True, "delay": 0, "level": "critical"},
    "sock_off": {"enabled": True, "delay": 60, "level": "warning"},
    "no_data": {"enabled": True, "threshold": 60, "level": "warning"},
    "base_offline": {"enabled": True, "delay": 60, "level": "warning"},
    "low_battery": {"enabled": True, "threshold": 10, "level": "notify"},
}

LEVELS = ("critical", "warning", "notify")
SOUNDS = ("siren", "beep", "chime", "none")


def deep_merge(base, override):
    out = copy.deepcopy(base)
    if not isinstance(override, dict):
        return out
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _num(v, lo, hi, default):
    try:
        f = float(v)
        if math.isnan(f):
            return default
        return max(lo, min(hi, f))
    except (TypeError, ValueError):
        return default


def local_dt(ts):
    """Local wall-clock time for a unix timestamp (the Android app overrides this)."""
    return datetime.fromtimestamp(ts)


def local_now():
    return local_dt(time.time())


def parse_schedule(text):
    """'' -> always. '20:00-07:30' -> (1200, 450) minutes. Returns None if invalid/always."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        a, b = text.split("-")
        ha, ma = [int(x) for x in a.strip().split(":")]
        hb, mb = [int(x) for x in b.strip().split(":")]
        if not (0 <= ha < 24 and 0 <= hb < 24 and 0 <= ma < 60 and 0 <= mb < 60):
            return None
        return ha * 60 + ma, hb * 60 + mb
    except Exception:
        return None


def in_schedule(text, now=None):
    sch = parse_schedule(text)
    if sch is None:
        return True
    now = now or local_now()
    m = now.hour * 60 + now.minute
    start, end = sch
    if start == end:
        return True
    if start < end:
        return start <= m < end
    return m >= start or m < end  # crosses midnight


def sanitize_settings(s):
    s = deep_merge(DEFAULT_SETTINGS, s)
    d = DEFAULT_SETTINGS
    s["region"] = s["region"] if s["region"] in ("europe", "world") else "europe"
    s["poll_seconds"] = _num(s["poll_seconds"], 1, 60, d["poll_seconds"])
    s["full_refresh_seconds"] = _num(s["full_refresh_seconds"], 5, 600, d["full_refresh_seconds"])
    s["device_refresh_seconds"] = _num(s["device_refresh_seconds"], 20, 1800, d["device_refresh_seconds"])
    s["activate_seconds"] = _num(s["activate_seconds"], 5, 120, d["activate_seconds"])
    s["avg_window"] = int(_num(s["avg_window"], 1, 60, d["avg_window"]))
    s["history_days"] = int(_num(s["history_days"], 0, 3650, d["history_days"]))
    s.pop("history_minutes", None)
    s["repeat_seconds"] = _num(s["repeat_seconds"], 3, 120, d["repeat_seconds"])
    s["alarms_use_average"] = bool(s["alarms_use_average"])
    s["only_while_monitoring"] = bool(s["only_while_monitoring"])
    for k in list(s["phone"].keys()):
        s["phone"][k] = bool(s["phone"][k])
    alarms = {}
    for key, default in DEFAULT_ALARMS.items():
        a = deep_merge(default, s["alarms"].get(key, {}))
        # label/help/unit always come from the code, not the file
        a["label"], a["help"], a["unit"] = default["label"], default["help"], default["unit"]
        a["enabled"] = bool(a["enabled"])
        if default["threshold"] is None:
            a["threshold"] = None
        else:
            a["threshold"] = _num(a["threshold"], 0, 400, default["threshold"])
        a["delay"] = _num(a["delay"], 0, 3600, default["delay"])
        a["level"] = a["level"] if a["level"] in LEVELS else default["level"]
        a["sound"] = a["sound"] if a["sound"] in SOUNDS else default["sound"]
        a["schedule"] = str(a.get("schedule") or "").strip()
        if a["schedule"] and parse_schedule(a["schedule"]) is None:
            a["schedule"] = ""
        alarms[key] = a
    s["alarms"] = alarms
    return s


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save_json(path, data, private=False):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    if private:
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    tmp.replace(path)


# --------------------------------------------------------------------------------------
# Data sources
# --------------------------------------------------------------------------------------

def _to_float(v):
    try:
        if v is None or isinstance(v, bool):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


class SourceError(Exception):
    pass


class NeedsLogin(Exception):
    pass


class OwletSource:
    """Talks to Owlet's cloud through pyowletapi."""

    demo = False

    def __init__(self, settings, creds):
        self.settings = settings
        self.creds = creds or {}
        self.api = None
        self.sock = None
        self.device = {}
        self.fast_ok = True  # single-property fast path works
        self._last_tokens = None

    async def connect(self):
        try:
            from pyowletapi.api import OwletAPI
            from pyowletapi.sock import Sock
        except ImportError as e:
            raise SourceError("pyowletapi is not installed. Run: pip install pyowletapi") from e
        self._Sock = Sock
        user = self.creds.get("user") or os.environ.get("OWLET_USER")
        password = self.creds.get("password") or os.environ.get("OWLET_PASS")
        tokens = self.creds.get("tokens") or {}
        if not (user and password) and not tokens.get("refresh"):
            raise NeedsLogin()
        region = self.creds.get("region") or self.settings["region"]
        kwargs = {}
        if tokens.get("api_token"):
            kwargs = dict(token=tokens.get("api_token"), expiry=tokens.get("expiry"),
                          refresh=tokens.get("refresh"))
        self.api = OwletAPI(region, user, password, **kwargs)
        try:
            await self.api.authenticate()
        except Exception:
            if kwargs and user and password:
                # saved tokens rejected -> clean login
                await self._close_api()
                self.api = OwletAPI(region, user, password)
                await self.api.authenticate()
            else:
                raise
        self._save_tokens()
        await self.refresh_devices(first=True)

    async def _close_api(self):
        if self.api:
            try:
                await self.api.close()
            except Exception:
                pass
        self.api = None

    async def close(self):
        await self._close_api()

    def _save_tokens(self):
        try:
            tokens = dict(self.api.tokens)
        except Exception:
            return
        if tokens and tokens != self._last_tokens:
            self._last_tokens = tokens
            self.creds["tokens"] = tokens
            if self.creds.get("user"):
                save_json(CREDS_FILE, self.creds, private=True)

    async def refresh_devices(self, first=False):
        resp = await self.api.get_devices()
        devices = resp.get("response", [])
        if not devices:
            raise SourceError("No Owlet socks found on this account")
        wanted = self.creds.get("dsn")
        chosen = None
        for d in devices:
            dev = d.get("device", d)
            if not wanted or dev.get("dsn") == wanted:
                chosen = dev
                break
        chosen = chosen or devices[0].get("device", devices[0])
        if first or self.sock is None or self.sock.serial != chosen.get("dsn"):
            self.sock = self._Sock(self.api, chosen)
        self.device = {
            "name": chosen.get("product_name") or chosen.get("name") or "Owlet",
            "serial": chosen.get("dsn"),
            "connection_status": chosen.get("connection_status"),
            "lan_ip": chosen.get("lan_ip"),
            "count": len(devices),
        }
        self._save_tokens()

    async def full_refresh(self):
        """Full property read (vitals + alert flags). Also sends APP_ACTIVE."""
        await self.sock.update_properties()
        self._save_tokens()
        raw = self.sock.raw_properties or {}
        return self._snapshot(raw)

    async def activate(self):
        await self.api.activate(self.sock.serial)

    async def fast_refresh(self):
        """Read only REAL_TIME_VITALS (1 small request) and re-use the library's parser."""
        if not self.fast_ok:
            return await self.full_refresh()
        try:
            resp = await self.api._request(
                "GET", f"/dsns/{self.sock.serial}/properties/REAL_TIME_VITALS.json")
            prop = resp.get("property", resp) if isinstance(resp, dict) else None
            if not prop or "value" not in prop:
                raise SourceError("unexpected REAL_TIME_VITALS response")
            raw = dict(self.sock.raw_properties or {})
            raw["REAL_TIME_VITALS"] = prop
            self.sock._raw_properties = raw
            self.sock._properties = await self.sock._normalise_properties()
            self._save_tokens()
            return self._snapshot(raw)
        except SourceError:
            self.fast_ok = False
            return await self.full_refresh()
        except AttributeError:
            # library internals changed -> fall back permanently to the public method
            self.fast_ok = False
            return await self.full_refresh()

    def _snapshot(self, raw):
        props = dict(self.sock.properties or {})
        rtv = raw.get("REAL_TIME_VITALS") or {}
        key = f'{rtv.get("data_updated_at")}|{rtv.get("value")}'
        return {"props": props, "reading_key": key, "device": dict(self.device)}


class DemoSource:
    """Fake sock for trying the app and testing alarms without a baby or an account."""

    demo = True

    def __init__(self, settings, creds=None):
        self.settings = settings
        self.device = {"name": "Demo sock", "serial": "DEMO0000", "connection_status": "Online",
                       "lan_ip": "192.168.1.22", "count": 1}
        self.scenario = "normal"
        self.scenario_until = 0
        self.upload_every = 3.0
        self._last_upload = 0
        self._n = 0
        self._ox = 97.5
        self._hr = 122.0
        self._props = {}
        self._battery = 98.0
        self._t0 = time.time()

    async def connect(self):
        self._make_reading()

    async def close(self):
        pass

    async def refresh_devices(self, first=False):
        self.device["connection_status"] = "Offline" if self.scenario == "base_offline" else "Online"

    async def activate(self):
        pass

    def set_scenario(self, name, seconds=90):
        self.scenario = name
        self.scenario_until = time.time() + seconds

    def _make_reading(self):
        self._n += 1
        sc = self.scenario
        self._ox += (97.5 - self._ox) * 0.3 + random.uniform(-0.8, 0.8)
        self._hr += (122 - self._hr) * 0.2 + random.uniform(-5, 5)
        ox, hr = self._ox, self._hr
        if sc == "low_oxygen":
            ox = random.uniform(82, 86)
        elif sc == "high_heart_rate":
            hr = random.uniform(210, 225)
        elif sc == "low_heart_rate":
            hr = random.uniform(65, 75)
        self._battery = max(5.0, self._battery - 0.01)
        sock_off = sc == "sock_off"
        self._props = {
            "oxygen_saturation": 0.0 if sock_off else round(ox),
            "heart_rate": 0.0 if sock_off else round(hr),
            "oxygen_10_av": 97.0,
            "battery_percentage": 12.0 if sc == "low_battery" else round(self._battery),
            "battery_minutes": 1100.0,
            "signal_strength": 50.0,
            "skin_temperature": 36,
            "movement": random.choice([0, 0, 0, 1, 2]),
            "sock_connection": 0 if sock_off else 2,
            "sock_off": sock_off,
            "sock_disconnected": False,
            "base_station_on": 1,
            "charging": 0,
            "high_oxygen_alert": False,
            "low_oxygen_alert": sc == "owlet_alert",
            "high_heart_rate_alert": False,
            "low_heart_rate_alert": False,
            "low_battery_alert": False,
            "lost_power_alert": False,
            "sleep_state": 15,
            "firmware_update_available": False,
            "last_updated": datetime.now(timezone.utc).strftime("%Y/%m/%d %H:%M:%S"),
        }

    async def _read(self):
        now = time.time()
        if self.scenario != "normal" and now > self.scenario_until:
            self.scenario = "normal"
        if self.scenario not in ("stale", "base_offline") and now - self._last_upload >= self.upload_every:
            self._last_upload = now
            self._make_reading()
        await asyncio.sleep(0.05)
        return {"props": dict(self._props), "reading_key": str(self._n), "device": dict(self.device)}

    async def full_refresh(self):
        return await self._read()

    async def fast_refresh(self):
        return await self._read()


# --------------------------------------------------------------------------------------
# Phone alarms (Termux:API)
# --------------------------------------------------------------------------------------

class PhoneAlarms:
    def __init__(self, port):
        self.port = port
        self.available = shutil.which("termux-notification") is not None
        self._volume_set = False

    async def _run(self, *cmd, timeout=12):
        if not self.available:
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return out.decode(errors="ignore")
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
            return None

    def _bg(self, *cmd, timeout=12):
        if self.available:
            asyncio.create_task(self._run(*cmd, timeout=timeout))

    async def _max_alarm_volume(self):
        out = await self._run("termux-volume")
        try:
            for s in json.loads(out or "[]"):
                if s.get("stream") == "alarm":
                    await self._run("termux-volume", "alarm", str(s.get("max_volume", 7)))
                    return
        except Exception:
            pass

    def fire(self, key, alarm, message, phone):
        if not self.available:
            return
        level = alarm["level"]
        base = f"http://127.0.0.1:{self.port}"
        if phone.get("notification", True):
            cmd = ["termux-notification", "--id", f"owlet_{key}",
                   "--title", f"Owlet: {alarm['label']}", "--content", message,
                   "--priority", "max" if level != "notify" else "default",
                   "--button1", "Acknowledge",
                   "--button1-action", f"curl -s -X POST {base}/api/ack/{key}",
                   "--button2", "Snooze 10 min",
                   "--button2-action", f"curl -s -X POST '{base}/api/snooze/{key}?minutes=10'",
                   "--action", f"termux-open-url {base}/"]
            if level == "critical":
                cmd += ["--ongoing", "--led-color", "ff0000", "--vibrate", "800,400,800,400,800"]
            elif level == "warning":
                cmd += ["--led-color", "ffaa00", "--vibrate", "500,300,500", "--sound"]
            self._bg(*cmd)
        if level != "notify":
            self.sound(alarm, phone)

    def sound(self, alarm, phone):
        if not self.available:
            return
        if phone.get("max_volume") and not self._volume_set and alarm["level"] == "critical":
            self._volume_set = True
            asyncio.create_task(self._max_alarm_volume())
        if phone.get("vibrate", True):
            self._bg("termux-vibrate", "-f", "-d", "1200" if alarm["level"] == "critical" else "500")
        if phone.get("speak", True):
            self._bg("termux-tts-speak", "-s", "ALARM", alarm["label"], timeout=20)

    def clear(self, key):
        self._bg("termux-notification-remove", f"owlet_{key}")
        self._volume_set = False


# --------------------------------------------------------------------------------------
# Permanent history (SQLite file on the phone)
# --------------------------------------------------------------------------------------

class HistoryStore:
    """Every distinct valid reading is stored. Reads use their own connection in a worker
    thread so a long query never blocks polling or alarms."""

    def __init__(self, path=None):
        self.path = path or HISTORY_DB
        APP_DIR.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS readings (
            t REAL PRIMARY KEY, ox REAL, hr REAL, temp REAL, mv REAL)""")
        self.db.commit()
        self._last_prune = 0

    def add(self, t, ox, hr, temp, mv):
        try:
            self.db.execute("INSERT OR REPLACE INTO readings VALUES (?,?,?,?,?)", (t, ox, hr, temp, mv))
            self.db.commit()
        except sqlite3.Error as e:
            print("history write failed:", e)

    def prune(self, days):
        if days <= 0 or time.time() - self._last_prune < 3600:
            return
        self._last_prune = time.time()
        self.db.execute("DELETE FROM readings WHERE t < ?", (time.time() - days * 86400,))
        self.db.commit()

    def clear(self):
        self.db.execute("DELETE FROM readings")
        self.db.commit()
        self.db.execute("VACUUM")

    def recent(self, seconds):
        cur = self.db.execute("SELECT t, ox, hr, temp, mv FROM readings WHERE t >= ? ORDER BY t",
                              (time.time() - seconds,))
        return cur.fetchall()

    def _read(self, fn):
        con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            return fn(con)
        finally:
            con.close()

    def query_sync(self, minutes, points=700, end=None):
        def run(con):
            end_t = end or time.time()
            if minutes and minutes > 0:
                start = end_t - minutes * 60
            else:
                row = con.execute("SELECT MIN(t) FROM readings").fetchone()
                start = row[0] if row and row[0] else end_t - 3600
            span = max(60.0, end_t - start)
            stats = con.execute(
                "SELECT COUNT(*), AVG(ox), MIN(ox), MAX(ox), AVG(hr), MIN(hr), MAX(hr) "
                "FROM readings WHERE t >= ? AND t <= ?", (start, end_t)).fetchone()
            n = stats[0] or 0
            out = {"start": start, "end": end_t, "bucket": 0,
                   "stats": {"n": n, "ox_avg": stats[1], "ox_min": stats[2], "ox_max": stats[3],
                             "hr_avg": stats[4], "hr_min": stats[5], "hr_max": stats[6]},
                   "t": [], "ox": [], "hr": [], "ox_lo": [], "ox_hi": [], "hr_lo": [], "hr_hi": []}
            if n <= points:
                rows = con.execute("SELECT t, ox, hr FROM readings WHERE t >= ? AND t <= ? ORDER BY t",
                                   (start, end_t)).fetchall()
                for t, ox, hr in rows:
                    out["t"].append(round(t, 1)); out["ox"].append(ox); out["hr"].append(hr)
                    out["ox_lo"].append(ox); out["ox_hi"].append(ox)
                    out["hr_lo"].append(hr); out["hr_hi"].append(hr)
                return out
            bucket = span / points
            out["bucket"] = bucket
            rows = con.execute(
                "SELECT CAST((t - ?) / ? AS INTEGER) AS b, AVG(t), AVG(ox), MIN(ox), MAX(ox), "
                "AVG(hr), MIN(hr), MAX(hr) FROM readings WHERE t >= ? AND t <= ? GROUP BY b ORDER BY b",
                (start, bucket, start, end_t)).fetchall()
            for _, t, ox, oxl, oxh, hr, hrl, hrh in rows:
                out["t"].append(round(t, 1))
                out["ox"].append(round(ox, 1) if ox is not None else None)
                out["hr"].append(round(hr, 1) if hr is not None else None)
                out["ox_lo"].append(oxl); out["ox_hi"].append(oxh)
                out["hr_lo"].append(hrl); out["hr_hi"].append(hrh)
            return out
        return self._read(run)

    async def query(self, minutes, points=700):
        return await asyncio.to_thread(self.query_sync, minutes, points)

    def info_sync(self):
        def run(con):
            n, t0, t1 = con.execute("SELECT COUNT(*), MIN(t), MAX(t) FROM readings").fetchone()
            return {"count": n, "first": t0, "last": t1}
        out = self._read(run)
        try:
            size = sum(p.stat().st_size for p in APP_DIR.glob(self.path.name + "*"))
        except OSError:
            size = 0
        out["bytes"] = size
        return out

    def csv_sync(self, minutes):
        def run(con):
            start = time.time() - minutes * 60 if minutes else 0
            lines = ["time_local,time_unix,oxygen_pct,heart_rate_bpm,skin_temp_c,movement"]
            for t, ox, hr, temp, mv in con.execute(
                    "SELECT t, ox, hr, temp, mv FROM readings WHERE t >= ? ORDER BY t", (start,)):
                lines.append(f"{local_dt(t):%Y-%m-%d %H:%M:%S},{t:.1f},"
                             f"{'' if ox is None else ox},{'' if hr is None else hr},"
                             f"{'' if temp is None else temp},{'' if mv is None else mv}")
            return "\n".join(lines) + "\n"
        return self._read(run)


# --------------------------------------------------------------------------------------
# Monitor: polling, averaging, alarm engine
# --------------------------------------------------------------------------------------

class Monitor:
    def __init__(self, demo, port):
        APP_DIR.mkdir(parents=True, exist_ok=True)
        self.demo = demo
        self.settings = sanitize_settings(load_json(SETTINGS_FILE, {}))
        self.creds = load_json(CREDS_FILE, {})
        self.phone = PhoneAlarms(port)
        self.source = None
        self.status = "starting"          # starting | needs_login | connecting | live | error
        self.error = ""
        self.device = {}
        self.props = {}
        self.last_poll = 0.0
        self.last_poll_ms = 0
        self.last_new_reading = 0.0
        self.last_reading_key = None
        self.consecutive_errors = 0
        self.poll_times = deque(maxlen=600)
        self.reading_times = deque(maxlen=600)
        self.store = HistoryStore()
        # in-memory copy of the last 15 minutes, used for the rolling average
        self.readings = deque(self.store.recent(900), maxlen=2000)  # (t, ox, hr, temp, movement)
        self.alarm_state = {k: {"since": None, "active": False, "acked": False, "fired_at": None,
                                "last_sound": 0, "message": "", "snoozed_until": 0}
                            for k in DEFAULT_ALARMS}
        self.paused_until = 0
        self.events = deque(load_events(), maxlen=300)
        self._wake = asyncio.Event()
        self._reconnect = asyncio.Event()

    # ---- settings / account ----
    def update_settings(self, incoming):
        self.settings = sanitize_settings(deep_merge(self.settings, incoming))
        save_json(SETTINGS_FILE, self.settings)
        self._wake.set()
        return self.settings

    def login(self, user, password, region):
        self.creds = {"user": user.strip(), "password": password, "region": region}
        save_json(CREDS_FILE, self.creds, private=True)
        self.settings["region"] = region
        save_json(SETTINGS_FILE, self.settings)
        self.status = "connecting"
        self.error = ""
        self._reconnect.set()

    def logout(self):
        self.creds = {}
        try:
            CREDS_FILE.unlink()
        except FileNotFoundError:
            pass
        self.status = "needs_login"
        self._reconnect.set()

    # ---- events ----
    def log(self, kind, key, message):
        ev = {"t": time.time(), "kind": kind, "key": key, "message": message}
        self.events.append(ev)
        try:
            with EVENTS_FILE.open("a") as f:
                f.write(json.dumps(ev) + "\n")
        except Exception:
            pass

    # ---- main loop ----
    async def run(self):
        while True:
            self._reconnect.clear()
            try:
                if self.source:
                    await self.source.close()
                self.source = DemoSource(self.settings) if self.demo else OwletSource(self.settings, self.creds)
                self.status = "connecting"
                await self.source.connect()
                self.device = dict(self.source.device)
                self.status = "live"
                self.error = ""
                self.consecutive_errors = 0
                self.log("info", "", f"Connected to {self.device.get('name')}")
                await self._poll_loop()
            except NeedsLogin:
                self.status = "needs_login"
                self.error = ""
                await self._reconnect.wait()
            except Exception as e:
                self.status = "error"
                self.error = f"{type(e).__name__}: {e}"
                traceback.print_exc()
                self.consecutive_errors += 1
                self.evaluate_alarms()
                wait = min(60, 5 * self.consecutive_errors)
                try:
                    await asyncio.wait_for(self._reconnect.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass

    async def _poll_loop(self):
        last_full = 0.0
        last_devices = time.time()
        last_activate = 0.0
        while not self._reconnect.is_set():
            s = self.settings
            start = time.time()
            try:
                if start - last_devices >= s["device_refresh_seconds"]:
                    await self.source.refresh_devices()
                    last_devices = start
                if start - last_full >= s["full_refresh_seconds"]:
                    snap = await self.source.full_refresh()   # includes APP_ACTIVE
                    last_full = last_activate = start
                else:
                    if start - last_activate >= s["activate_seconds"]:
                        await self.source.activate()
                        last_activate = start
                    snap = await self.source.fast_refresh()
                self._ingest(snap)
                self.consecutive_errors = 0
                if self.status != "live":
                    self.status, self.error = "live", ""
            except Exception as e:
                self.consecutive_errors += 1
                self.error = f"{type(e).__name__}: {e}"
                if self.consecutive_errors >= 3:
                    self.status = "error"
                print(f"[{datetime.now():%H:%M:%S}] poll error #{self.consecutive_errors}: {self.error}")
                if self.consecutive_errors >= 10:
                    raise  # full reconnect
            self.last_poll = time.time()
            self.last_poll_ms = int((self.last_poll - start) * 1000)
            self.poll_times.append(self.last_poll)
            self.evaluate_alarms()
            # error back-off: 1,2,4,8..30 s
            interval = s["poll_seconds"]
            if self.consecutive_errors:
                interval = min(30, max(interval, 2 ** min(self.consecutive_errors, 5)))
            delay = max(0.05, interval - (time.time() - start))
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    def _ingest(self, snap):
        now = time.time()
        self.props = snap["props"]
        self.device = snap.get("device") or self.device
        key = snap.get("reading_key")
        if key != self.last_reading_key:
            self.last_reading_key = key
            self.last_new_reading = now
            self.reading_times.append(now)
            p = self.props
            ox, hr = _to_float(p.get("oxygen_saturation")), _to_float(p.get("heart_rate"))
            valid = (ox and ox > 0 and hr and hr > 0 and not p.get("sock_off")
                     and not p.get("sock_disconnected") and not p.get("charging"))
            if valid:
                row = (now, ox, hr, _to_float(p.get("skin_temperature")), _to_float(p.get("movement")))
                self.readings.append(row)
                self.store.add(*row)
        while self.readings and self.readings[0][0] < now - 900:
            self.readings.popleft()
        self.store.prune(self.settings["history_days"])

    # ---- derived values ----
    def averages(self):
        n = self.settings["avg_window"]
        recent = list(self.readings)[-n:]
        # only average readings from the last 2 minutes - never mix in old data
        recent = [r for r in recent if time.time() - r[0] < 120]
        if not recent:
            return {"ox": None, "hr": None, "temp": None, "n": 0}

        def avg(i):
            vals = [r[i] for r in recent if r[i] is not None]
            return round(sum(vals) / len(vals), 1) if vals else None
        return {"ox": avg(1), "hr": avg(2), "temp": avg(3), "n": len(recent)}

    def rates(self):
        now = time.time()
        polls = [t for t in self.poll_times if now - t < 60]
        reads = [t for t in self.reading_times if now - t < 300]
        gaps = [b - a for a, b in zip(reads, reads[1:])]
        return {
            "polls_per_min": len(polls),
            "seconds_between_readings": round(sum(gaps) / len(gaps), 1) if gaps else None,
            "last_poll_ms": self.last_poll_ms,
            "seconds_since_new": round(now - self.last_new_reading, 1) if self.last_new_reading else None,
        }

    def monitoring(self):
        bso = self.props.get("base_station_on")
        return bso in (1, True, "1") or bso is None

    # ---- alarm engine ----
    def _conditions(self):
        """Return {key: (condition_bool, message)}."""
        s = self.settings
        p = self.props
        A = s["alarms"]
        now = time.time()
        res = {}
        avg = self.averages()
        use_avg = s["alarms_use_average"]
        live_ox, live_hr = _to_float(p.get("oxygen_saturation")), _to_float(p.get("heart_rate"))
        fresh = self.last_new_reading and now - self.last_new_reading < 120
        sock_on = bool(p) and not p.get("sock_off") and not p.get("sock_disconnected") and not p.get("charging")
        ox = avg["ox"] if use_avg else (live_ox if (live_ox and fresh and sock_on) else None)
        hr = avg["hr"] if use_avg else (live_hr if (live_hr and fresh and sock_on) else None)
        watching = self.monitoring() or not s["only_while_monitoring"]
        what = "average" if use_avg else "reading"

        t = A["low_oxygen"]["threshold"]
        res["low_oxygen"] = (watching and ox is not None and ox < t,
                             f"Oxygen {what} {ox:.0f}% (below {t:.0f}%)" if ox is not None else "")
        t = A["high_heart_rate"]["threshold"]
        res["high_heart_rate"] = (watching and hr is not None and hr > t,
                                  f"Heart rate {what} {hr:.0f} bpm (above {t:.0f})" if hr is not None else "")
        t = A["low_heart_rate"]["threshold"]
        res["low_heart_rate"] = (watching and hr is not None and hr < t,
                                 f"Heart rate {what} {hr:.0f} bpm (below {t:.0f})" if hr is not None else "")

        flags = {"high_oxygen_alert": "high oxygen", "low_oxygen_alert": "low oxygen",
                 "high_heart_rate_alert": "high heart rate", "low_heart_rate_alert": "low heart rate",
                 "lost_power_alert": "base lost power"}
        raised = [name for f, name in flags.items() if p.get(f) in (True, 1, "1")]
        res["owlet_alerts"] = (bool(raised), "Base station alert: " + ", ".join(raised) if raised else "")

        off = bool(p) and (p.get("sock_off") or p.get("sock_disconnected")) and not p.get("charging")
        res["sock_off"] = (watching and bool(off), "Sock is off or disconnected from the base")

        t = A["no_data"]["threshold"]
        if self.last_new_reading:
            age = now - self.last_new_reading
        else:
            age = now - START_TIME
        reason = ""
        if self.consecutive_errors:
            reason = " - phone can't reach Owlet's servers"
        elif self.device.get("connection_status") not in (None, "Online"):
            reason = " - base is offline"
        res["no_data"] = (self.status not in ("needs_login", "starting") and watching and age >= t,
                          f"No new reading for {age:.0f} s{reason}")

        cs = self.device.get("connection_status")
        lost = p.get("lost_power_alert") in (True, 1, "1")
        res["base_offline"] = ((cs not in (None, "Online")) or lost,
                               "Base lost power" if lost else f"Base station is {cs}")

        b = _to_float(p.get("battery_percentage"))
        t = A["low_battery"]["threshold"]
        res["low_battery"] = (b is not None and b < t and not p.get("charging"),
                              f"Sock battery {b:.0f}%" if b is not None else "")
        return res

    def evaluate_alarms(self):
        now = time.time()
        s = self.settings
        conds = self._conditions()
        paused = now < self.paused_until
        for key, (cond, message) in conds.items():
            a = s["alarms"][key]
            st = self.alarm_state[key]
            allowed = a["enabled"] and in_schedule(a["schedule"])
            if cond and allowed:
                if st["since"] is None:
                    st["since"] = now
                st["message"] = message
                if (not st["active"] and now - st["since"] >= a["delay"]
                        and not paused and now >= st["snoozed_until"]):
                    st.update(active=True, acked=False, fired_at=now, last_sound=now)
                    self.log("alarm", key, message)
                    print(f"[{datetime.now():%H:%M:%S}] ALARM {a['label']}: {message}")
                    self.phone.fire(key, a, message, s["phone"])
            else:
                if st["active"]:
                    self.log("clear", key, f"{a['label']} cleared")
                    self.phone.clear(key)
                st.update(since=None, active=False, acked=False, fired_at=None)
        # repeat critical, unacknowledged alarms on the phone
        for key, st in self.alarm_state.items():
            a = s["alarms"][key]
            if (st["active"] and not st["acked"] and a["level"] == "critical" and not paused
                    and now - st["last_sound"] >= s["repeat_seconds"]):
                st["last_sound"] = now
                self.phone.sound(a, s["phone"])

    def ack(self, key=None):
        if key and key.startswith("test_"):
            self.phone.clear(key)
            return
        keys = [key] if key else list(self.alarm_state)
        for k in keys:
            st = self.alarm_state.get(k)
            if st and st["active"] and not st["acked"]:
                st["acked"] = True
                self.log("ack", k, f"{self.settings['alarms'][k]['label']} acknowledged")
                self.phone.clear(k)

    def snooze(self, key, minutes):
        if key.startswith("test_"):
            self.phone.clear(key)
            return
        st = self.alarm_state.get(key)
        if not st:
            return
        st["snoozed_until"] = time.time() + minutes * 60
        if st["active"]:
            self.phone.clear(key)
        st.update(active=False, acked=False, since=None)
        self.log("snooze", key, f"{self.settings['alarms'][key]['label']} snoozed {minutes:g} min")

    def pause(self, minutes):
        self.paused_until = time.time() + minutes * 60 if minutes > 0 else 0
        if minutes > 0:
            for k, st in self.alarm_state.items():
                if st["active"]:
                    self.phone.clear(k)
                st.update(active=False, acked=False, since=None)
            self.log("pause", "", f"All alarms paused for {minutes:g} min")
        else:
            self.log("pause", "", "Alarms resumed")

    def test_alarm(self, key):
        a = self.settings["alarms"].get(key)
        if a:
            self.phone.fire(f"test_{key}", a, "Test alarm - tap Acknowledge to clear", self.settings["phone"])
            self.log("test", key, f"Test: {a['label']}")

    # ---- API payloads ----
    def state(self):
        now = time.time()
        active = []
        for k, st in self.alarm_state.items():
            a = self.settings["alarms"][k]
            entry = {"key": k, "label": a["label"], "level": a["level"], "sound": a["sound"],
                     "message": st["message"]}
            if st["active"]:
                active.append(dict(entry, acked=st["acked"], fired_at=st["fired_at"]))
        pending = [{"key": k, "label": self.settings["alarms"][k]["label"],
                    "in": round(self.settings["alarms"][k]["delay"] - (now - st["since"]), 0)}
                   for k, st in self.alarm_state.items()
                   if st["since"] and not st["active"] and self.settings["alarms"][k]["enabled"]]
        snoozed = {k: round(st["snoozed_until"] - now) for k, st in self.alarm_state.items()
                   if st["snoozed_until"] > now}
        return {
            "version": APP_VERSION, "demo": self.demo, "status": self.status, "error": self.error,
            "device": self.device, "props": self.props, "avg": self.averages(), "rates": self.rates(),
            "monitoring": self.monitoring(), "alarms": active, "pending": pending, "snoozed": snoozed,
            "paused_for": max(0, round(self.paused_until - now)),
            "phone_alarms": self.phone.available,
            "account": self.creds.get("user", "") if not self.demo else "demo",
            "fast_path": getattr(self.source, "fast_ok", True),
            "now": now,
        }

    async def history(self, minutes):
        return await self.store.query(minutes)


def load_events():
    out = []
    try:
        lines = EVENTS_FILE.read_text().splitlines()[-300:]
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
        if len(lines) >= 300:  # keep the file small
            EVENTS_FILE.write_text("\n".join(lines) + "\n")
    except FileNotFoundError:
        pass
    return out


# --------------------------------------------------------------------------------------
# Tiny HTTP server (stdlib only)
# --------------------------------------------------------------------------------------

UI_HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0b1416">
<link rel="manifest" href="/manifest.json">
<title>Owlet Monitor</title>
<style>
  :root {
    --bg: #0b1416; --card: #13201f; --card2: #182927; --line: #24403c;
    --ink: #e7f1ef; --ink2: #a9bfbb; --muted: #6f8a85;
    --ox: #3cc6b4; --hr: #f08a7a;
    --good: #3fbf7f; --warn: #f2b33d; --crit: #ff4d4f; --info: #6aa8ff;
    --r: 16px;
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body { margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.4 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  body { padding-bottom: calc(72px + env(safe-area-inset-bottom)); }
  button, input, select { font: inherit; color: inherit; }
  .wrap { max-width: 720px; margin: 0 auto; padding: 12px 14px; }
  header { position: sticky; top: 0; z-index: 5; background: var(--bg);
    padding: 10px 14px 8px; border-bottom: 1px solid var(--line); }
  header .row { max-width: 720px; margin: 0 auto; display: flex; align-items: center; gap: 10px; }
  header h1 { font-size: 17px; margin: 0; flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .pill { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 99px;
    font-size: 13px; background: var(--card2); color: var(--ink2); white-space: nowrap; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); }
  .dot.good { background: var(--good); } .dot.warn { background: var(--warn); } .dot.crit { background: var(--crit); }
  .card { background: var(--card); border-radius: var(--r); padding: 14px; margin-bottom: 12px; }
  .card h2 { font-size: 13px; letter-spacing: .06em; text-transform: uppercase; color: var(--muted);
    margin: 0 0 10px; font-weight: 600; }
  .muted { color: var(--muted); } .ink2 { color: var(--ink2); } .small { font-size: 13px; }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .vital { background: var(--card); border-radius: var(--r); padding: 14px 10px 12px; text-align: center; position: relative; }
  .vital .ring { width: 100%; max-width: 170px; aspect-ratio: 1; margin: 0 auto; position: relative; }
  .vital svg { width: 100%; height: 100%; transform: rotate(-90deg); }
  .vital .center { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; }
  .vital .num { font-size: 46px; font-weight: 700; line-height: 1; font-variant-numeric: tabular-nums; }
  .vital .unit { font-size: 14px; color: var(--ink2); margin-top: 2px; }
  .vital .lbl { font-size: 14px; color: var(--ink2); margin-top: 8px; font-weight: 600; }
  .vital .sub { font-size: 12px; color: var(--muted); margin-top: 2px; font-variant-numeric: tabular-nums; }
  .badge { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; padding: 2px 8px; border-radius: 99px;
    background: var(--card2); color: var(--ink2); margin-top: 6px; }
  .badge.good { color: var(--good); } .badge.warn { color: var(--warn); } .badge.crit { color: #fff; background: var(--crit); }
  .chips { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
  .chip { background: var(--card2); border-radius: 12px; padding: 9px 10px; min-width: 0; }
  .chip .k { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; }
  .chip .v { font-size: 15px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .chip .v.warn { color: var(--warn); } .chip .v.crit { color: var(--crit); } .chip .v.good { color: var(--good); }
  .rate { font-size: 12px; color: var(--muted); margin-top: 10px; line-height: 1.5; }
  .chartbox { position: relative; }
  .chartbox canvas { width: 100%; height: 150px; display: block; touch-action: pan-y; }
  .chart-title { display: flex; justify-content: space-between; align-items: baseline; margin: 6px 0 4px; }
  .chart-title b { font-size: 14px; } .chart-title span { font-size: 12px; color: var(--muted); }
  .tip { position: absolute; pointer-events: none; background: #0b1416ee; border: 1px solid var(--line); border-radius: 8px;
    padding: 4px 8px; font-size: 12px; white-space: nowrap; display: none; transform: translateX(-50%); top: 0; }
  .seg { display: inline-flex; background: var(--card2); border-radius: 10px; padding: 3px; gap: 2px; }
  .seg button { border: 0; background: none; padding: 5px 10px; border-radius: 8px; color: var(--ink2); font-size: 13px; }
  .seg button.on { background: var(--line); color: var(--ink); }
  .btn { border: 0; border-radius: 12px; padding: 11px 16px; background: var(--card2); font-weight: 600; cursor: pointer; }
  .btn.primary { background: var(--ox); color: #062521; } .btn.danger { background: var(--crit); color: #fff; }
  .btn.ghost { background: transparent; border: 1px solid var(--line); }
  a.btn { color: var(--ink); display: inline-block; }
  .btn.sm { padding: 7px 12px; font-size: 13px; border-radius: 10px; }
  .btn:disabled { opacity: .5; }
  .row { display: flex; align-items: center; gap: 10px; }
  .row.wrap { flex-wrap: wrap; }
  .spacer { flex: 1; }
  .banner { border-radius: 12px; padding: 10px 12px; margin-bottom: 10px; display: flex; gap: 10px; align-items: center; }
  .banner.warn { background: #3a2c0e; color: #ffd98a; } .banner.crit { background: #4a1414; color: #ffd0d0; }
  .banner.info { background: #14283f; color: #cfe2ff; } .banner.notify { background: var(--card2); color: var(--ink2); }
  .banner .msg { flex: 1; font-size: 14px; } .banner b { display: block; }
  /* alarm overlay */
  #overlay { position: fixed; inset: 0; z-index: 50; background: #7a0d10; display: none;
    flex-direction: column; align-items: center; justify-content: center; padding: 24px; text-align: center; }
  #overlay.show { display: flex; animation: flash 1s infinite; }
  @keyframes flash { 50% { background: #b3161b; } }
  #overlay .icon { font-size: 64px; line-height: 1; }
  #overlay h2 { font-size: 30px; margin: 12px 0 6px; }
  #overlay p { font-size: 17px; margin: 0 0 26px; color: #ffe3e3; }
  #overlay .btn { width: 100%; max-width: 360px; padding: 18px; font-size: 19px; margin-bottom: 12px; }
  #overlay .ackbtn { background: #fff; color: #7a0d10; }
  #overlay .snz { background: transparent; border: 2px solid #ffb3b3; color: #fff; }
  /* tabs */
  nav { position: fixed; bottom: 0; left: 0; right: 0; z-index: 10; background: #0e1a1b; border-top: 1px solid var(--line);
    display: flex; padding-bottom: env(safe-area-inset-bottom); }
  nav button { flex: 1; border: 0; background: none; padding: 9px 0 10px; color: var(--muted); font-size: 12px;
    display: flex; flex-direction: column; align-items: center; gap: 2px; position: relative; }
  nav button .i { font-size: 20px; line-height: 1.1; }
  nav button.on { color: var(--ox); }
  nav .cnt { position: absolute; top: 4px; left: 55%; background: var(--crit); color: #fff; border-radius: 99px;
    font-size: 10px; padding: 0 5px; display: none; }
  section.view { display: none; } section.view.on { display: block; }
  /* forms */
  .alarm { background: var(--card); border-radius: var(--r); padding: 14px; margin-bottom: 10px; }
  .alarm.off { opacity: .6; }
  .alarm .top { display: flex; align-items: center; gap: 10px; }
  .alarm .top b { flex: 1; font-size: 16px; }
  .alarm .help { font-size: 13px; color: var(--muted); margin: 4px 0 10px; }
  .fields { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .field label { display: block; font-size: 12px; color: var(--muted); margin-bottom: 4px; }
  .field input, .field select { width: 100%; background: var(--card2); border: 1px solid var(--line); border-radius: 10px;
    padding: 9px 10px; }
  .field .inp { display: flex; align-items: center; background: var(--card2); border: 1px solid var(--line); border-radius: 10px; }
  .field .inp input { border: 0; background: none; min-width: 0; }
  .field .inp span { padding: 0 10px 0 0; color: var(--muted); font-size: 13px; }
  .field.full { grid-column: 1 / -1; }
  .sched { display: flex; gap: 6px; align-items: center; }
  .sched input { flex: 1; }
  .switch { position: relative; width: 48px; height: 28px; flex: none; }
  .switch input { opacity: 0; width: 0; height: 0; position: absolute; }
  .switch span { position: absolute; inset: 0; background: var(--line); border-radius: 99px; transition: .2s; }
  .switch span::after { content: ""; position: absolute; width: 22px; height: 22px; left: 3px; top: 3px; border-radius: 50%;
    background: #cfdcda; transition: .2s; }
  .switch input:checked + span { background: var(--ox); }
  .switch input:checked + span::after { transform: translateX(20px); background: #fff; }
  .setrow { display: flex; align-items: center; gap: 12px; padding: 10px 0; border-bottom: 1px solid var(--line); }
  .setrow:last-child { border-bottom: 0; }
  .setrow .t { flex: 1; } .setrow .t small { display: block; color: var(--muted); font-size: 12px; }
  .setrow input[type=number] { width: 84px; background: var(--card2); border: 1px solid var(--line); border-radius: 10px; padding: 8px; text-align: right; }
  .ev { display: flex; gap: 10px; padding: 9px 0; border-bottom: 1px solid var(--line); font-size: 14px; }
  .ev .tm { color: var(--muted); font-variant-numeric: tabular-nums; width: 74px; flex: none; font-size: 13px; }
  .ev .ic { width: 20px; flex: none; text-align: center; }
  #toast { position: fixed; bottom: 86px; left: 50%; transform: translateX(-50%); background: var(--ink); color: var(--bg);
    padding: 8px 16px; border-radius: 99px; font-size: 14px; font-weight: 600; opacity: 0; transition: .25s; z-index: 60; pointer-events: none; }
  #toast.show { opacity: 1; }
  .login { max-width: 380px; margin: 40px auto; }
  .login .field { margin-bottom: 12px; }
  .disclaimer { font-size: 12px; color: var(--muted); line-height: 1.5; }
  .pend { font-size: 13px; color: var(--warn); margin: -4px 0 10px; }
  details.orig { margin-top: 10px; background: #0f1b1a; border: 1px solid var(--line); border-radius: 12px; }
  details.orig summary { list-style: none; cursor: pointer; padding: 8px 12px; font-size: 13px; color: var(--info);
    display: flex; align-items: center; gap: 6px; }
  details.orig summary::-webkit-details-marker { display: none; }
  details.orig summary::after { content: "▾"; margin-left: auto; color: var(--muted); }
  details.orig[open] summary::after { content: "▴"; }
  details.orig .body { padding: 0 12px 10px; font-size: 13px; color: var(--ink2); line-height: 1.5; }
  details.orig .body b { color: var(--ink); font-weight: 600; }
  .seg.wide { display: flex; width: 100%; } .seg.wide button { flex: 1; padding: 6px 0; }
  .statline { font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }
  @media (max-width: 380px) { .vital .num { font-size: 38px; } .chips { grid-template-columns: repeat(2, 1fr); } }
</style>
</head>
<body>

<header>
  <div class="row">
    <h1 id="devname">Owlet Monitor</h1>
    <span class="pill" id="statuspill"><span class="dot" id="statusdot"></span><span id="statustext">Starting…</span></span>
  </div>
</header>

<div id="overlay" role="alertdialog" aria-live="assertive">
  <div class="icon">⚠️</div>
  <h2 id="ovTitle">Alarm</h2>
  <p id="ovMsg"></p>
  <button class="btn ackbtn" id="ovAck">Acknowledge</button>
  <button class="btn snz" id="ovSnooze">Snooze 10 min</button>
</div>

<div class="wrap">
  <div id="banners"></div>

  <!-- LOGIN -->
  <section class="view" id="v-login">
    <div class="login card">
      <h2>Sign in with your Owlet account</h2>
      <div class="field"><label>Email</label><input id="lgUser" type="email" autocomplete="username"></div>
      <div class="field"><label>Password</label><input id="lgPass" type="password" autocomplete="current-password"></div>
      <div class="field"><label>Region</label>
        <select id="lgRegion"><option value="europe">Europe</option><option value="world">Rest of world</option></select></div>
      <button class="btn primary" id="lgBtn" style="width:100%">Sign in</button>
      <p class="disclaimer">Your login is stored only on this phone (inside Termux) and is sent only to Owlet's servers.</p>
    </div>
  </section>

  <!-- MONITOR -->
  <section class="view" id="v-monitor">
    <div class="grid2" style="margin-bottom:12px">
      <div class="vital" id="oxCard">
        <div class="ring">
          <svg viewBox="0 0 100 100"><circle cx="50" cy="50" r="44" fill="none" stroke="#24403c" stroke-width="7"/>
            <circle id="oxArc" cx="50" cy="50" r="44" fill="none" stroke="var(--ox)" stroke-width="7" stroke-linecap="round"
              stroke-dasharray="276.5" stroke-dashoffset="276.5"/></svg>
          <div class="center"><div class="num" id="oxNum">--</div><div class="unit">%</div></div>
        </div>
        <div class="lbl">Oxygen</div>
        <div class="sub" id="oxSub">&nbsp;</div>
        <div class="badge" id="oxBadge">—</div>
      </div>
      <div class="vital" id="hrCard">
        <div class="ring">
          <svg viewBox="0 0 100 100"><circle cx="50" cy="50" r="44" fill="none" stroke="#24403c" stroke-width="7"/>
            <circle id="hrArc" cx="50" cy="50" r="44" fill="none" stroke="var(--hr)" stroke-width="7" stroke-linecap="round"
              stroke-dasharray="276.5" stroke-dashoffset="276.5"/></svg>
          <div class="center"><div class="num" id="hrNum">--</div><div class="unit">bpm</div></div>
        </div>
        <div class="lbl">Heart rate</div>
        <div class="sub" id="hrSub">&nbsp;</div>
        <div class="badge" id="hrBadge">—</div>
      </div>
    </div>
    <details class="orig" style="margin:-2px 0 12px"><summary>ⓘ How the Owlet app shows these</summary><div class="body">
      With <b>Health Notifications</b> switched on, the Owlet Dream app shows your baby's <b>live</b> heart rate and oxygen during a session.
      Without them, the Dream Sock only shows <b>"Average O2 (10 min)"</b>: one number recalculated every 10 minutes, not a live reading
      (that's the "Owlet 10-min O₂" tile below). Owlet's history graphs also use 10-minute averages.<br>
      <b>Here:</b> the big number is the average of your last few readings (set in Settings), and the live value is shown underneath.
    </div></details>
    <div id="pending"></div>

    <div class="card">
      <h2>Sock &amp; base</h2>
      <div class="chips" id="chips"></div>
      <div class="rate" id="rate"></div>
      <details class="orig"><summary>ⓘ How the Owlet base shows status</summary><div class="body">
        The Owlet base station uses its light ring: <b>green</b> = tracking normally · <b>yellow</b> = sock off, badly placed, or out of range
        for at least 60 seconds · <b>blue</b> = sock battery has about 50 minutes left · <b>red</b> = heart rate or oxygen outside Owlet's limits ·
        <b>lavender</b> (Dream Sock) = lots of movement for a long time. If several are active, the base shows the most urgent one.
        The app shows the sock's Bluetooth signal in the Sock control hub on the Home page.
      </div></details>
    </div>

    <div class="card">
      <h2 style="margin:0 0 8px">History</h2>
      <div class="seg wide" id="rangeSeg">
        <button data-m="15">15m</button><button data-m="60">1h</button><button data-m="360">6h</button><button data-m="720">12h</button>
        <button data-m="1440">24h</button><button data-m="10080">7d</button><button data-m="0">All</button>
      </div>
      <div class="chart-title" style="margin-top:12px"><b>Oxygen (%)</b><span id="oxRange"></span></div>
      <div class="chartbox"><canvas id="oxChart"></canvas><div class="tip" id="oxTip"></div></div>
      <div class="chart-title" style="margin-top:12px"><b>Heart rate (bpm)</b><span id="hrRange"></span></div>
      <div class="chartbox"><canvas id="hrChart"></canvas><div class="tip" id="hrTip"></div></div>
      <div class="small muted" style="margin-top:8px">Line = average, shaded band = lowest to highest reading in each slot, so short dips stay visible on long ranges.
        Dashed line = your alarm threshold. Every valid reading is saved on this phone.</div>
      <details class="orig"><summary>ⓘ How the Owlet app shows history</summary><div class="body">
        The Owlet Dream app graphs <b>10-minute averages</b> of oxygen and pulse rate for each sleep session, with an average for the whole session,
        and only for periods with enough data. The older Owlet app for the Smart Sock 3 kept history for <b>30 days</b>.<br>
        <b>Here:</b> every reading is stored on your phone and kept until you delete it (Settings → History). You can also export it as a spreadsheet.
      </div></details>
    </div>
  </section>

  <!-- ALARMS -->
  <section class="view" id="v-alarms">
    <div class="card">
      <h2>All alarms</h2>
      <div class="row wrap" id="pauseRow" style="margin-bottom:6px"></div>
      <div class="setrow"><div class="t">Use the average for alarms<small>Smooths out single bad readings. Off = react to every single reading.</small></div>
        <label class="switch"><input type="checkbox" data-set="alarms_use_average"><span></span></label></div>
      <div class="setrow"><div class="t">Only while the base is monitoring<small>No vitals or sock alarms while the base station is switched off.</small></div>
        <label class="switch"><input type="checkbox" data-set="only_while_monitoring"><span></span></label></div>
      <div class="setrow"><div class="t">Repeat critical alarms every<small>Until you tap Acknowledge</small></div>
        <input type="number" min="3" max="120" step="1" data-set="repeat_seconds"><span class="muted small">s</span></div>
      <details class="orig"><summary>ⓘ How the Owlet app handles alarms</summary><div class="body">
        <b>Snooze:</b> a short press on the base or "Dismiss" in the app silences the sound for <b>60 seconds</b>. The light stays on and the alert
        stays active until the reading recovers; a new alert restarts the sound. There is no "acknowledge" that silences it for good.<br>
        <b>Pause:</b> one tap on the base pauses monitoring; hold it until it beeps to switch the base off. In "Recovery mode", a paused base
        restarts monitoring after 20 minutes if the sock gets 4 minutes of good readings.<br>
        <b>Averaging:</b> Owlet doesn't publish any averaging or delay for its red alerts; they're described as triggering when the measured value
        crosses the limit.<br>
        <b>Thresholds:</b> fixed by Owlet, you can't change them. Use the button below to copy them here.
      </div></details>
      <div class="row" style="margin-top:10px"><button class="btn sm" id="owletPreset">Use Owlet's thresholds</button>
        <span class="small muted">80% oxygen · 50 / 220 bpm · 60 s sock off</span></div>
    </div>

    <div class="card">
      <h2>On this phone</h2>
      <div id="phoneWarn"></div>
      <div class="setrow"><div class="t">Notification<small>Works with the browser closed. Has Acknowledge / Snooze buttons.</small></div>
        <label class="switch"><input type="checkbox" data-set="phone.notification"><span></span></label></div>
      <div class="setrow"><div class="t">Vibrate</div>
        <label class="switch"><input type="checkbox" data-set="phone.vibrate"><span></span></label></div>
      <div class="setrow"><div class="t">Speak the alarm out loud<small>Uses the phone's ALARM volume, so it plays when the ringer is silent.</small></div>
        <label class="switch"><input type="checkbox" data-set="phone.speak"><span></span></label></div>
      <div class="setrow"><div class="t">Turn alarm volume to max<small>On critical alarms</small></div>
        <label class="switch"><input type="checkbox" data-set="phone.max_volume"><span></span></label></div>
      <details class="orig"><summary>ⓘ How the Owlet app alerts you</summary><div class="body">
        During a red alert the <b>base station</b> flashes red and plays a loud tone in the nursery, and the <b>app</b> sends a push notification.
        Red (health) alerts only work after you switch on <b>Health Notifications</b> in the Dream app's sock settings, which includes
        playing a test of the red alert sound.<br>
        <b>Here:</b> alarms sound on this phone (and in the nursery only if the Owlet base does it too).
      </div></details>
    </div>

    <div id="alarmList"></div>
    <div class="row" style="margin:6px 0 14px"><button class="btn ghost sm" id="resetAlarms">Reset alarms to defaults</button></div>
    <p class="disclaimer">The default thresholds are starting points, not medical advice. Agree the right values with your paediatrician.
      This app is not a medical device; keep the Owlet base station and official app alarms on.</p>
  </section>

  <!-- LOG -->
  <section class="view" id="v-log">
    <div class="card"><h2>Alarm log</h2><div id="events"></div></div>
  </section>

  <!-- SETTINGS -->
  <section class="view" id="v-settings">
    <div class="card">
      <h2>Readings</h2>
      <div class="setrow"><div class="t">Ask for new data every<small>1 s is the fastest. The base itself only uploads every few seconds.</small></div>
        <input type="number" min="1" max="60" step="0.5" data-set="poll_seconds"><span class="muted small">s</span></div>
      <div class="setrow"><div class="t">Average over the last<small>Distinct readings (not polls)</small></div>
        <input type="number" min="1" max="60" step="1" data-set="avg_window"><span class="muted small">readings</span></div>
      <div class="setrow"><div class="t">Full refresh every<small>Reads Owlet's alert flags and status</small></div>
        <input type="number" min="5" max="600" step="1" data-set="full_refresh_seconds"><span class="muted small">s</span></div>
      <div class="setrow"><div class="t">Wake the base every<small>Tells the base an app is watching, so it keeps sending live data</small></div>
        <input type="number" min="5" max="120" step="1" data-set="activate_seconds"><span class="muted small">s</span></div>
      <details class="orig"><summary>ⓘ How the Owlet app gets readings</summary><div class="body">
        The sock measures continuously and sends readings to the base over Bluetooth; the base uploads them to Owlet's cloud over Wi-Fi,
        and the app reads them from the cloud. While an app is watching, it sends the base an "app active" signal so the base keeps sending
        live readings. That's what "Wake the base" does here. Owlet doesn't publish how often the base uploads; the line under
        "Sock &amp; base" on the Monitor tab shows what your base actually does.
      </div></details>
    </div>
    <div class="card">
      <h2>History</h2>
      <div class="small ink2" id="histInfo">—</div>
      <div class="setrow"><div class="t">Keep readings for</div>
        <select data-set="history_days" class="field" style="width:auto;background:var(--card2);border:1px solid var(--line);border-radius:10px;padding:8px">
          <option value="0">Forever</option><option value="7">7 days</option><option value="30">30 days</option>
          <option value="90">90 days</option><option value="365">1 year</option></select></div>
      <div class="row wrap" style="margin-top:10px">
        <a class="btn sm" id="exportCsv" href="/api/export.csv" download style="text-decoration:none">Export all as CSV</a>
        <span class="spacer"></span><button class="btn sm ghost" id="clearHist">Delete history</button>
      </div>
    </div>
    <div class="card">
      <h2>Screen</h2>
      <div class="setrow"><div class="t">Keep the screen on<small>While this page is open</small></div>
        <label class="switch"><input type="checkbox" id="wakeToggle"><span></span></label></div>
    </div>
    <div class="card" id="appCard" style="display:none">
      <h2>Android app</h2>
      <div class="setrow"><div class="t">Demo mode<small>Fake data for testing alarms. The app restarts when you switch.</small></div>
        <label class="switch"><input type="checkbox" id="demoToggle"><span></span></label></div>
      <div class="setrow"><div class="t">Battery optimisation<small>Must be off, or Android may stop monitoring at night</small></div>
        <button class="btn sm" id="battBtn">Check</button></div>
      <div class="setrow"><div class="t">Notification &amp; alarm settings<small>Sound, lock screen, Do Not Disturb</small></div>
        <button class="btn sm" id="notifBtn">Open</button></div>
    </div>
    <div class="card" id="demoCard" style="display:none">
      <h2>Demo: simulate a problem</h2>
      <p class="small muted" style="margin-top:0">Each lasts 90 seconds. Use these to hear and tune your alarms.</p>
      <div class="row wrap">
        <button class="btn sm" data-demo="low_oxygen">Low oxygen</button>
        <button class="btn sm" data-demo="high_heart_rate">High HR</button>
        <button class="btn sm" data-demo="low_heart_rate">Low HR</button>
        <button class="btn sm" data-demo="sock_off">Sock off</button>
        <button class="btn sm" data-demo="stale">No data</button>
        <button class="btn sm" data-demo="base_offline">Base offline</button>
        <button class="btn sm" data-demo="low_battery">Low battery</button>
        <button class="btn sm" data-demo="owlet_alert">Owlet alert</button>
        <button class="btn sm" data-demo="normal">Back to normal</button>
      </div>
    </div>
    <div class="card">
      <h2>Account</h2>
      <div class="setrow"><div class="t" id="acct">—</div><button class="btn sm ghost" id="logoutBtn">Sign out</button></div>
      <div class="small muted" id="devinfo" style="margin-top:6px"></div>
    </div>
    <p class="disclaimer">Owlet Monitor <span id="ver"></span> · unofficial, uses Owlet's cloud through the open-source pyowletapi library.
      Not affiliated with Owlet. Not a medical device.</p>
  </section>
</div>

<nav>
  <button data-tab="monitor" class="on"><span class="i">◉</span>Monitor</button>
  <button data-tab="alarms"><span class="i">🔔</span>Alarms<span class="cnt" id="alarmCnt"></span></button>
  <button data-tab="log"><span class="i">☰</span>Log</button>
  <button data-tab="settings"><span class="i">⚙</span>Settings</button>
</nav>
<div id="toast"></div>

<script>
"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const api = async (path, opts = {}) => {
  const r = await fetch(path, { cache: "no-store", ...opts,
    headers: { "Content-Type": "application/json" },
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined });
  if (!r.ok) throw new Error((await r.text()) || r.status);
  return r.json();
};
const post = (path, body) => api(path, { method: "POST", body: body || {} });
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }
};
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("show"), 1600); }
const fmt = (v, d = 0) => (v === null || v === undefined || Number.isNaN(v)) ? "--" : Number(v).toFixed(d);
const clock = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

const NATIVE = window.OwletAndroid || null;   // present when running inside the Android app
const IN_APP = !!NATIVE;
let S = null;          // last state
let SET = null;        // settings
let HIST = { t: [], ox: [], hr: [], ox_lo: [], ox_hi: [], hr_lo: [], hr_hi: [], start: 0, end: 0, bucket: 0, stats: {} };
let tab = "monitor";
let rangeMin = store.get("range2", 60);
let serverOk = true;

/* ---------------- tabs ---------------- */
function showTab(name) {
  tab = name;
  $$("nav button").forEach(b => b.classList.toggle("on", b.dataset.tab === name));
  const needsLogin = S && S.status === "needs_login";
  $$("section.view").forEach(v => v.classList.remove("on"));
  $("#v-" + (needsLogin ? "login" : name)).classList.add("on");
  if (name === "log") loadEvents();
  if (name === "settings") loadHistInfo();
  if (name === "monitor") drawCharts();
}
$$("nav button").forEach(b => b.onclick = () => { unlockAudio(); showTab(b.dataset.tab); });

/* ---------------- sound ---------------- */
let AC = null, audioOk = false;
function unlockAudio() {
  try {
    if (!AC) AC = new (window.AudioContext || window.webkitAudioContext)();
    if (AC.state === "suspended") AC.resume();
    audioOk = AC.state === "running" || AC.state === "suspended";
  } catch (e) {}
}
document.addEventListener("pointerdown", unlockAudio, { capture: true });
function tone(freq, start, dur, type = "sine", vol = 0.35, freqEnd) {
  const o = AC.createOscillator(), g = AC.createGain();
  o.type = type; o.frequency.setValueAtTime(freq, AC.currentTime + start);
  if (freqEnd) o.frequency.linearRampToValueAtTime(freqEnd, AC.currentTime + start + dur);
  g.gain.setValueAtTime(0.0001, AC.currentTime + start);
  g.gain.exponentialRampToValueAtTime(vol, AC.currentTime + start + 0.02);
  g.gain.setValueAtTime(vol, AC.currentTime + start + dur - 0.05);
  g.gain.exponentialRampToValueAtTime(0.0001, AC.currentTime + start + dur);
  o.connect(g).connect(AC.destination); o.start(AC.currentTime + start); o.stop(AC.currentTime + start + dur + 0.05);
}
function playSound(kind) {
  if (IN_APP) return;   // the Android app plays alarms natively
  if (!AC || AC.state !== "running" || kind === "none") return;
  if (kind === "siren") { for (let i = 0; i < 3; i++) { tone(650, i * 0.7, 0.35, "square", 0.25, 1250); tone(1250, i * 0.7 + 0.35, 0.35, "square", 0.25, 650); } }
  else if (kind === "beep") { for (let i = 0; i < 3; i++) tone(880, i * 0.3, 0.18, "square", 0.2); }
  else if (kind === "chime") { tone(784, 0, 0.35, "sine", 0.35); tone(1047, 0.3, 0.6, "sine", 0.35); }
}
let lastSoundAt = 0;
const seenFired = new Set();
function alarmSounds() {
  if (!S || IN_APP) return;
  const now = Date.now();
  const crit = S.alarms.find(a => a.level === "critical" && !a.acked);
  if (crit && S.paused_for === 0) {
    if (now - lastSoundAt > 2400) { lastSoundAt = now; playSound(crit.sound);
      if (navigator.vibrate) navigator.vibrate([600, 300, 600]); }
  }
  for (const a of S.alarms) {   // warnings: sound once when they fire
    const id = a.key + ":" + a.fired_at;
    if (a.level === "warning" && !seenFired.has(id)) { seenFired.add(id); playSound(a.sound);
      if (navigator.vibrate) navigator.vibrate([300, 200, 300]); }
    if (a.level === "notify" && !seenFired.has(id)) { seenFired.add(id); playSound("chime"); }
  }
}

/* ---------------- wake lock ---------------- */
let wakeLock = null;
async function setWake(on) {
  store.set("wake", on);
  if (IN_APP) { try { NATIVE.keepScreenOn(!!on); } catch (e) {} return; }
  try {
    if (on && "wakeLock" in navigator) { wakeLock = await navigator.wakeLock.request("screen"); }
    else if (!on && wakeLock) { await wakeLock.release(); wakeLock = null; }
  } catch (e) { if (on) toast("Screen lock not available"); }
}
$("#wakeToggle").onchange = e => setWake(e.target.checked);
document.addEventListener("visibilitychange", () => { if (!document.hidden && store.get("wake", false)) setWake(true); });

/* ---------------- render: header, banners, overlay ---------------- */
function statusInfo() {
  if (!serverOk) return ["crit", "App stopped"];
  if (!S) return ["", "Starting…"];
  const age = S.rates.seconds_since_new;
  if (S.status === "needs_login") return ["warn", "Sign in"];
  if (S.status === "connecting" || S.status === "starting") return ["warn", "Connecting…"];
  if (S.status === "error") return ["crit", "Connection problem"];
  if (S.device.connection_status && S.device.connection_status !== "Online") return ["crit", "Base " + S.device.connection_status];
  if (age === null) return ["warn", "Waiting for data"];
  if (age > 30) return ["warn", `No new data ${Math.round(age)}s`];
  return ["good", `Live · ${Math.round(age)}s ago`];
}
function renderHeader() {
  const [cls, txt] = statusInfo();
  $("#statusdot").className = "dot " + cls;
  $("#statustext").textContent = txt;
  if (S) $("#devname").textContent = (S.demo ? "DEMO · " : "") + (S.device.name || "Owlet Monitor");
}
function renderBanners() {
  const out = [];
  if (!serverOk) out.push(IN_APP ? `<div class="banner warn"><div class="msg"><b>Monitor is starting…</b>If this doesn't go away, close and reopen the app.</div></div>`
    : `<div class="banner crit"><div class="msg"><b>The app isn't running</b>Open Termux and start it again: python owlet_monitor.py</div></div>`);
  if (!IN_APP && (S && AC && AC.state !== "running" || (S && !AC))) out.push(`<div class="banner info"><div class="msg"><b>Tap anywhere to enable alarm sounds</b>Browsers only play sound after a tap.</div></div>`);
  if (S && S.paused_for > 0) out.push(`<div class="banner warn"><div class="msg"><b>All alarms paused</b>${Math.ceil(S.paused_for / 60)} min left</div><button class="btn sm" onclick="pauseAll(0)">Resume</button></div>`);
  if (S && S.status === "error" && S.error) out.push(`<div class="banner crit"><div class="msg"><b>Can't reach Owlet</b>${esc(S.error)} — retrying…</div></div>`);
  if (S) for (const a of S.alarms) {
    if (a.level === "critical" && !a.acked) continue; // shown in overlay
    const cls = a.level === "critical" ? "crit" : a.level;
    out.push(`<div class="banner ${cls}"><div class="msg"><b>${a.level === "critical" ? "⚠️" : a.level === "warning" ? "⚠" : "ℹ"} ${esc(a.label)}${a.acked ? " · acknowledged" : ""}</b>${esc(a.message)}</div>
      ${a.acked ? "" : `<button class="btn sm" onclick="ack('${a.key}')">OK</button>`}
      <button class="btn sm ghost" onclick="snooze('${a.key}',10)">Snooze</button></div>`);
  }
  $("#banners").innerHTML = out.join("");
  const crit = S && S.alarms.find(a => a.level === "critical" && !a.acked) && S.paused_for === 0
    ? S.alarms.find(a => a.level === "critical" && !a.acked) : null;
  $("#overlay").classList.toggle("show", !!crit);
  if (crit) { $("#ovTitle").textContent = crit.label; $("#ovMsg").textContent = crit.message;
    $("#ovAck").onclick = () => ack(crit.key); $("#ovSnooze").onclick = () => snooze(crit.key, 10); }
  const n = S ? S.alarms.filter(a => !a.acked).length : 0;
  $("#alarmCnt").style.display = n ? "block" : "none"; $("#alarmCnt").textContent = n;
}
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
async function ack(key) { await post("/api/ack/" + key).catch(() => {}); toast("Acknowledged"); poll(); }
async function snooze(key, m) { await post(`/api/snooze/${key}?minutes=${m}`).catch(() => {}); toast(`Snoozed ${m} min`); poll(); }
async function pauseAll(m) { await post("/api/pause", { minutes: m }).catch(() => {}); toast(m ? `Alarms paused ${m} min` : "Alarms on"); poll(); }

/* ---------------- render: monitor ---------------- */
function setRing(arcId, value, lo, hi) {
  const frac = value === null || value === undefined ? 0 : Math.max(0, Math.min(1, (value - lo) / (hi - lo)));
  $(arcId).setAttribute("stroke-dashoffset", (276.5 * (1 - frac)).toFixed(1));
}
function vitalState(kind, v) {
  if (!SET || v === null || v === undefined) return ["", "—"];
  const A = SET.alarms;
  if (kind === "ox") {
    if (A.low_oxygen.enabled && v < A.low_oxygen.threshold) return ["crit", "⚠ Below " + A.low_oxygen.threshold + "%"];
    return ["good", "✓ Normal"];
  }
  if (A.high_heart_rate.enabled && v > A.high_heart_rate.threshold) return ["crit", "⚠ Above " + A.high_heart_rate.threshold];
  if (A.low_heart_rate.enabled && v < A.low_heart_rate.threshold) return ["crit", "⚠ Below " + A.low_heart_rate.threshold];
  return ["good", "✓ Normal"];
}
function renderMonitor() {
  if (!S) return;
  const p = S.props || {}, a = S.avg || {};
  const sockOn = p.oxygen_saturation > 0 && !p.sock_off && !p.sock_disconnected && !p.charging;
  const fresh = S.rates.seconds_since_new !== null && S.rates.seconds_since_new < 60;
  const useAvg = SET ? SET.alarms_use_average : true;
  const showOx = useAvg ? a.ox : (sockOn && fresh ? p.oxygen_saturation : null);
  const showHr = useAvg ? a.hr : (sockOn && fresh ? p.heart_rate : null);
  $("#oxNum").textContent = fmt(showOx); $("#hrNum").textContent = fmt(showHr);
  setRing("#oxArc", showOx, 80, 100); setRing("#hrArc", showHr, 40, 240);
  const live = (v) => sockOn && fresh ? fmt(v) : "--";
  $("#oxSub").textContent = useAvg ? `avg of ${a.n || 0} · live ${live(p.oxygen_saturation)}` : `live · avg ${fmt(a.ox)}`;
  $("#hrSub").textContent = useAvg ? `avg of ${a.n || 0} · live ${live(p.heart_rate)}` : `live · avg ${fmt(a.hr)}`;
  for (const [k, v] of [["ox", showOx], ["hr", showHr]]) {
    const [cls, txt] = vitalState(k, v);
    const b = $("#" + k + "Badge"); b.className = "badge " + cls; b.textContent = txt;
  }
  // chips
  let sock = "—", sockCls = "";
  if (p.charging) { sock = "Charging"; }
  else if (p.sock_off || p.sock_disconnected) { sock = p.sock_off ? "Off" : "Disconnected"; sockCls = "warn"; }
  else if (Object.keys(p).length) { sock = "On"; sockCls = "good"; }
  const bat = p.battery_percentage, mins = p.battery_minutes;
  const batTxt = bat === undefined ? "—" : `${fmt(bat)}%` + (mins && !p.charging ? ` · ${Math.floor(mins / 60)}h` : "");
  const chips = [
    ["Sock", sock, sockCls],
    ["Base", S.monitoring ? "Monitoring" : "Off", S.monitoring ? "good" : "warn"],
    ["Battery", batTxt, bat !== undefined && bat < 20 && !p.charging ? "warn" : ""],
    ["Signal", p.signal_strength !== undefined ? fmt(p.signal_strength) : "—", ""],
    ["Skin temp", p.skin_temperature ? fmt(p.skin_temperature) + "°C" : "—", ""],
    ["Movement", p.movement !== undefined ? fmt(p.movement) : "—", ""],
    ["Owlet 10-min O₂", p.oxygen_10_av ? fmt(p.oxygen_10_av) + "%" : "—", ""],
    ["Cloud", S.device.connection_status || "—", S.device.connection_status === "Online" ? "good" : "crit"],
    ["Firmware", p.firmware_update_available ? "Update ready" : (Object.keys(p).length ? "Up to date" : "—"), p.firmware_update_available ? "warn" : ""],
  ];
  $("#chips").innerHTML = chips.map(([k, v, c]) => `<div class="chip"><div class="k">${k}</div><div class="v ${c}">${esc(v)}</div></div>`).join("");
  const r = S.rates;
  $("#rate").innerHTML = `New reading every <b>${r.seconds_between_readings ?? "–"} s</b> from the base · asked ${r.polls_per_min}× in the last minute · last request ${r.last_poll_ms} ms`
    + (p.last_updated ? ` · base time ${esc(p.last_updated)} UTC` : "")
    + (S.fast_path === false ? " · (fast mode off, using full reads)" : "");
  // pending
  $("#pending").innerHTML = (S.pending || []).filter(x => x.in > 0).map(x =>
    `<div class="pend">⏱ ${esc(x.label)} alarm in ${x.in}s if it doesn't recover</div>`).join("");
}

/* ---------------- charts ---------------- */
function drawChart(canvasId, tipId, key, color, unit, thresholds, fixedMin, fixedMax) {
  const cv = $(canvasId); if (!cv || !cv.offsetWidth) return;
  const dpr = window.devicePixelRatio || 1, W = cv.offsetWidth, H = cv.offsetHeight;
  cv.width = W * dpr; cv.height = H * dpr;
  const g = cv.getContext("2d"); g.scale(dpr, dpr); g.clearRect(0, 0, W, H);
  const live = rangeMin > 0 && rangeMin <= 1440;
  const t1 = live ? Date.now() / 1000 : (HIST.end || Date.now() / 1000);
  const t0 = rangeMin > 0 ? t1 - rangeMin * 60 : (HIST.start || t1 - 3600);
  const pts = [];
  for (let i = 0; i < HIST.t.length; i++) {
    const v = HIST[key][i];
    if (HIST.t[i] >= t0 && v != null) pts.push([HIST.t[i], v, HIST[key + "_lo"][i] ?? v, HIST[key + "_hi"][i] ?? v]);
  }
  let lo = fixedMin, hi = fixedMax;
  for (const p of pts) { lo = Math.min(lo, p[2]); hi = Math.max(hi, p[3]); }
  for (const th of thresholds) { lo = Math.min(lo, th - 2); hi = Math.max(hi, th + 2); }
  const pad = (hi - lo) * 0.08 || 1; lo -= pad; hi += pad;
  const L = 34, R = 8, T = 8, B = 20;
  const x = t => L + (t - t0) / ((t1 - t0) || 1) * (W - L - R);
  const y = v => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
  g.font = "11px system-ui, sans-serif"; g.fillStyle = "#6f8a85"; g.strokeStyle = "#1f3532"; g.lineWidth = 1;
  const step = niceStep((hi - lo) / 3);
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) {
    g.beginPath(); g.moveTo(L, Math.round(y(v)) + .5); g.lineTo(W - R, Math.round(y(v)) + .5); g.stroke();
    g.textAlign = "right"; g.textBaseline = "middle"; g.fillText(Math.round(v), L - 6, y(v));
  }
  g.textBaseline = "top";
  const longSpan = (t1 - t0) > 2 * 86400;
  for (const f of [0, 0.5, 1]) {
    const t = t0 + f * (t1 - t0), d = new Date(t * 1000);
    let label = longSpan ? d.toLocaleDateString([], { day: "numeric", month: "short" })
                         : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    if (f === 1 && live) label = "now";
    g.textAlign = f === 0 ? "left" : f === 1 ? "right" : "center";
    g.fillText(label, f === 0 ? L : f === 1 ? W - R : x(t), H - B + 5);
  }
  g.setLineDash([5, 4]); g.strokeStyle = "#ff4d4f99"; g.lineWidth = 1.5;
  for (const th of thresholds) { g.beginPath(); g.moveTo(L, y(th)); g.lineTo(W - R, y(th)); g.stroke(); }
  g.setLineDash([]);
  const gap = Math.max(30, (HIST.bucket || 0) * 3);
  // min-max band
  if (HIST.bucket) {
    g.fillStyle = color + "33";
    let seg = [];
    const flush = () => { if (seg.length > 1) { g.beginPath();
      seg.forEach((p, i) => i ? g.lineTo(x(p[0]), y(p[3])) : g.moveTo(x(p[0]), y(p[3])));
      for (let i = seg.length - 1; i >= 0; i--) g.lineTo(x(seg[i][0]), y(seg[i][2]));
      g.closePath(); g.fill(); } seg = []; };
    let prevT = null;
    for (const p of pts) { if (prevT !== null && p[0] - prevT > gap) flush(); seg.push(p); prevT = p[0]; }
    flush();
  }
  g.strokeStyle = color; g.lineWidth = 2; g.lineJoin = "round"; g.lineCap = "round"; g.beginPath();
  let prev = null;
  for (const [t, v] of pts) { if (prev === null || t - prev > gap) g.moveTo(x(t), y(v)); else g.lineTo(x(t), y(v)); prev = t; }
  g.stroke();
  if (pts.length) {
    const [t, v] = pts[pts.length - 1];
    g.fillStyle = color; g.beginPath(); g.arc(x(t), y(v), 4, 0, 7); g.fill();
    g.strokeStyle = "#13201f"; g.lineWidth = 2; g.stroke();
  } else {
    g.fillStyle = "#6f8a85"; g.textAlign = "center"; g.textBaseline = "middle";
    g.fillText("No readings in this period", (L + W - R) / 2, (T + H - B) / 2);
  }
  const st = HIST.stats || {};
  const a = st[key + "_avg"], mn = st[key + "_min"], mx = st[key + "_max"];
  $(canvasId.replace("Chart", "Range")).textContent = st.n ? `avg ${fmt(a)} · min ${fmt(mn)} · max ${fmt(mx)} ${unit}` : "";
  cv._chart = { pts, x, y, color, unit, tipId, L, R, W, T, B, H, bucket: HIST.bucket };
}
function niceStep(raw) { const p = Math.pow(10, Math.floor(Math.log10(raw || 1))); const n = raw / p;
  return (n < 1.5 ? 1 : n < 3.5 ? 2 : n < 7.5 ? 5 : 10) * p; }
function hover(cv, clientX) {
  const c = cv._chart; if (!c || !c.pts.length) return;
  const rect = cv.getBoundingClientRect(), px = clientX - rect.left;
  let best = null, bd = 1e9;
  for (const p of c.pts) { const d = Math.abs(c.x(p[0]) - px); if (d < bd) { bd = d; best = p; } }
  if (!best || bd > 40) return unhover(cv);
  drawCharts(true);
  const g = cv.getContext("2d"), X = c.x(best[0]), Y = c.y(best[1]);
  g.strokeStyle = "#a9bfbb88"; g.lineWidth = 1; g.beginPath(); g.moveTo(X, c.T); g.lineTo(X, c.H - c.B); g.stroke();
  g.fillStyle = c.color; g.beginPath(); g.arc(X, Y, 5, 0, 7); g.fill(); g.strokeStyle = "#13201f"; g.lineWidth = 2; g.stroke();
  const tip = $(c.tipId); tip.style.display = "block";
  const when = c.bucket > 43200 || (rangeMin === 0 || rangeMin > 1440)
    ? new Date(best[0] * 1000).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : clock(best[0]);
  tip.innerHTML = c.bucket ? `<b>${best[1].toFixed(0)} ${c.unit}</b> avg · ${best[2].toFixed(0)}–${best[3].toFixed(0)} · ${when}`
                           : `<b>${best[1].toFixed(0)} ${c.unit}</b> · ${when}`;
  tip.style.left = Math.max(60, Math.min(c.W - 60, X)) + "px"; tip.style.top = "-6px";
  cv._hovering = true;
}
function unhover(cv) { $(cv._chart?.tipId || "#oxTip").style.display = "none"; cv._hovering = false; drawCharts(true); }
for (const id of ["#oxChart", "#hrChart"]) {
  const cv = $(id);
  cv.addEventListener("pointermove", e => hover(cv, e.clientX));
  cv.addEventListener("pointerdown", e => hover(cv, e.clientX));
  cv.addEventListener("pointerleave", () => unhover(cv));
  cv.addEventListener("pointerup", e => { if (e.pointerType !== "mouse") setTimeout(() => unhover(cv), 1800); });
}
function drawCharts(force) {
  if (tab !== "monitor") return;
  if (!force && ($("#oxChart")._hovering || $("#hrChart")._hovering)) return;
  const A = SET ? SET.alarms : null;
  const oxT = A && A.low_oxygen.enabled ? [A.low_oxygen.threshold] : [];
  const hrT = A ? [A.high_heart_rate.enabled ? A.high_heart_rate.threshold : null,
                   A.low_heart_rate.enabled ? A.low_heart_rate.threshold : null].filter(v => v !== null) : [];
  drawChart("#oxChart", "#oxTip", "ox", "#3cc6b4", "%", oxT, 94, 100);
  drawChart("#hrChart", "#hrTip", "hr", "#f08a7a", "bpm", hrT, 100, 150);
}
$$("#rangeSeg button").forEach(b => { b.classList.toggle("on", +b.dataset.m === rangeMin);
  b.onclick = () => { rangeMin = +b.dataset.m; store.set("range", rangeMin);
    $$("#rangeSeg button").forEach(x => x.classList.toggle("on", x === b)); pollHistory(); }; });
window.addEventListener("resize", () => drawCharts(true));

/* ---------------- alarms editor ---------------- */
const ORIG = {
  low_oxygen: "Red alert when oxygen goes <b>below 80%</b> (Dream Sock and Smart Sock 3). Fixed, you can't change it. Owlet publishes no delay.",
  high_heart_rate: "Red alert when heart rate goes <b>above 220 bpm</b>. Fixed.",
  low_heart_rate: "Dream Sock: red alert <b>below 50 bpm</b>. The older Smart Sock 3 alerted below 60 bpm only when oxygen was also below 85%, to avoid false alarms. Fixed.",
  owlet_alerts: "These are the red alerts the Owlet base raises itself, using the limits above. The base flashes red and sounds; the app sends a push notification. Turning this on repeats them on this phone.",
  sock_off: "Yellow alert when the sock is off or badly placed and gets no good readings for <b>at least 60 seconds</b>.",
  no_data: "Yellow alert when the sock is out of the base's range or the signal is blocked for <b>at least 60 seconds</b>. The Owlet app has no alarm for your phone losing contact with Owlet's servers. That part is extra here.",
  base_offline: "Newer Owlet bases raise a <b>power loss</b> alert (not on base model OBL 3.0) and a <b>connection issue</b> alert.",
  low_battery: "Blue (low priority) alert when the sock battery has about <b>50 minutes</b> left. Owlet uses minutes; this app uses percent.",
};
const LEVEL_TXT = { critical: "Critical – repeats until acknowledged", warning: "Warning – sounds once", notify: "Notification only" };
const SOUND_TXT = { siren: "Siren", beep: "Beeps", chime: "Chime", none: "Silent" };
function renderAlarmEditor() {
  if (!SET) return;
  const list = $("#alarmList");
  const html = Object.entries(SET.alarms).map(([key, a]) => {
    const hasSched = !!a.schedule;
    const [from, to] = hasSched ? a.schedule.split("-") : ["20:00", "07:00"];
    const snoozed = S && S.snoozed && S.snoozed[key] ? `<span class="badge warn">Snoozed ${Math.ceil(S.snoozed[key] / 60)}m</span>` : "";
    return `<div class="alarm ${a.enabled ? "" : "off"}" data-key="${key}">
      <div class="top"><b>${esc(a.label)}</b>${snoozed}
        <label class="switch"><input type="checkbox" data-a="enabled" ${a.enabled ? "checked" : ""}><span></span></label></div>
      <div class="help">${esc(a.help)}</div>
      <div class="fields">
        ${a.threshold !== null ? `<div class="field"><label>${key === "no_data" ? "After" : "Threshold"}</label>
          <div class="inp"><input type="number" inputmode="decimal" data-a="threshold" value="${a.threshold}"><span>${esc(a.unit)}</span></div></div>` : ""}
        ${key !== "no_data" ? `<div class="field"><label>Trigger after</label>
          <div class="inp"><input type="number" inputmode="numeric" min="0" data-a="delay" value="${a.delay}"><span>s</span></div></div>` : ""}
        <div class="field full"><label>Type</label><select data-a="level">
          ${Object.entries(LEVEL_TXT).map(([v, t]) => `<option value="${v}" ${a.level === v ? "selected" : ""}>${t}</option>`).join("")}</select></div>
        <div class="field"><label>Sound in browser</label><select data-a="sound">
          ${Object.entries(SOUND_TXT).map(([v, t]) => `<option value="${v}" ${a.sound === v ? "selected" : ""}>${t}</option>`).join("")}</select></div>
        <div class="field"><label>Active</label><select data-a="schedMode">
          <option value="always" ${hasSched ? "" : "selected"}>Always</option>
          <option value="hours" ${hasSched ? "selected" : ""}>Only between…</option></select></div>
        <div class="field full sched" style="display:${hasSched ? "flex" : "none"}">
          <input type="time" data-a="from" value="${from}"><span class="muted">to</span><input type="time" data-a="to" value="${to}"></div>
      </div>
      <div class="row" style="margin-top:10px"><span class="spacer"></span><button class="btn sm ghost" data-test="${key}">Test this alarm</button></div>
      ${ORIG[key] ? `<details class="orig" ${openNotes.has(key) ? "open" : ""} data-note="${key}"><summary>ⓘ How the Owlet app does this</summary><div class="body">${ORIG[key]}</div></details>` : ""}
    </div>`;
  }).join("");
  if (list._html !== html) {
    if (list.contains(document.activeElement)) return; // don't redraw under the user's finger
    list.innerHTML = html; list._html = html;
  }
}
const openNotes = new Set();
$("#alarmList").addEventListener("toggle", e => {
  const k = e.target.dataset && e.target.dataset.note; if (!k) return;
  if (e.target.open) openNotes.add(k); else openNotes.delete(k);
}, true);
$("#alarmList").addEventListener("change", e => {
  const card = e.target.closest(".alarm"); if (!card) return;
  const key = card.dataset.key, f = e.target.dataset.a;
  const patch = {};
  if (f === "enabled") patch.enabled = e.target.checked;
  else if (f === "threshold" || f === "delay") patch[f] = parseFloat(e.target.value);
  else if (f === "level" || f === "sound") patch[f] = e.target.value;
  if (f === "sound") { unlockAudio(); playSound(e.target.value); }
  if (f === "schedMode" || f === "from" || f === "to") {
    const mode = $("[data-a=schedMode]", card).value;
    $(".sched", card).style.display = mode === "hours" ? "flex" : "none";
    patch.schedule = mode === "hours" ? `${$("[data-a=from]", card).value}-${$("[data-a=to]", card).value}` : "";
  }
  saveSettings({ alarms: { [key]: patch } });
});
$("#alarmList").addEventListener("click", async e => {
  const k = e.target.dataset.test; if (!k) return;
  unlockAudio(); playSound(SET.alarms[k].sound);
  if (navigator.vibrate) navigator.vibrate([400, 200, 400]);
  await post("/api/test/" + k).catch(() => {});
  toast(S && S.phone_alarms ? "Test sent to phone too" : "Test played (browser only)");
});
$("#resetAlarms").onclick = async () => { if (!confirmTwice()) return; SET = await post("/api/settings/reset");
  $("#alarmList")._html = ""; renderAlarmEditor(); renderSettingInputs(); toast("Alarms reset"); };
let _confirmAt = 0;
function confirmTwice() { if (Date.now() - _confirmAt < 3000) return true; _confirmAt = Date.now(); toast("Tap again to confirm"); return false; }

function renderPauseRow() {
  const p = S ? S.paused_for : 0;
  $("#pauseRow").innerHTML = p > 0
    ? `<span class="badge warn">Paused · ${Math.ceil(p / 60)} min left</span><span class="spacer"></span><button class="btn sm primary" onclick="pauseAll(0)">Resume now</button>`
    : `<span class="small muted">Pause everything for</span><span class="spacer"></span>
       <button class="btn sm" onclick="pauseAll(15)">15m</button><button class="btn sm" onclick="pauseAll(30)">30m</button><button class="btn sm" onclick="pauseAll(60)">1h</button>`;
  $("#phoneWarn").innerHTML = S && !S.phone_alarms ? `<div class="banner warn"><div class="msg"><b>Phone alarms are off</b>Without the Termux:API app, alarms only sound while this page is open. Install Termux:API from F-Droid, then run <code>pkg install termux-api</code> and restart the app.</div></div>` : "";
}

/* ---------------- generic settings inputs ---------------- */
function getPath(o, p) { return p.split(".").reduce((a, k) => a && a[k], o); }
function setPath(p, v) { const parts = p.split("."), out = {}; let cur = out;
  parts.forEach((k, i) => { if (i === parts.length - 1) cur[k] = v; else cur = cur[k] = {}; }); return out; }
function renderSettingInputs() {
  if (!SET) return;
  for (const el of $$("[data-set]")) {
    if (el === document.activeElement) continue;
    const v = getPath(SET, el.dataset.set);
    if (el.type === "checkbox") el.checked = !!v; else el.value = v;
  }
}
document.addEventListener("change", e => {
  const el = e.target; if (!el.dataset.set) return;
  const v = el.type === "checkbox" ? el.checked : parseFloat(el.value);
  saveSettings(setPath(el.dataset.set, v));
});
let saveQ = {}, saveT = null;
function merge(a, b) { for (const k in b) { if (b[k] && typeof b[k] === "object" && !Array.isArray(b[k])) a[k] = merge(a[k] || {}, b[k]); else a[k] = b[k]; } return a; }
function saveSettings(patch) {
  merge(saveQ, patch); clearTimeout(saveT);
  saveT = setTimeout(async () => {
    const body = saveQ; saveQ = {};
    try { SET = await post("/api/settings", body); toast("Saved"); renderSettingInputs(); $("#alarmList")._html = ""; renderAlarmEditor(); drawCharts(true); }
    catch (e) { toast("Couldn't save"); }
  }, 350);
}

/* ---------------- log ---------------- */
const EV_ICON = { alarm: "🔴", clear: "🟢", ack: "✔", snooze: "💤", pause: "⏸", test: "🔔", info: "ℹ" };
async function loadEvents() {
  try {
    const evs = await api("/api/events");
    $("#events").innerHTML = evs.length ? evs.map(e => {
      const d = new Date(e.t * 1000), today = new Date().toDateString() === d.toDateString();
      return `<div class="ev"><div class="tm">${today ? "" : d.toLocaleDateString([], { day: "numeric", month: "short" }) + " "}${clock(e.t)}</div>
        <div class="ic">${EV_ICON[e.kind] || "•"}</div><div>${esc(e.message)}</div></div>`;
    }).join("") : `<div class="muted small">No events yet.</div>`;
  } catch (e) {}
}

/* ---------------- login / account ---------------- */
$("#lgBtn").onclick = async () => {
  const user = $("#lgUser").value.trim(), password = $("#lgPass").value, region = $("#lgRegion").value;
  if (!user || !password) return toast("Enter email and password");
  $("#lgBtn").disabled = true;
  try { await post("/api/login", { user, password, region }); toast("Signing in…"); $("#lgPass").value = ""; }
  catch (e) { toast("Sign-in failed"); }
  $("#lgBtn").disabled = false;
};
$("#logoutBtn").onclick = async () => { if (!confirmTwice()) return; await post("/api/logout"); poll(); };
$("#demoToggle").onchange = e => { try { NATIVE.setDemo(e.target.checked); } catch (err) {} toast("Restarting…"); };
$("#battBtn").onclick = () => { try { NATIVE.openBatterySettings(); } catch (e) {} };
$("#notifBtn").onclick = () => { try { NATIVE.openNotificationSettings(); } catch (e) {} };
$$("[data-demo]").forEach(b => b.onclick = async () => { unlockAudio(); await post("/api/demo/" + b.dataset.demo); toast("Simulating: " + b.textContent); });

/* ---------------- loops ---------------- */
let lastStatus = null;
async function poll() {
  try {
    S = await api("/api/state"); serverOk = true;
  } catch (e) { serverOk = false; }
  if (S && S.status !== lastStatus) {
    const wasLogin = lastStatus === "needs_login" || S.status === "needs_login";
    lastStatus = S.status; if (wasLogin) showTab(tab);
    if (S.status === "error" && S.error && /auth|password|credential/i.test(S.error)) toast("Owlet rejected the login");
  }
  renderHeader(); renderBanners();
  if (S) {
    renderMonitor(); renderPauseRow();
    $("#demoCard").style.display = S.demo ? "block" : "none";
    if (IN_APP) { $("#appCard").style.display = "block"; $("#demoToggle").checked = !!S.demo; }
    $("#acct").innerHTML = S.demo ? "Demo mode" : `Signed in as <b>${esc(S.account || "—")}</b>`;
    $("#devinfo").textContent = S.device.serial ? `${S.device.name} · serial ${S.device.serial} · base IP ${S.device.lan_ip || "?"}` : "";
    $("#ver").textContent = "v" + S.version;
    if (tab === "alarms") renderAlarmEditor();
    alarmSounds();
  }
}
let histBusy = false, histAt = 0;
async function pollHistory() {
  if (histBusy) return; histBusy = true;
  try { HIST = await api("/api/history?minutes=" + rangeMin); histAt = Date.now(); drawCharts(true); } catch (e) {}
  histBusy = false;
}
function maybeHistory() {   // short ranges refresh often, long ranges every 30 s
  const every = rangeMin > 0 && rangeMin <= 60 ? 4000 : rangeMin > 0 && rangeMin <= 1440 ? 15000 : 30000;
  if (tab === "monitor" && Date.now() - histAt > every) pollHistory();
}
async function loadHistInfo() {
  try {
    const i = await api("/api/history/info");
    const mb = (i.bytes / 1048576).toFixed(1);
    $("#histInfo").textContent = i.count ? `${i.count.toLocaleString()} readings saved since ${new Date(i.first * 1000).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })} · ${mb} MB on this phone`
      : "No readings saved yet.";
  } catch (e) {}
}
$("#clearHist").onclick = async () => { if (!confirmTwice()) return; await post("/api/history/clear"); toast("History deleted"); loadHistInfo(); pollHistory(); };
$("#owletPreset").onclick = async () => { if (!confirmTwice()) return; SET = await post("/api/settings/preset/owlet");
  $("#alarmList")._html = ""; renderAlarmEditor(); renderSettingInputs(); drawCharts(true); toast("Owlet's thresholds applied"); };
async function boot() {
  try { SET = await api("/api/settings"); } catch (e) {}
  $("#wakeToggle").checked = store.get("wake", false);
  if (store.get("wake", false)) setWake(true);
  renderSettingInputs();
  await poll(); showTab("monitor"); renderAlarmEditor(); pollHistory();
  setInterval(poll, 1000); setInterval(maybeHistory, 1000);
  setInterval(() => { if (tab === "settings") loadHistInfo(); }, 10000);
  setInterval(() => { if (tab === "log") loadEvents(); }, 5000);
}
boot();
</script>
</body>
</html>
'''


class HttpServer:
    def __init__(self, monitor):
        self.m = monitor

    async def handle(self, reader, writer):
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
            lines = head.decode("latin-1").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = {}
            for ln in lines[1:]:
                if ":" in ln:
                    k, v = ln.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            body = b""
            n = int(headers.get("content-length", "0") or 0)
            if n:
                body = await asyncio.wait_for(reader.readexactly(min(n, 100_000)), timeout=10)
            status, ctype, payload = await self.route(method, target, body)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
            writer.close()
            return
        except Exception as e:
            traceback.print_exc()
            status, ctype, payload = 500, "application/json", json.dumps({"error": str(e)}).encode()
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 500: "Server Error"}.get(status, "OK")
        extra = ""
        if ctype.startswith("text/csv"):
            extra = f"Content-Disposition: attachment; filename=owlet_history_{datetime.now():%Y%m%d_%H%M}.csv\r\n"
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\nContent-Type: {ctype}\r\nContent-Length: {len(payload)}\r\n{extra}"
            f"Cache-Control: no-store\r\nConnection: close\r\n\r\n".encode() + payload)
        try:
            await writer.drain()
        except ConnectionError:
            pass
        writer.close()

    async def route(self, method, target, body):
        m = self.m
        u = urlparse(target)
        path, q = u.path.rstrip("/") or "/", parse_qs(u.query)

        def ok(obj=None):
            return 200, "application/json", json.dumps(obj if obj is not None else {"ok": True}).encode()

        def data():
            try:
                return json.loads(body or b"{}")
            except Exception:
                return {}

        if method == "GET":
            if path in ("/", "/index.html"):
                return 200, "text/html; charset=utf-8", UI_HTML.encode()
            if path == "/manifest.json":
                return ok({"name": "Owlet Monitor", "short_name": "Owlet", "start_url": "/",
                           "display": "standalone", "background_color": "#0b1416",
                           "theme_color": "#0b1416"})
            if path == "/favicon.ico":
                return 200, "image/svg+xml", b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><circle cx="8" cy="8" r="7" fill="#3cc6b4"/></svg>'
            if path == "/api/state":
                return ok(m.state())
            if path == "/api/history":
                return ok(await m.history(_num((q.get("minutes") or [60])[0], 0, 10_000_000, 60)))
            if path == "/api/history/info":
                return ok(await asyncio.to_thread(m.store.info_sync))
            if path == "/api/export.csv":
                minutes = _num((q.get("minutes") or [0])[0], 0, 10_000_000, 0)
                csv = await asyncio.to_thread(m.store.csv_sync, minutes)
                return 200, "text/csv; charset=utf-8", csv.encode()
            if path == "/api/settings":
                return ok(m.settings)
            if path == "/api/events":
                return ok(list(m.events)[-200:][::-1])
            return 404, "application/json", b'{"error":"not found"}'

        if method == "POST":
            parts = path.strip("/").split("/")
            if path == "/api/settings":
                return ok(m.update_settings(data()))
            if path == "/api/settings/reset":
                return ok(m.update_settings({"alarms": copy.deepcopy(DEFAULT_ALARMS)}))
            if path == "/api/settings/preset/owlet":
                return ok(m.update_settings({"alarms": copy.deepcopy(OWLET_PRESET)}))
            if path == "/api/history/clear":
                m.store.clear()
                m.readings.clear()
                m.log("info", "", "History deleted")
                return ok()
            if path == "/api/login":
                d = data()
                if not d.get("user") or not d.get("password"):
                    return 400, "application/json", b'{"error":"email and password required"}'
                m.login(d["user"], d["password"], d.get("region", "europe"))
                return ok()
            if path == "/api/logout":
                m.logout()
                return ok()
            if path == "/api/ack":
                m.ack()
                return ok()
            if len(parts) == 3 and parts[:2] == ["api", "ack"]:
                m.ack(parts[2])
                return ok()
            if len(parts) == 3 and parts[:2] == ["api", "snooze"]:
                m.snooze(parts[2], _num((q.get("minutes") or [10])[0], 1, 240, 10))
                return ok()
            if path == "/api/pause":
                m.pause(_num(data().get("minutes", 0), 0, 720, 0))
                return ok()
            if len(parts) == 3 and parts[:2] == ["api", "test"]:
                m.test_alarm(parts[2])
                return ok()
            if len(parts) == 3 and parts[:2] == ["api", "demo"] and m.demo and m.source:
                m.source.set_scenario(parts[2])
                return ok()
            return 404, "application/json", b'{"error":"not found"}'
        return 400, "application/json", b'{"error":"bad method"}'


START_TIME = time.time()


async def serve(demo=False, port=8080, host="127.0.0.1", open_browser=True, phone_alarms_cls=None):
    """Start the monitor + web UI. Used by the command line and by the Android app."""
    global EVENTS_FILE, HISTORY_DB, PhoneAlarms
    if phone_alarms_cls is not None:
        PhoneAlarms = phone_alarms_cls
    if demo:  # keep fake data out of your real history and log
        EVENTS_FILE = APP_DIR / "events_demo.jsonl"
        HISTORY_DB = APP_DIR / "history_demo.db"
    monitor = Monitor(demo, port)
    server = await asyncio.start_server(HttpServer(monitor).handle, host, port)
    url = f"http://localhost:{port}/"
    print(f"Owlet Monitor {APP_VERSION}{' (DEMO)' if demo else ''} running at {url}")
    print("Phone alarms:", "available" if monitor.phone.available else
          "NOT available - install the Termux:API app and run: pkg install termux-api")
    if shutil.which("termux-wake-lock"):
        print("Keep this Termux session open. Press Ctrl+C to stop.")
        os.system("termux-wake-lock")
    if open_browser and shutil.which("termux-open-url"):
        os.system(f"termux-open-url {url}")
    async with server:
        await asyncio.gather(server.serve_forever(), monitor.run())


async def main():
    ap = argparse.ArgumentParser(description="Owlet Monitor")
    ap.add_argument("--demo", action="store_true", help="fake data, no account needed")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1",
                    help="127.0.0.1 = this phone only, 0.0.0.0 = any device on your Wi-Fi")
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    args = ap.parse_args()
    await serve(args.demo, args.port, args.host, not args.no_browser)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
        if shutil.which("termux-wake-unlock"):
            os.system("termux-wake-unlock")
