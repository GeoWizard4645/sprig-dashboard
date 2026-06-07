# app_base.py
# ---------------------------------------------------------------------------
# Common base class for every screen in the dashboard. main.py owns a registry
# of these and routes global navigation events to the active one.
#
# Navigation contract (events are dispatched by main.py):
#   single I -> on_up      single K -> on_down
#   single J -> on_left    single L -> on_right
#   double I / double L -> on_select
#   double J / double K -> on_back
# ---------------------------------------------------------------------------

import time
import gfx_engine as g


class App:
    name = "APP"
    refresh_interval = 0          # seconds between auto refreshes (0 = never)
    # When True the app is kept fresh in the background even when not on screen
    # (so data is ready the instant you switch to it). Live-only apps that block
    # (e.g. wlan.scan) set this False and refresh only while active.
    background_refresh = True
    # "light" apps are cheap to refresh; "heavy" apps stream large payloads and
    # are only warmed in the background while the active screen is light, so the
    # device isn't overworked when you're already on a data-heavy screen.
    bg_cost = "light"

    def __init__(self, gfx, wifi):
        self.gfx = gfx
        self.wifi = wifi
        self.dirty = True         # request a re-render
        self.status = ""          # transient status / error line
        self.last_refresh = 0     # ticks_ms of last successful refresh
        self.active = False       # True while this app is the one on screen
        # When True, main dispatches I/J/K/L as instant single presses with no
        # double-click detection (so rapid repeats never read as select/back).
        # Used by text-entry screens like the ticker search.
        self.raw_input = False

    # -- lifecycle ----------------------------------------------------------
    def on_enter(self):
        self.dirty = True

    def on_exit(self):
        pass

    # -- navigation (override what you need) --------------------------------
    def on_up(self): pass
    def on_down(self): pass
    def on_left(self): pass
    def on_right(self): pass
    def on_select(self): pass
    def on_back(self): pass

    # -- data ---------------------------------------------------------------
    async def refresh(self):
        """Override: pull fresh data. Called by main on refresh_interval."""
        pass

    def due(self):
        if self.last_refresh == 0:        # never fetched -> fetch now
            return True
        if self.refresh_interval == 0:
            return False
        return time.ticks_diff(time.ticks_ms(), self.last_refresh) >= self.refresh_interval * 1000

    def schedule_retry(self, secs):
        """Make due() fire again in ~secs (used to back off after an error)."""
        self.last_refresh = time.ticks_add(
            time.ticks_ms(), -((self.refresh_interval - secs) * 1000))

    # -- rendering ----------------------------------------------------------
    def render(self):
        """Override: draw into self.gfx (content area starts at g.CONTENT_Y)."""
        pass

    # -- helpers ------------------------------------------------------------
    def msg(self, text, color=g.GREY):
        """Centered single-line message in the content area."""
        self.gfx.draw_text(text, max(2, (g.WIDTH - 8 * len(text)) // 2),
                           g.HEIGHT // 2 - 4, color)
