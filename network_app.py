# network_app.py  (button A)  --  "Ghost Sniffer"
# ---------------------------------------------------------------------------
# Surprise project: a live WLAN signal-strength visualizer. Repeatedly runs
# wlan.scan() and renders a real-time histogram of nearby access points plus a
# scrolling waterfall of the strongest signal over time.
#
# Note: wlan.scan() is a blocking call (~1-2s). It runs on the refresh tick,
# so the UI pauses briefly during each sweep -- expected for an RF scanner.
# ---------------------------------------------------------------------------

import gfx_engine as g
from app_base import App
import config

_SEC = {0: "OPEN", 1: "WEP", 2: "WPA", 3: "WPA2", 4: "WPA/2", 5: "WPA3", 7: "WPA2-E"}


def _frac(rssi):
    f = (rssi + 100) / 70.0       # -100 dBm -> 0, -30 dBm -> 1
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


class NetworkApp(App):
    name = "GHOST SNIFFER"
    refresh_interval = config.NET_SCAN_S

    def __init__(self, gfx, wifi):
        super().__init__(gfx, wifi)
        self.aps = []              # list of (ssid, channel, rssi, security)
        self.cursor = 0
        self.history = []          # strongest RSSI per scan (waterfall)

    async def refresh(self):
        wlan = self.wifi.wlan
        try:
            wlan.active(True)
            raw = wlan.scan()      # blocking
            nets = []
            for n in raw:
                ssid = n[0]
                try:
                    ssid = ssid.decode()
                except Exception:
                    ssid = str(ssid)
                if not ssid:
                    ssid = "<hidden>"
                nets.append((ssid, n[2], n[3], n[4]))
            nets.sort(key=lambda x: x[2], reverse=True)
            self.aps = nets
            best = nets[0][2] if nets else -100
            self.history.append(best)
            if len(self.history) > g.WIDTH:
                self.history = self.history[-g.WIDTH:]
            self.status = "%d APs" % len(nets)
            if self.cursor >= len(nets):
                self.cursor = max(0, len(nets) - 1)
        except Exception as e:
            self.status = "scan err: %s" % e
        self.dirty = True

    # -- navigation ---------------------------------------------------------
    def on_up(self):
        if self.aps:
            self.cursor = (self.cursor - 1) % len(self.aps)
        self.dirty = True

    def on_down(self):
        if self.aps:
            self.cursor = (self.cursor + 1) % len(self.aps)
        self.dirty = True

    def on_left(self):
        self.on_up()

    def on_right(self):
        self.on_down()

    # -- rendering ----------------------------------------------------------
    def render(self):
        gfx = self.gfx
        gfx.draw_text("RF SCAN  %s" % (self.status or "..."), 4, g.CONTENT_Y, g.ACCENT)
        if not self.aps:
            gfx.draw_text("scanning air...", 6, g.HEIGHT // 2, g.GREY)
            return
        self._histogram()
        self._detail()
        self._waterfall()

    def _histogram(self):
        gfx = self.gfx
        top = g.CONTENT_Y + 12
        h = 44
        base = top + h
        n = min(len(self.aps), 13)
        bw = g.WIDTH // 13
        for i in range(n):
            _ssid, _ch, rssi, _sec = self.aps[i]
            f = _frac(rssi)
            x = i * bw + 1
            col = _bar_color(f)
            if i == self.cursor:
                gfx.draw_rect(x - 1, top - 1, bw, h + 2, g.WHITE)
            gfx.vbar(x, top, bw - 2, h, f, col)
        gfx.hline(0, base + 1, g.WIDTH, g.DIM)

    def _detail(self):
        gfx = self.gfx
        ssid, ch, rssi, sec = self.aps[self.cursor]
        y = g.CONTENT_Y + 60
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
                gfx.vline(x, min(prev, yv), abs(prev - yv) + 1, g.ACCENT)
            gfx.pixel(x, yv, g.WHITE)
            prev = yv
