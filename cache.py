# cache.py
# ---------------------------------------------------------------------------
# Tiny JSON persistence layer on the Pico's flash. Lets each app render
# last-known values instantly on boot (no blank screens, no waiting) and
# survive power cycles. Writes are atomic (temp + rename) and throttled to
# spare the flash from wear.
# ---------------------------------------------------------------------------

import json
import os
import time

_DIR = "/cache"
_MIN_SAVE_MS = 120000      # don't rewrite the same file more than every 2 min
_last = {}


def _ensure():
    try:
        os.mkdir(_DIR)
    except OSError:
        pass            # already exists (or read-only fs)


def load(name, default=None):
    try:
        with open("%s/%s.json" % (_DIR, name)) as f:
            return json.load(f)
    except Exception:
        return default


def save(name, obj, force=False):
    if not force:
        t = _last.get(name)
        if t is not None and time.ticks_diff(time.ticks_ms(), t) < _MIN_SAVE_MS:
            return False
    try:
        _ensure()
        tmp = "%s/%s.tmp" % (_DIR, name)
        with open(tmp, "w") as f:
            json.dump(obj, f)
        os.rename(tmp, "%s/%s.json" % (_DIR, name))
        _last[name] = time.ticks_ms()
        return True
    except Exception:
        return False
