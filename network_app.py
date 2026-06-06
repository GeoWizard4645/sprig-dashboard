# network_app.py  (button A)  --  "Ghost Sniffer"
# ---------------------------------------------------------------------------
# Surprise project: a live WLAN signal visualizer AND a Sprig system monitor.
#
#   J / L  toggles between two views:
#     * RF SCAN : live histogram of nearby AP signal strength + a scrolling
#                 "peak signal" waterfall (from wlan.scan()).
#     * SYSTEM  : real-time Sprig telemetry -- RP2040 die temperature, CPU
#                 clock, RAM & flash usage bars, uptime, IP / MAC and the
#                 connected AP's RSSI.
#   I / K  scroll the AP list (RF view).
#
# Note: wlan.scan() blocks ~1-2s; it only runs while the RF view is active.
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

    def __init__(self, gfx, wifi):
        super().__init__(gfx, wifi)
        self.view = "scan"        # "scan" | "sys"
        self.aps = []
        self.cursor = 0
        self.history = []
        self.boot = time.ticks_ms()

    async def refresh(self):
        if self.view != "scan":
            self.dirty = True     # keep telemetry ticking; skip the heavy scan
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
        self.view = "sys" if self.view == "scan" else "scan"
        self.dirty = True

    def on_right(self):
        self.on_left()

    def on_up(self):
        if self.view == "scan" and self.aps:
            self.cursor = (self.cursor - 1) % len(self.aps)
        self.dirty = True

    def on_down(self):
        if self.view == "scan" and self.aps:
            self.cursor = (self.cursor + 1) % len(self.aps)
        self.dirty = True

    # -- rendering ----------------------------------------------------------
    def render(self):
        if self.view == "sys":
            self._render_sys()
        else:
            self._render_scan()

    # --- RF scanner view ---
    def _render_scan(self):
        gfx = self.gfx
        gfx.draw_text("RF SCAN  %s" % (self.status or "..."), 4, g.CONTENT_Y, g.ACCENT)
        gfx.draw_text("J/L:sys", g.WIDTH - 8 * 7 - 2, g.CONTENT_Y, g.DIM)
        if not self.aps:
            gfx.draw_text("scanning air...", 6, g.HEIGHT // 2, g.GREY)
            return
        self._histogram()
        self._detail()
        self._waterfall()

    def _histogram(self):
        gfx = self.gfx
        top = g.CONTENT_Y + 12
        h = 40
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

    def _detail(self):
        gfx = self.gfx
        ssid, ch, rssi, sec = self.aps[self.cursor]
        y = g.CONTENT_Y + 56
        gfx.draw_text(ssid[:18], 4, y, g.WHITE)
        gfx.draw_text("CH%-3d %4ddBm %s" % (ch, rssi, _SEC.get(sec, "?")),
                     4, y + 10, _bar_color(_frac(rssi)))

    def _waterfall(self):
        gfx = self.gfx
        base = g.HEIGHT - 2
        top = g.HEIGHT - 22
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
        gfx.draw_text("J/L:rf", g.WIDTH - 8 * 6 - 2, g.CONTENT_Y, g.DIM)
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
            free = gc.mem_free()
            total = alloc + free
            self._statbar(y, "RAM", alloc / total, "%dk" % (alloc // 1024),
                         g.GREEN if alloc / total < 0.75 else g.RED)
        except Exception:
            pass
        y += 11

        try:
            st = os.statvfs("/")
            fbs, fblk, ffree = st[0], st[2], st[3]
            ftot = fbs * fblk
            fused = ftot - fbs * ffree
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
