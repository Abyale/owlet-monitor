"""Entry point used by the Android app (called from MonitorService.kt through Chaquopy)."""
import asyncio
import os
import time
from datetime import datetime, timedelta, timezone


def run(files_dir, port, demo=False):
    # Must be set before owlet_monitor is imported: that's where its data folder is decided.
    os.environ["OWLET_HOME"] = str(files_dir)
    import owlet_monitor as om
    from java import jclass

    Bridge = jclass("com.abyale.owletmonitor.AlarmBridge")
    TimeZone = jclass("java.util.TimeZone")

    def local_dt(ts):
        # Android's Python doesn't know the phone's time zone, so ask Java.
        offset_ms = TimeZone.getDefault().getOffset(int(ts * 1000))
        return (datetime.fromtimestamp(ts, timezone.utc) + timedelta(milliseconds=offset_ms)).replace(tzinfo=None)

    om.local_dt = local_dt
    om.local_now = lambda: local_dt(time.time())

    class AndroidAlarms:
        def __init__(self, port):
            self.port = port
            self.available = True

        def fire(self, key, alarm, message, phone):
            Bridge.fire(str(key), str(alarm["label"]), str(message), str(alarm["level"]),
                        bool(phone.get("notification", True)), bool(phone.get("vibrate", True)),
                        bool(phone.get("speak", True)), bool(phone.get("max_volume", True)))

        def sound(self, alarm, phone):
            Bridge.repeat(str(alarm["level"]), bool(phone.get("vibrate", True)))

        def clear(self, key):
            Bridge.clear(str(key))

    asyncio.run(om.serve(demo=bool(demo), port=int(port), host="127.0.0.1",
                         open_browser=False, phone_alarms_cls=AndroidAlarms))
