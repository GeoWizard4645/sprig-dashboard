# sports_app.py  (button D)
# ---------------------------------------------------------------------------
# Score / standings aggregator over ESPN's public site API (no key).
# Tabs: LIVE (all leagues' in-progress games) + F1 / NFL / NBA / MLB.
#
#   J/L      switch tab            I/K      scroll list
#   dbl I/L  toggle SCORES <-> STANDINGS
#   dbl J/K  (unused here)
#
# - LIVE games are prioritised; events sort in-progress -> scheduled -> final.
# - Per-league tabs lazy-load only when opened; standings load on first toggle.
# - All fetches retry with backoff (fixes F1 / ESPN intermittent failures).
# - Last-known scores + standings are cached to flash for instant boot.
# - Huge (100-350 KB) ESPN payloads are streamed/parsed, never fully buffered.
# ---------------------------------------------------------------------------

import gfx_engine as g
import uasyncio as asyncio
import time
from app_base import App
from wifi_manager import http_stream, JsonSax
import config
import cache

_SPLIT_Y = 82
_STATE_ORDER = {"in": 0, "pre": 1, "post": 2}


class _Scoreboard:
    """SAX handler: compact event list from ESPN scoreboard JSON, streamed."""
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


class _Standings:
    """SAX handler: generic ESPN standings extractor. Works for F1 driver
    standings (athlete + rank/championshipPts), F1 constructors (team +
    rank/points) and team leagues (team abbreviation + wins/losses/ties).
    Captures both an abbreviation and a short name per row so the renderer can
    pick the nicer one. Streams the 150-235 KB document."""
    _WANT = ("rank", "championshipPts", "points", "wins", "losses", "ties")

    def __init__(self):
        self.groups = []
        self.grp = None
        self.row = None
        self.sname = None
        self.sval = None

    def start(self, parent, kind, key):
        if kind != "o":
            return
        if parent == ("a", "children"):
            self.grp = {"label": "", "rows": []}
        elif parent == ("a", "entries") and self.grp is not None:
            self.row = {"abbr": "", "short": "", "stats": {}}
        elif parent == ("a", "stats") and self.row is not None:
            self.sname = None
            self.sval = None

    def end(self, parent, kind, key):
        if kind != "o":
            return
        if parent == ("a", "children") and self.grp is not None:
            if self.grp["rows"]:
                self.groups.append(self.grp)
            self.grp = None
        elif parent == ("a", "entries") and self.row is not None:
            self.grp["rows"].append(self.row)
            self.row = None
        elif parent == ("a", "stats") and self.row is not None:
            if self.sname in self._WANT:
                self.row["stats"][self.sname] = self.sval or ""

    def value(self, stack, key, val):
        top = stack[-1] if stack else None
        par = stack[-2] if len(stack) >= 2 else None
        gp = stack[-3] if len(stack) >= 3 else None
        if par == ("a", "children") and key == "name" and self.grp is not None \
                and not self.grp["label"]:
            self.grp["label"] = val
        if self.row is not None:
            if gp == ("a", "entries") and top in (("o", "team"), ("o", "athlete")):
                if key == "abbreviation" and not self.row["abbr"]:
                    self.row["abbr"] = val
                elif key in ("shortDisplayName", "shortName") and not self.row["short"]:
                    self.row["short"] = val
            elif par == ("a", "stats"):
                if key == "name":
                    self.sname = val
                elif key == "displayValue":
                    self.sval = val


class SportsApp(App):
    name = "SPORTS"
    refresh_interval = config.SPORTS_REFRESH_S
    bg_cost = "heavy"             # streams big payloads -> warm only when idle
    _BG_INTERVAL = 180           # relaxed refresh cadence when off screen (s)
    _STD_TTL = 1800              # re-warm standings at most this often (s)

    def __init__(self, gfx, wifi):
        super().__init__(gfx, wifi)
        self.tiers = config.SPORTS_TIERS
        self.labels = ["LIVE"] + [t[0] for t in self.tiers]
        self.tab = 0
        self.cursor = 0
        self.std_offset = 0
        self.mode = "scores"          # "scores" | "standings"
        self.loading = False
        self.loading_std = False
        self.active = False
        self.std_time = {}            # tier -> ticks_ms of last standings load
        # restore last-known data from flash for an instant first paint
        self.cache = {}
        for k, v in (cache.load("sports_scores", {}) or {}).items():
            try:
                self.cache[int(k)] = v
            except Exception:
                pass
        self.live = cache.load("sports_live", []) or []
        self.standings = {}
        for k, v in (cache.load("sports_standings", {}) or {}).items():
            try:
                self.standings[int(k)] = v
            except Exception:
                pass

    def _tier(self):
        return self.tab - 1 if self.tab >= 1 else -1

    def _focus(self):
        t = self._tier()
        return self.tiers[t][2] if t >= 0 else None

    def _has_live(self, tier):
        return any(e["state"] == "in" for e in self.cache.get(tier, []))

    # -- data ---------------------------------------------------------------
    async def _stream(self, url, factory, tries=3):
        last = None
        for attempt in range(tries):
            try:
                h = factory()
                await http_stream(url, JsonSax(h).feed)
                return h
            except Exception as e:
                last = e
                await asyncio.sleep_ms(400 * (attempt + 1))
        raise last

    async def _load(self, tier):
        label, path, _f = self.tiers[tier]
        if not await self.wifi.ensure():
            self.status = "no wifi"
            return
        self.status = "loading %s..." % label
        self.dirty = True
        url = "https://site.api.espn.com/apis/site/v2/sports/%s/scoreboard?limit=40" % path
        h = await self._stream(url, _Scoreboard)
        evs = h.events
        for ev in evs:
            ev["rows"].sort(key=lambda r: 0 if r[2] == "away" else 1)
            ev["rows"] = [(r[0], r[1]) for r in ev["rows"]]
        evs.sort(key=lambda e: _STATE_ORDER.get(e["state"], 3))
        self.cache[tier] = evs
        cache.save("sports_scores", {str(k): v for k, v in self.cache.items()})

    async def _load_live(self):
        live = []
        errs = 0
        for tier in range(len(self.tiers)):
            try:
                await self._load(tier)
            except Exception:
                errs += 1
            for ev in self.cache.get(tier, []):
                if ev["state"] == "in":
                    live.append((self.tiers[tier][0], ev))
        self.live = live
        cache.save("sports_live", live)
        self.status = "" if live else ("fetch failed" if errs == len(self.tiers) else "nothing live")

    async def _load_standings(self, tier):
        label, path, _f = self.tiers[tier]
        if not await self.wifi.ensure():
            self.status = "no wifi"
            return
        self.status = "loading %s table..." % label
        self.dirty = True
        url = "https://site.api.espn.com/apis/v2/sports/%s/standings" % path
        h = await self._stream(url, _Standings)
        kind = "f1" if path.startswith("racing") else "team"
        # keep ALL groups: F1 -> drivers + constructors; teams -> conferences
        self.standings[tier] = {"kind": kind, "groups": h.groups}
        self.std_time[tier] = time.ticks_ms()
        cache.save("sports_standings", {str(k): v for k, v in self.standings.items()})
        self.status = ""

    async def refresh(self):
        self.loading = True
        try:
            if self.tab == 0:
                # LIVE rescans all leagues only on screen or for the first boot
                if not (self.live and not self.active):
                    await self._load_live()
            else:
                await self._load(self._tier())
                self.status = ""
        except Exception as e:
            self.status = "err: %s" % str(e)[:18]
        self.loading = False
        self.dirty = True

    def _ensure(self):
        if self.loading:
            return
        if self.tab == 0:
            if not self.live:
                self.loading = True
                asyncio.create_task(self._refresh_task())
        elif self._tier() not in self.cache:
            self.loading = True
            asyncio.create_task(self._refresh_task())

    def _ensure_standings(self):
        t = self._tier()
        if t >= 0 and t not in self.standings and not self.loading_std:
            self.loading_std = True
            asyncio.create_task(self._std_task(t))

    async def _refresh_task(self):
        await self.refresh()
        self.last_refresh = time.ticks_ms()

    async def _std_task(self, tier):
        try:
            await self._load_standings(tier)
        except Exception as e:
            self.status = "table err"
        self.loading_std = False
        self.dirty = True

    def due(self):
        # off-screen: refresh on a relaxed cadence to avoid overworking the device
        if self.last_refresh == 0:
            return True
        interval = self.refresh_interval if self.active else self._BG_INTERVAL
        return time.ticks_diff(time.ticks_ms(), self.last_refresh) >= interval * 1000

    async def prefetch_step(self):
        """One unit of background warming (called by main only on light screens).
        Loads any missing standings, then re-warms a stale one. Returns True if
        it did work, False when everything is warm (so the caller idles and we
        don't burn power re-fetching)."""
        if self.loading or self.loading_std:
            return False
        now = time.ticks_ms()
        targets = [t for t in range(len(self.tiers)) if t not in self.standings]
        if not targets:
            targets = [t for t in range(len(self.tiers))
                       if self.std_time.get(t, 0)
                       and time.ticks_diff(now, self.std_time[t]) > self._STD_TTL * 1000]
        if not targets:
            return False
        self.loading_std = True
        try:
            await self._load_standings(targets[0])
        except Exception:
            pass
        finally:
            self.loading_std = False
        return True

    # -- navigation ---------------------------------------------------------
    def _events(self):
        t = self._tier()
        return self.cache.get(t, []) if t >= 0 else []

    def _scroll_len(self):
        if self.mode == "standings":
            return 0
        if self.tab == 0:
            return len(self.live)
        return len(self._events())

    def on_up(self):
        if self.mode == "standings":
            self.std_offset = max(0, self.std_offset - 1)
        else:
            n = self._scroll_len()
            if n:
                self.cursor = (self.cursor - 1) % n
        self.dirty = True

    def on_down(self):
        if self.mode == "standings":
            self.std_offset += 1
        else:
            n = self._scroll_len()
            if n:
                self.cursor = (self.cursor + 1) % n
        self.dirty = True

    def _switch(self, delta):
        self.tab = (self.tab + delta) % len(self.labels)
        self.cursor = 0
        self.std_offset = 0
        if self.mode == "standings":
            self._ensure_standings()
        else:
            self._ensure()
        self.dirty = True

    def on_left(self):
        self._switch(-1)

    def on_right(self):
        self._switch(1)

    def on_select(self):
        # toggle scores <-> standings
        self.mode = "standings" if self.mode == "scores" else "scores"
        self.cursor = 0
        self.std_offset = 0
        if self.mode == "standings":
            self._ensure_standings()
        else:
            self._ensure()
        self.dirty = True

    def on_enter(self):
        self.active = True
        self.dirty = True
        if self.mode == "standings":
            self._ensure_standings()
        else:
            self._ensure()

    def on_exit(self):
        self.active = False

    # -- rendering ----------------------------------------------------------
    def render(self):
        self._tabs()
        if self.mode == "standings":
            if self.tab == 0:
                self.gfx.draw_text("Pick a league (J/L)", 6, _SPLIT_Y - 20, g.GREY)
                self.gfx.draw_text("for standings", 6, _SPLIT_Y - 8, g.DIM)
            else:
                self._render_standings()
            return
        if self.tab == 0:
            self._live_home()
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
        x = 1
        y = g.CONTENT_Y
        for i, label in enumerate(self.labels):
            active = (i == self.tab)
            live = (i == 0 and bool(self.live)) or (i >= 1 and self._has_live(i - 1))
            w = 8 * len(label) + 4
            if active:
                gfx.draw_rect(x, y, w, 11, g.ACCENT, fill=True)
                gfx.draw_text(label, x + 2, y + 2, g.BLACK)
            else:
                gfx.draw_rect(x, y, w, 11, g.RED if live else g.DIM)
                gfx.draw_text(label, x + 2, y + 2, g.RED if live else g.GREY)
            if live and not active:
                gfx.draw_rect(x + w - 3, y + 1, 2, 2, g.RED, fill=True)
            x += w + 2
        # mode pill on the far right
        m = "STBL" if self.mode == "standings" else "SCOR"
        gfx.draw_text(m, g.WIDTH - 8 * 4 - 1, y + 2, g.ACCENT)

    def _live_home(self):
        gfx = self.gfx
        y = g.CONTENT_Y + 14
        if not self.live:
            if self.loading:
                gfx.draw_text("scanning leagues...", 6, y + 8, g.YELLOW)
            else:
                gfx.draw_text("No games live right now", 6, y + 8, g.GREY)
                gfx.draw_text("J/L browse  dblI/L table", 6, y + 22, g.DIM)
            return
        gfx.draw_text("LIVE NOW  (%d)" % len(self.live), 4, y, g.RED)
        y += 13
        row_h = 22
        rows_fit = (g.HEIGHT - y) // row_h
        if self.cursor >= len(self.live):
            self.cursor = 0
        start = max(0, self.cursor - rows_fit + 1) if self.cursor >= rows_fit else 0
        for i in range(start, min(len(self.live), start + rows_fit)):
            lbl, ev = self.live[i]
            sel = (i == self.cursor)
            if sel:
                gfx.draw_rect(0, y - 1, g.WIDTH, row_h, g.PANEL, fill=True)
                gfx.vline(0, y - 1, row_h, g.RED)
            gfx.draw_text(lbl, 4, y, g.ACCENT)
            rows = ev["rows"]
            if len(rows) >= 2 and len(rows[0][0]) <= 5:
                gfx.draw_text("%s %s-%s %s" % (rows[0][0], rows[0][1], rows[1][1], rows[1][0]),
                             40, y, g.WHITE)
            else:
                gfx.draw_text(ev["name"][:15], 40, y, g.WHITE)
            gfx.draw_text(ev["detail"][:30], 4, y + 10, g.RED)
            y += row_h

    @staticmethod
    def _name(kind, r):
        # F1 prefers the readable short name (driver "K. Antonelli", constructor
        # "Mercedes"); team leagues prefer the compact abbreviation ("NE").
        if kind == "f1":
            return r.get("short") or r.get("abbr") or "?"
        return r.get("abbr") or r.get("short") or "?"

    def _std_lines(self, st):
        """Flatten groups into scrollable (text, is_header) lines. For F1 this
        yields a DRIVERS section then a CONSTRUCTORS section -- two scrollable
        lists in one column."""
        out = []
        kind = st["kind"]
        for grp in st["groups"]:
            lbl = grp["label"]
            if kind == "f1":
                hdr = "DRIVERS" if "Driver" in lbl else (
                    "CONSTRUCTORS" if "Constructor" in lbl else lbl.upper()[:18])
            else:
                hdr = lbl[:21]
            out.append((hdr, True))
            for r in grp["rows"]:
                s = r["stats"]
                name = self._name(kind, r)
                if kind == "f1":
                    pts = s.get("championshipPts") or s.get("points") or ""
                    out.append(("%2s %-12s %4s" % (s.get("rank", ""), name[:12], pts), False))
                else:
                    rec = "%s-%s" % (s.get("wins", "?"), s.get("losses", "?"))
                    ti = s.get("ties", "")
                    if ti and ti != "0":
                        rec += "-" + ti
                    out.append(("%-4s %s" % (name[:4], rec), False))
        return out

    def _render_standings(self):
        gfx = self.gfx
        t = self._tier()
        st = self.standings.get(t)
        y = g.CONTENT_Y + 13
        if st is None:
            gfx.draw_text(self.status or "loading table...", 6, y + 6, g.YELLOW)
            return
        lines = self._std_lines(st)
        if not lines:
            gfx.draw_text("no standings", 6, y + 6, g.GREY)
            return
        gfx.draw_text("%s TABLE" % self.labels[self.tab], 4, y, g.ACCENT)
        gfx.draw_text("I/K", g.WIDTH - 8 * 3 - 2, y, g.DIM)
        y += 12
        row_h = 10
        rows_fit = (g.HEIGHT - y) // row_h
        if self.std_offset > max(0, len(lines) - rows_fit):
            self.std_offset = max(0, len(lines) - rows_fit)
        focus = self._focus()
        for i in range(self.std_offset, min(len(lines), self.std_offset + rows_fit)):
            txt, hdr = lines[i]
            if hdr:
                gfx.draw_text(txt, 2, y, g.ACCENT)
            else:
                col = g.WHITE
                if focus and txt.startswith(focus):
                    col = g.YELLOW
                gfx.draw_text(txt, 6, y, col)
            y += row_h

    def _scoreboard(self, ev):
        gfx = self.gfx
        focus = self._focus()
        y = g.CONTENT_Y + 14
        state = ev["state"]
        state_col = g.RED if state == "in" else (g.GREEN if state == "post" else g.YELLOW)
        if state == "in":
            gfx.draw_text("LIVE", g.WIDTH - 8 * 4 - 30, g.CONTENT_Y + 1, g.RED)
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
        start = self.cursor - rows_fit + 1 if self.cursor >= rows_fit else 0
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
