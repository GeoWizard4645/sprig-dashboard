# network_app.py  (button A)  --  "Ghost Sniffer"
# ---------------------------------------------------------------------------
# Surprise project: a Wi-Fi recon tool + Sprig system monitor.  J/L cycle
# between three views:
#
#   RF SCAN  : live histogram of nearby AP signal strength, a scrolling
#              "peak signal" waterfall, and open/hidden network counts.
#   CHANNELS : 2.4 GHz channel-congestion analyzer -- AP count per channel,
#              busiest channel, and a recommended clear channel.
#   SYSTEM   : Sprig telemetry -- RP2040 die temp, CPU clock, RAM & flash
#              usage bars, uptime, IP / MAC, connected-AP RSSI.
#
#   I/K scroll the AP list (RF view).
#
# Note: wlan.scan() blocks ~1-2s; it only runs for the RF / CHANNELS views.
# ---------------------------------------------------------------------------

import gfx_engine as g
from app_base import App
import config
import machine
import gc
import os
import time

try:
    import ubinascii as _binascii
except ImportError:
    import binascii as _binascii

_SEC = {0: "OPEN", 1: "WEP", 2: "WPA", 3: "WPA2", 4: "WPA/2", 5: "WPA3", 7: "WPA2-E"}
_TEMP_ADC = machine.ADC(4)
_VIEWS = ("scan", "channels", "sys")


def _frac(rssi):
    f = (rssi + 100) / 70.0
    if f < 0:
        return 0.0
    if f > 1:
        return 1.0
    return f


def _bar_color(frac):
    if frac > 0.66:
        return g.GREEN
    if frac > 0.33:
        return g.YELLOW
    return g.RED


def _read_temp_c():
    raw = _TEMP_ADC.read_u16()
    v = raw * 3.3 / 65535
    return 27 - (v - 0.706) / 0.001721


class NetworkApp(App):
    name = "GHOST SNIFFER"
    refresh_interval = config.NET_SCAN_S
    background_refresh = False     # wlan.scan() blocks; only run while on screen

    def __init__(self, gfx, wifi):
        super().__init__(gfx, wifi)
        self.vi = 0
        self.aps = []             # (ssid, channel, rssi, security)
        self.cursor = 0
        self.history = []
        self.boot = time.ticks_ms()

    def _view(self):
        return _VIEWS[self.vi]

    async def refresh(self):
        if self._view() == "sys":
            self.dirty = True     # telemetry only; skip the heavy scan
            return
        wlan = self.wifi.wlan
        try:
            wlan.active(True)
            nets = []
            for n in wlan.scan():
                ssid = n[0]
                try:
                    ssid = ssid.decode()
                except Exception:
                    ssid = str(ssid)
                nets.append((ssid or "<hidden>", n[2], n[3], n[4]))
            nets.sort(key=lambda x: x[2], reverse=True)
            self.aps = nets
            self.history.append(nets[0][2] if nets else -100)
            if len(self.history) > g.WIDTH:
                self.history = self.history[-g.WIDTH:]
            self.status = "%d APs" % len(nets)
            if self.cursor >= len(nets):
                self.cursor = max(0, len(nets) - 1)
        except Exception as e:
            self.status = "scan err: %s" % e
        self.dirty = True

    # -- navigation ---------------------------------------------------------
    def on_left(self):
        self.vi = (self.vi - 1) % len(_VIEWS)
        self.dirty = True

    def on_right(self):
        self.vi = (self.vi + 1) % len(_VIEWS)
        self.dirty = True

    def on_up(self):
        if self._view() == "scan" and self.aps:
            self.cursor = (self.cursor - 1) % len(self.aps)
        self.dirty = True

    def on_down(self):
        if self._view() == "scan" and self.aps:
            self.cursor = (self.cursor + 1) % len(self.aps)
        self.dirty = True

    # -- rendering ----------------------------------------------------------
    def render(self):
        v = self._view()
        if v == "sys":
            self._render_sys()
        elif v == "channels":
            self._render_channels()
        else:
            self._render_scan()

    def _hint(self, label):
        self.gfx.draw_text(label, g.WIDTH - 8 * len(label) - 2, g.CONTENT_Y, g.DIM)

    # --- RF scanner view ---
    def _render_scan(self):
        gfx = self.gfx
        n_open = sum(1 for a in self.aps if a[3] == 0)
        gfx.draw_text("RF SCAN  %s" % (self.status or "..."), 4, g.CONTENT_Y, g.ACCENT)
        self._hint("J/L>chan")
        if not self.aps:
            gfx.draw_text("scanning air...", 6, g.HEIGHT // 2, g.GREY)
            return
        self._histogram()
        ssid, ch, rssi, sec = self.aps[self.cursor]
        y = g.CONTENT_Y + 54
        gfx.draw_text(ssid[:20], 4, y, g.WHITE)
        gfx.draw_text("CH%-3d %4ddBm %s" % (ch, rssi, _SEC.get(sec, "?")),
                     4, y + 10, _bar_color(_frac(rssi)))
        oc = g.RED if n_open else g.GREEN
        gfx.draw_text("open:%d" % n_open, g.WIDTH - 8 * 7, y + 10, oc)
        self._waterfall()

    def _histogram(self):
        gfx = self.gfx
        top = g.CONTENT_Y + 12
        h = 38
        n = min(len(self.aps), 13)
        bw = g.WIDTH // 13
        for i in range(n):
            rssi = self.aps[i][2]
            f = _frac(rssi)
            x = i * bw + 1
            if i == self.cursor:
                gfx.draw_rect(x - 1, top - 1, bw, h + 2, g.WHITE)
            gfx.vbar(x, top, bw - 2, h, f, _bar_color(f))
        gfx.hline(0, top + h + 1, g.WIDTH, g.DIM)

    def _waterfall(self):
        gfx = self.gfx
        base = g.HEIGHT - 2
        top = g.HEIGHT - 20
        h = base - top
        gfx.draw_rect(0, top - 1, g.WIDTH, h + 2, g.PANEL, fill=True)
        gfx.draw_text("PEAK", 2, top - 9, g.DIM)
        data = self.history
        x0 = g.WIDTH - len(data)
        prev = None
        for i, rssi in enumerate(data):
            x = x0 + i
            yv = base - int(_frac(rssi) * h)
            if prev is not None:
                gfx.line(x - 1, prev, x, yv, g.ACCENT)
            gfx.pixel(x, yv, g.WHITE)
            prev = yv

    # --- channel congestion view ---
    def _render_channels(self):
        gfx = self.gfx
        gfx.draw_text("CHANNELS 2.4G", 4, g.CONTENT_Y, g.ACCENT)
        self._hint("J/L>sys")
        if not self.aps:
            gfx.draw_text("scanning air...", 6, g.HEIGHT // 2, g.GREY)
            return
        counts = [0] * 14
        for _ssid, ch, _rssi, _sec in self.aps:
            if 1 <= ch <= 13:
                counts[ch] += 1
        peak = max(counts) or 1
        top = g.CONTENT_Y + 14
        h = 70
        base = top + h
        cw = g.WIDTH // 13
        for ch in range(1, 14):
            c = counts[ch]
            x = (ch - 1) * cw + 1
            col = g.RED if c >= 3 else (g.YELLOW if c == 2 else (g.GREEN if c == 1 else g.DIM))
            bh = int(h * c / peak)
            gfx.draw_rect(x, base - bh, cw - 2, bh, col, fill=True)
            gfx.draw_text(str(ch), x, base + 2, g.GREY)
            if c:
                gfx.draw_text(str(c), x, base - bh - 9, g.WHITE)
        busiest = counts.index(peak) if peak else 0
        # recommend the emptiest of the non-overlapping channels 1/6/11
        clear = min((1, 6, 11), key=lambda c: counts[c])
        gfx.draw_text("busiest CH%d  use CH%d" % (busiest, clear),
                     4, g.HEIGHT - 10, g.ACCENT)

    # --- system telemetry view ---
    def _statbar(self, y, label, frac, value, col):
        gfx = self.gfx
        if frac < 0:
            frac = 0
        elif frac > 1:
            frac = 1
        bx, bw = 42, 66
        gfx.draw_text(label, 4, y, g.GREY)
        gfx.draw_rect(bx, y, bw, 7, g.PANEL, fill=True)
        gfx.draw_rect(bx, y, int(bw * frac), 7, col, fill=True)
        gfx.draw_text(value, bx + bw + 3, y, g.WHITE)

    def _render_sys(self):
        gfx = self.gfx
        gfx.draw_text("SPRIG SYSTEMS", 4, g.CONTENT_Y, g.ACCENT)
        self._hint("J/L>rf")
        y = g.CONTENT_Y + 12
        try:
            mhz = machine.freq() // 1000000
        except Exception:
            mhz = 0
        gfx.draw_text("RP2040  %d MHz" % mhz, 4, y, g.WHITE)
        y += 12
        try:
            t = _read_temp_c()
            tc = g.GREEN if t < 40 else (g.YELLOW if t < 50 else g.RED)
            self._statbar(y, "TEMP", t / 60.0, "%.1fC" % t, tc)
        except Exception:
            gfx.draw_text("TEMP  n/a", 4, y, g.DIM)
        y += 11
        try:
            alloc = gc.mem_alloc()
            total = alloc + gc.mem_free()
            self._statbar(y, "RAM", alloc / total, "%dk" % (alloc // 1024),
                         g.GREEN if alloc / total < 0.75 else g.RED)
        except Exception:
            pass
        y += 11
        try:
            st = os.statvfs("/")
            ftot = st[0] * st[2]
            fused = ftot - st[0] * st[3]
            self._statbar(y, "FLASH", fused / ftot, "%dk" % (fused // 1024), g.BLUE)
        except Exception:
            pass
        y += 12
        up = time.ticks_diff(time.ticks_ms(), self.boot) // 1000
        gfx.draw_text("UP   %d:%02d:%02d" % (up // 3600, (up % 3600) // 60, up % 60),
                     4, y, g.GREY)
        y += 10
        wlan = self.wifi.wlan
        try:
            ip = wlan.ifconfig()[0]
        except Exception:
            ip = "0.0.0.0"
        gfx.draw_text("IP   %s" % ip, 4, y, g.GREY)
        y += 10
        try:
            mac = _binascii.hexlify(wlan.config("mac"), ":").decode()
        except Exception:
            mac = "--"
        gfx.draw_text("MAC  %s" % mac, 4, y, g.GREY)
        y += 10
        try:
            rssi = wlan.status("rssi")
            ssid = wlan.config("essid")
            gfx.draw_text("NET  %s %ddBm" % (ssid[:9], rssi), 4, y,
                         _bar_color(_frac(rssi)))
        except Exception:
            gfx.draw_text("NET  offline", 4, y, g.RED)
