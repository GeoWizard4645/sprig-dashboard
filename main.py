# main.py
# ---------------------------------------------------------------------------
# App-router + global input handler for the Sprig Dashboard.
#
# Hardware controls (Sprig, active-low buttons):
#   D-pad cluster selects the app:
#       W -> Weather      A -> Ghost Sniffer (network)
#       S -> Finance      D -> Sports
#   Action cluster is in-app navigation (dispatched to the active app):
#       single I -> up      single K -> down
#       single J -> left    single L -> right
#       double I / double L -> on_select()
#       double J / double K -> on_back()
#
# Three cooperative uasyncio tasks: input polling, render, and a per-app
# refresh scheduler. A fourth optional task syncs an NTP clock for the header.
# ---------------------------------------------------------------------------

import uasyncio as asyncio
import time
import gc
from machine import Pin, PWM

import gfx_engine as g
import config
from wifi_manager import WifiManager
from weather_app import WeatherApp
from finance_app import FinanceApp
from sports_app import SportsApp
from network_app import NetworkApp

DOUBLE_MS = 320       # max gap between presses to count as a double-click
DEBOUNCE_MS = 40
APP_PINS = {5: "W", 6: "A", 7: "S", 8: "D"}
NAV_PINS = {12: "I", 13: "J", 14: "K", 15: "L"}


class Dashboard:
    def __init__(self):
        self.gfx = g.GFX()
        self.wifi = WifiManager()
        self.apps = {
            "W": WeatherApp(self.gfx, self.wifi),
            "S": FinanceApp(self.gfx, self.wifi),
            "D": SportsApp(self.gfx, self.wifi),
            "A": NetworkApp(self.gfx, self.wifi),
        }
        self.current = "W"
        self.app = self.apps["W"]
        self.clock = ""

        # buzzer (optional, carrier-dependent)
        self.buzzer = None
        if config.AUDIO_PIN is not None:
            try:
                self.buzzer = PWM(Pin(config.AUDIO_PIN))
                self.buzzer.duty_u16(0)
            except Exception:
                self.buzzer = None

        # input pins
        self.pins = {}
        for gp in list(APP_PINS) + list(NAV_PINS):
            self.pins[gp] = Pin(gp, Pin.IN, Pin.PULL_UP)
        self._prev = {gp: 1 for gp in self.pins}
        self._last_press = {gp: 0 for gp in self.pins}
        self._pending = {}     # nav name -> ticks_ms of first press

    # -- audio --------------------------------------------------------------
    async def _beep(self, freq, ms):
        if not self.buzzer:
            return
        try:
            self.buzzer.freq(freq)
            self.buzzer.duty_u16(2500)
            await asyncio.sleep_ms(ms)
        finally:
            try:
                self.buzzer.duty_u16(0)
            except Exception:
                pass

    def beep(self, freq=2000, ms=12):
        asyncio.create_task(self._beep(freq, ms))

    # -- app routing --------------------------------------------------------
    def switch_app(self, key):
        if key == self.current:
            return
        self.app.active = False
        self.app.on_exit()
        self.current = key
        self.app = self.apps[key]
        self.app.active = True
        self.app.on_enter()
        self.app.dirty = True
        self.beep(1400, 22)
        gc.collect()

    # -- event dispatch -----------------------------------------------------
    def _emit_single(self, name):
        a = self.app
        if name == "I":
            a.on_up()
        elif name == "K":
            a.on_down()
        elif name == "J":
            a.on_left()
        elif name == "L":
            a.on_right()
        self.beep(2000, 8)

    def _emit_double(self, name):
        a = self.app
        if name in ("I", "L"):
            a.on_select()
        else:
            a.on_back()
        self.beep(2600, 16)

    def _on_press(self, gp, now):
        if gp in APP_PINS:
            self.switch_app(APP_PINS[gp])
            return
        name = NAV_PINS[gp]
        # raw-input screens (e.g. ticker search) want instant single presses
        # with no double-click detection, so repeats can't trigger select/back.
        if getattr(self.app, "raw_input", False):
            self._emit_single(name)
            return
        if name in self._pending and time.ticks_diff(now, self._pending[name]) <= DOUBLE_MS:
            del self._pending[name]
            self._emit_double(name)
        else:
            self._pending[name] = now

    # -- tasks --------------------------------------------------------------
    async def input_loop(self):
        while True:
            now = time.ticks_ms()
            for gp, pin in self.pins.items():
                v = pin.value()
                if v == 0 and self._prev[gp] == 1:
                    if time.ticks_diff(now, self._last_press[gp]) > DEBOUNCE_MS:
                        self._last_press[gp] = now
                        self._on_press(gp, now)
                self._prev[gp] = v
            # fire single events whose double-click window has elapsed
            for name in list(self._pending):
                if time.ticks_diff(now, self._pending[name]) > DOUBLE_MS:
                    del self._pending[name]
                    self._emit_single(name)
            await asyncio.sleep_ms(12)

    async def render_loop(self):
        gfx = self.gfx
        while True:
            if self.app.dirty:
                self.app.dirty = False
                gfx.fill(g.BG)
                try:
                    self.app.render()
                except Exception as e:
                    gfx.draw_text("render error", 4, g.CONTENT_Y + 20, g.RED)
                    gfx.draw_text(str(e)[:24], 4, g.CONTENT_Y + 32, g.RED)
                gfx.header(self.app.name, self.wifi.connected, self.clock)
                gfx.show()
            await asyncio.sleep_ms(33)

    def _busy(self, a):
        return getattr(a, "loading", False) or getattr(a, "loading_std", False)

    async def refresh_loop(self):
        # Keeps EVERY app's data warm (not just the visible one) so switching is
        # instant. Priority order: the ACTIVE app first; then background apps,
        # one at a time. A "heavy" background app (sports) is only warmed while
        # the active screen is "light" -- so we use the spare capacity of light
        # screens for heavy data, and don't pile work on data-heavy screens.
        while True:
            did = False
            active_light = getattr(self.app, "bg_cost", "light") == "light"
            cands = [self.app]
            for a in self.apps.values():
                if a is self.app or not getattr(a, "background_refresh", True):
                    continue
                if getattr(a, "bg_cost", "light") == "heavy" and not active_light:
                    continue           # don't add heavy work on a heavy screen
                cands.append(a)
            for a in cands:
                if a.due() and not self._busy(a):
                    a.last_refresh = time.ticks_ms()
                    ok = True
                    try:
                        await a.refresh()
                    except Exception as e:
                        a.status = "refresh err: %s" % e
                        a.dirty = True
                        ok = False
                    gc.collect()
                    st = a.status or ""
                    if (not ok) or ("err" in st) or ("wifi" in st) or ("failed" in st):
                        a.schedule_retry(15)
                    did = True
                    break       # one refresh per pass keeps the UI responsive
            await asyncio.sleep_ms(60 if did else 500)

    async def prefetch_loop(self):
        # On light screens with spare RAM/CPU, warm the heaviest data (sports
        # standings: F1 drivers + constructors, NFL/NBA/MLB tables) one league
        # at a time. Idles (no fetch) once everything is warm, so it doesn't
        # overwork the device or waste power.
        sports = self.apps.get("D")
        if sports is None or not hasattr(sports, "prefetch_step"):
            return
        while True:
            await asyncio.sleep_ms(2500)
            if getattr(self.app, "bg_cost", "light") != "light":
                continue           # active screen is heavy -> stay out of its way
            if not self.wifi.connected:
                continue
            try:
                if await sports.prefetch_step():
                    gc.collect()
            except Exception:
                pass

    async def clock_task(self):
        synced = False
        while True:
            if self.wifi.connected and not synced:
                try:
                    import ntptime
                    ntptime.settime()
                    synced = True
                except Exception:
                    pass
            if synced:
                t = time.localtime()
                self.clock = "%02d:%02d" % (t[3], t[4])   # UTC
                self.app.dirty = True
            await asyncio.sleep(30)

    def boot_screen(self):
        gfx = self.gfx
        gfx.fill(g.BG)
        gfx.text_center("SPRIG", 26, g.ACCENT, scale=3)
        gfx.text_center("DASHBOARD", 56, g.WHITE)
        gfx.draw_rect(20, 72, g.WIDTH - 40, 2, g.DIM, fill=True)
        gfx.text_center("W weather   S finance", 86, g.GREY)
        gfx.text_center("D sports    A sniffer", 98, g.GREY)
        gfx.text_center("connecting wifi...", 114, g.YELLOW)
        gfx.show()

    async def run(self):
        self.boot_screen()
        asyncio.create_task(self.wifi.connect())
        asyncio.create_task(self.clock_task())
        asyncio.create_task(self.input_loop())
        asyncio.create_task(self.refresh_loop())
        asyncio.create_task(self.prefetch_loop())
        self.app.active = True
        self.app.on_enter()
        self.app.dirty = True
        await self.render_loop()


def main():
    gc.collect()
    dash = Dashboard()
    try:
        asyncio.run(dash.run())
    except KeyboardInterrupt:
        pass
    finally:
        asyncio.new_event_loop()


if __name__ == "__main__":
    main()
