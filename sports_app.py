# sports_app.py  (button D)
# ---------------------------------------------------------------------------
# Score / standings aggregator over ESPN's public site API (no key).
# Priority tabs: F1 > NFL > NBA > MLB, with focus teams highlighted
# (Giants / Knicks / Yankees).
#
# Behaviour:
#   - LIVE games are prioritised above everything: events are sorted
#     in-progress -> scheduled -> final, and the cursor lands on a live game.
#   - On first open the app scans all leagues and defaults to the highest
#     priority tier that has a live game. If nothing is live it shows F1
#     driver standings.
#   - Layout: top ~60% live scoreboard, bottom ~40% league-wide scores.
#   - Only the active tab is streamed, to stay within the Pico's RAM budget;
#     huge (100-350 KB) ESPN payloads are parsed on the fly, never buffered.
# ---------------------------------------------------------------------------

import gfx_engine as g
import uasyncio as asyncio
from app_base import App
from wifi_manager import http_stream, JsonSax
import config

_SPLIT_Y = 82
_STATE_ORDER = {"in": 0, "pre": 1, "post": 2}


class _Scoreboard:
    """SAX handler: extract a compact event list from ESPN scoreboard JSON
    while it streams, so the full payload never lands in RAM."""
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
            if len(self.cur["rows"]) < 8:
                nm = self.comp["ab"] or self.comp["ath"] or "?"
                self.cur["rows"].append((nm, self.comp["sc"], self.comp["ha"]))
            self.comp = None

    def value(self, stack, key, val):
        if self.cur is None:
            return
        top = stack[-1] if stack else None
        par = stack[-2] if len(stack) >= 2 else None
        gp = stack[-3] if len(stack) >= 3 else None
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
            elif gp == ("a", "competitors"):
                if top == ("o", "team") and key == "abbreviation":
                    self.comp["ab"] = val
                elif top == ("o", "athlete") and key == "shortName":
                    self.comp["ath"] = val
        if key == "shortDetail":
            self.cur["detail"] = val
        elif key == "state" and val in ("pre", "in", "post"):
            self.cur["state"] = val


class _F1Standings:
    """SAX handler: pull driver name / rank / championship points from the
    235 KB ESPN F1 standings document (Driver Standings child only)."""
    def __init__(self):
        self.entries = []
        self.cur = None
        self.active = False
        self.stat_name = None
        self.stat_val = None

    def start(self, parent, kind, key):
        if kind != "o":
            return
        if parent == ("a", "entries") and self.active and len(self.entries) < 25:
            self.cur = {"name": "", "rank": "", "pts": ""}
        elif parent == ("a", "stats") and self.cur is not None:
            self.stat_name = None
            self.stat_val = None

    def end(self, parent, kind, key):
        if kind != "o":
            return
        if parent == ("a", "children"):
            self.active = False
        elif parent == ("a", "entries") and self.cur is not None:
            self.entries.append(self.cur)
            self.cur = None
        elif parent == ("a", "stats") and self.cur is not None:
            if self.stat_name == "rank":
                self.cur["rank"] = self.stat_val or ""
            elif self.stat_name == "championshipPts":
                self.cur["pts"] = self.stat_val or ""

    def value(self, stack, key, val):
        top = stack[-1] if stack else None
        par = stack[-2] if len(stack) >= 2 else None
        gp = stack[-3] if len(stack) >= 3 else None
        if par == ("a", "children") and key == "name":
            self.active = (val == "Driver Standings")
            return
        if not self.active:
            return
        if top == ("o", "athlete") and key == "shortName" and gp == ("a", "entries"):
            if self.cur is not None:
                self.cur["name"] = val
        if par == ("a", "stats"):
            if key == "name":
                self.stat_name = val
            elif key == "displayValue":
                self.stat_val = val


class SportsApp(App):
    name = "SPORTS"
    refresh_interval = config.SPORTS_REFRESH_S

    def __init__(self, gfx, wifi):
        super().__init__(gfx, wifi)
        self.tiers = config.SPORTS_TIERS
        self.tier = 0
        self.cursor = 0
        self.cache = {}            # tier_idx -> list[event] (live-first)
        self.standings = []        # F1 driver standings
        self.std_offset = 0
        self.loading = False
        self._scanned = False

    def _focus(self):
        return self.tiers[self.tier][2]

    def _has_live(self, tier):
        return any(e["state"] == "in" for e in self.cache.get(tier, []))

    def _f1_standings_mode(self):
        return self.tier == 0 and not self._has_live(0) and bool(self.standings)

    # -- data ---------------------------------------------------------------
    async def _load(self, tier=None):
        if tier is None:
            tier = self.tier
        label, path, _f = self.tiers[tier]
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
            await http_stream(url, JsonSax(sb).feed)
            evs = sb.events
            for ev in evs:
                ev["rows"].sort(key=lambda r: 0 if r[2] == "away" else 1)
                ev["rows"] = [(r[0], r[1]) for r in ev["rows"]]
            evs.sort(key=lambda e: _STATE_ORDER.get(e["state"], 3))   # live first
            self.cache[tier] = evs
            self.status = "" if evs else "no events"
        except Exception as e:
            self.status = "err: %s" % e
        self.loading = False
        # F1 with no live session -> make sure standings are available
        if tier == 0 and not self._has_live(0) and not self.standings:
            asyncio.create_task(self._load_standings())
        self.dirty = True

    async def _load_standings(self):
        url = "https://site.api.espn.com/apis/v2/sports/racing/f1/standings"
        try:
            h = _F1Standings()
            await http_stream(url, JsonSax(h).feed)
            if h.entries:
                self.standings = h.entries
        except Exception as e:
            self.status = "std err: %s" % e
        self.dirty = True

    async def _scan_all(self):
        """First-run: load every league, default to the top-priority tier
        with a live game (else F1 standings)."""
        best = None
        for i in range(len(self.tiers)):
            await self._load(i)
            if best is None and self._has_live(i):
                best = i
        self.tier = best if best is not None else 0
        self.cursor = 0
        self._scanned = True
        if self.tier == 0 and not self._has_live(0) and not self.standings:
            await self._load_standings()
        self.dirty = True

    async def refresh(self):
        if not self._scanned:
            await self._scan_all()
        else:
            await self._load(self.tier)

    def _ensure_loaded(self):
        if self.tier not in self.cache and not self.loading:
            asyncio.create_task(self._load(self.tier))

    # -- navigation ---------------------------------------------------------
    def _events(self):
        return self.cache.get(self.tier, [])

    def on_up(self):
        if self._f1_standings_mode():
            self.std_offset = max(0, self.std_offset - 1)
        else:
            ev = self._events()
            if ev:
                self.cursor = (self.cursor - 1) % len(ev)
        self.dirty = True

    def on_down(self):
        if self._f1_standings_mode():
            self.std_offset = min(max(0, len(self.standings) - 8), self.std_offset + 1)
        else:
            ev = self._events()
            if ev:
                self.cursor = (self.cursor + 1) % len(ev)
        self.dirty = True

    def on_left(self):
        self.tier = (self.tier - 1) % len(self.tiers)
        self.cursor = 0
        self.std_offset = 0
        self._ensure_loaded()
        self.dirty = True

    def on_right(self):
        self.tier = (self.tier + 1) % len(self.tiers)
        self.cursor = 0
        self.std_offset = 0
        self._ensure_loaded()
        self.dirty = True

    def on_select(self):
        asyncio.create_task(self._load(self.tier))
        self.dirty = True

    def on_enter(self):
        self.dirty = True

    # -- rendering ----------------------------------------------------------
    def render(self):
        self._tabs()
        if self._f1_standings_mode():
            self._standings()
            return
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
            live = self._has_live(i)
            w = 8 * len(label) + 6
            if active:
                gfx.draw_rect(x, y, w, 11, g.ACCENT, fill=True)
                gfx.draw_text(label, x + 3, y + 2, g.BLACK)
            else:
                gfx.draw_rect(x, y, w, 11, g.RED if live else g.DIM)
                gfx.draw_text(label, x + 3, y + 2, g.RED if live else g.GREY)
            if live:                       # live dot
                gfx.draw_rect(x + w - 3, y + 1, 2, 2, g.RED, fill=True)
            x += w + 3

    def _standings(self):
        gfx = self.gfx
        y = g.CONTENT_Y + 14
        gfx.draw_text("F1 DRIVER STANDINGS", 4, y, g.ACCENT)
        gfx.draw_text("I/K scroll", g.WIDTH - 8 * 10 - 2, y, g.DIM)
        y += 12
        rows = self.standings[self.std_offset:self.std_offset + 8]
        for e in rows:
            rk = e["rank"]
            col = g.YELLOW if rk == "1" else g.WHITE
            gfx.draw_text(rk.rjust(2), 4, y, col)
            gfx.draw_text(e["name"][:16], 26, y, col)
            pts = e["pts"]
            gfx.draw_text(pts, g.WIDTH - 8 * len(pts) - 4, y, g.GREEN)
            y += 12

    def _scoreboard(self, ev):
        gfx = self.gfx
        focus = self._focus()
        y = g.CONTENT_Y + 14
        state = ev["state"]
        state_col = g.RED if state == "in" else (g.GREEN if state == "post" else g.YELLOW)
        if state == "in":
            gfx.draw_text("LIVE", g.WIDTH - 8 * 4 - 3, g.CONTENT_Y + 1, g.RED)
        rows = ev["rows"]
        if len(rows) >= 2 and len(rows[0][0]) <= 5:
            for idx in range(2):
                nm, sc = rows[idx]
                ry = y + idx * 26
                col = g.YELLOW if (focus and nm == focus) else g.WHITE
                gfx.draw_text(nm, 6, ry, col, scale=2)
                gfx.draw_text(str(sc), g.WIDTH - 6 - 8 * 3 * len(str(sc)), ry, col, scale=3)
            gfx.draw_text(ev["detail"][:26], 6, y + 53, state_col)
        else:
            gfx.draw_text(ev["name"][:22], 6, y, g.WHITE)
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
            names = (rows[0][0] if rows else "", rows[1][0] if len(rows) > 1 else "")
            if focus and focus in names:
                col = g.YELLOW
            elif ev["state"] == "in":
                col = g.RED
            elif sel:
                col = g.WHITE
            gfx.draw_text(line[:18], 3, y, col)
            st = ev["detail"]
            if st:
                short = st.replace("Final", "F").replace(" - ", " ")[:6]
                gfx.draw_text(short, g.WIDTH - 8 * len(short) - 2, y,
                             g.RED if ev["state"] == "in" else g.DIM)
            y += row_h
