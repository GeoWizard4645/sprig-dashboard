# sports_app.py  (button D)
# ---------------------------------------------------------------------------
# Score / standings aggregator over ESPN's public site API (no key).
# Priority tabs: F1 > NFL > NBA > MLB, with focus teams highlighted
# (Giants / Knicks / Yankees).
#
# Layout: top ~60% = live scoreboard for the selected event,
#         bottom ~40% = league-wide scores list.
# Only the active tab is fetched, to stay within the Pico's RAM budget.
# ---------------------------------------------------------------------------

import gfx_engine as g
import uasyncio as asyncio
from app_base import App
from wifi_manager import http_stream, JsonSax
import config

_SPLIT_Y = 82          # boundary between scoreboard (top) and list (bottom)


class _Scoreboard:
    """SAX handler that extracts a compact event list from ESPN scoreboard
    JSON while it streams, so the full payload never lands in RAM."""
    def __init__(self):
        self.events = []
        self.cur = None
        self.comp = None

    def start(self, parent, kind, key):
        if kind != "o":
            return
        if parent == ("a", "events") and len(self.events) < 40:
            self.cur = {"name": "", "detail": "", "state": "pre", "rows": []}
        elif parent == ("a", "competitors") and self.cur is not None:
            self.comp = {"ab": "", "ath": "", "sc": "", "ha": ""}

    def end(self, parent, kind, key):
        if kind != "o":
            return
        if parent == ("a", "events") and self.cur is not None:
            self.events.append(self.cur)
            self.cur = None
        elif parent == ("a", "competitors") and self.comp is not None:
            if len(self.cur["rows"]) < 8:    # cap (F1 repeats drivers per session)
                nm = self.comp["ab"] or self.comp["ath"] or "?"
                self.cur["rows"].append((nm, self.comp["sc"], self.comp["ha"]))
            self.comp = None

    def value(self, stack, key, val):
        if self.cur is None:
            return
        top = stack[-1] if stack else None
        par = stack[-2] if len(stack) >= 2 else None
        gp = stack[-3] if len(stack) >= 3 else None    # grandparent
        if par == ("a", "events"):
            if key == "shortName":
                self.cur["name"] = val
            elif key == "name" and not self.cur["name"]:
                self.cur["name"] = val
        if self.comp is not None:
            if par == ("a", "competitors"):
                if key == "score":
                    self.comp["sc"] = val
                elif key == "homeAway":
                    self.comp["ha"] = val
            # only a competitor's *direct* team/athlete (gp == competitors),
            # so nested probable-pitcher / leader athletes don't clobber it
            elif gp == ("a", "competitors"):
                if top == ("o", "team") and key == "abbreviation":
                    self.comp["ab"] = val
                elif top == ("o", "athlete") and key == "shortName":
                    self.comp["ath"] = val
        if key == "shortDetail":
            self.cur["detail"] = val
        elif key == "state" and val in ("pre", "in", "post"):
            self.cur["state"] = val


class SportsApp(App):
    name = "SPORTS"
    refresh_interval = config.SPORTS_REFRESH_S

    def __init__(self, gfx, wifi):
        super().__init__(gfx, wifi)
        self.tiers = config.SPORTS_TIERS
        self.tier = 0
        self.cursor = 0
        self.cache = {}            # tier_idx -> list[event]
        self.loading = False

    def _focus(self):
        return self.tiers[self.tier][2]

    # -- data ---------------------------------------------------------------
    async def _load(self, tier=None):
        if tier is None:
            tier = self.tier
        label, path, _focus = self.tiers[tier]
        if not await self.wifi.ensure():
            self.status = "no wifi"
            self.dirty = True
            return
        self.loading = True
        self.status = "loading %s..." % label
        self.dirty = True
        url = "https://site.api.espn.com/apis/site/v2/sports/%s/scoreboard?limit=40" % path
        try:
            sb = _Scoreboard()
            sax = JsonSax(sb)
            await http_stream(url, sax.feed)
            for ev in sb.events:
                ev["rows"].sort(key=lambda r: 0 if r[2] == "away" else 1)
                ev["rows"] = [(r[0], r[1]) for r in ev["rows"]]
            self.cache[tier] = sb.events
            self.status = "" if sb.events else "no events"
        except Exception as e:
            self.status = "err: %s" % e
        self.loading = False
        self.dirty = True

    async def refresh(self):
        await self._load(self.tier)

    def _ensure_loaded(self):
        if self.tier not in self.cache and not self.loading:
            asyncio.create_task(self._load(self.tier))

    # -- navigation ---------------------------------------------------------
    def _events(self):
        return self.cache.get(self.tier, [])

    def on_up(self):
        ev = self._events()
        if ev:
            self.cursor = (self.cursor - 1) % len(ev)
        self.dirty = True

    def on_down(self):
        ev = self._events()
        if ev:
            self.cursor = (self.cursor + 1) % len(ev)
        self.dirty = True

    def on_left(self):
        self.tier = (self.tier - 1) % len(self.tiers)
        self.cursor = 0
        self._ensure_loaded()
        self.dirty = True

    def on_right(self):
        self.tier = (self.tier + 1) % len(self.tiers)
        self.cursor = 0
        self._ensure_loaded()
        self.dirty = True

    def on_select(self):
        asyncio.create_task(self._load(self.tier))
        self.dirty = True

    def on_enter(self):
        # First load is driven by main's refresh scheduler (due() == True on
        # first view); tab switches use _ensure_loaded(). Avoids a double fetch
        # of the large scoreboard JSON on entry.
        self.dirty = True

    # -- rendering ----------------------------------------------------------
    def render(self):
        self._tabs()
        events = self._events()
        if not events:
            self.gfx.draw_text(self.status or "loading...", 6, _SPLIT_Y - 20,
                              g.YELLOW if self.status else g.GREY)
            return
        if self.cursor >= len(events):
            self.cursor = 0
        self._scoreboard(events[self.cursor])
        self._list(events)

    def _tabs(self):
        gfx = self.gfx
        x = 2
        y = g.CONTENT_Y
        for i, (label, _p, _f) in enumerate(self.tiers):
            active = (i == self.tier)
            w = 8 * len(label) + 6
            if active:
                gfx.draw_rect(x, y, w, 11, g.ACCENT, fill=True)
                gfx.draw_text(label, x + 3, y + 2, g.BLACK)
            else:
                gfx.draw_rect(x, y, w, 11, g.DIM)
                gfx.draw_text(label, x + 3, y + 2, g.GREY)
            x += w + 3

    def _scoreboard(self, ev):
        gfx = self.gfx
        focus = self._focus()
        y = g.CONTENT_Y + 14
        state = ev["state"]
        state_col = g.RED if state == "in" else (g.GREEN if state == "post" else g.YELLOW)
        rows = ev["rows"]
        if len(rows) >= 2 and len(rows[0][0]) <= 5:
            # team-vs-team scoreboard
            for idx in range(2):
                nm, sc = rows[idx]
                ry = y + idx * 26
                col = g.YELLOW if (focus and nm == focus) else g.WHITE
                gfx.draw_text(nm, 6, ry, col, scale=2)
                gfx.draw_text(str(sc), g.WIDTH - 6 - 8 * 3 * len(str(sc)), ry, col, scale=3)
            gfx.draw_text(ev["detail"][:26], 6, y + 53, state_col)
        else:
            # racing / individual event
            gfx.draw_text(ev["name"][:22], 6, y, g.WHITE, scale=1)
            gfx.draw_text(ev["detail"][:26], 6, y + 12, state_col)
            ly = y + 26
            for i, (nm, sc) in enumerate(rows[:3]):
                gfx.draw_text("%d. %s" % (i + 1, nm[:16]), 8, ly, g.GREY)
                ly += 10
        gfx.hline(0, _SPLIT_Y - 2, g.WIDTH, g.DIM)

    def _list(self, events):
        gfx = self.gfx
        focus = self._focus()
        y = _SPLIT_Y + 2
        row_h = 11
        rows_fit = (g.HEIGHT - y) // row_h
        # keep cursor visible
        start = 0
        if self.cursor >= rows_fit:
            start = self.cursor - rows_fit + 1
        for i in range(start, min(len(events), start + rows_fit)):
            ev = events[i]
            sel = (i == self.cursor)
            if sel:
                gfx.draw_rect(0, y - 1, g.WIDTH, row_h, g.PANEL, fill=True)
            line = ev["name"]
            rows = ev["rows"]
            if len(rows) >= 2 and len(rows[0][0]) <= 5:
                line = "%s %s-%s %s" % (rows[0][0], rows[0][1], rows[1][1], rows[1][0])
            col = g.GREY
            if focus and focus in (rows[0][0] if rows else "", rows[1][0] if len(rows) > 1 else ""):
                col = g.YELLOW
            elif sel:
                col = g.WHITE
            st = ev["detail"]
            gfx.draw_text(line[:18], 3, y, col)
            if st:
                short = st.replace("Final", "F").replace(" - ", " ")[:6]
                gfx.draw_text(short, g.WIDTH - 8 * len(short) - 2, y,
                             g.RED if ev["state"] == "in" else g.DIM)
            y += row_h
