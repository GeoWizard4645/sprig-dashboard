# sports_app.py  (button D)
# ---------------------------------------------------------------------------
# Score / standings aggregator over ESPN's public site API (no key).
#
# Two MODES (double-tap I/L to toggle):  LIVE  and  STANDINGS
# Tabs (J/L):  ALL · F1 · NFL · NBA · MLB
#   - ALL is the universal default in BOTH modes: every in-progress game across
#     all leagues, aggregated. (Kept exactly as before.)
#   - In LIVE mode a league tab shows that league's scoreboard.
#   - In STANDINGS mode a league tab shows the table — F1 shows DRIVERS and
#     CONSTRUCTORS as two scrollable sections; NFL/NBA/MLB show W-L tables.
#
# Memory: a Pico W can't hold every league's data at once, so only the data for
# the view you're looking at lives in RAM; everything else is parked on flash
# (`/cache/spscore_N`, `/cache/spstd_N`) and reloaded instantly when needed.
# All fetches are serialized (one TLS connection at a time) and retried.
# ---------------------------------------------------------------------------

import gfx_engine as g
import uasyncio as asyncio
import time
import gc
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
    """Generic ESPN standings extractor (F1 drivers+constructors, team W-L)."""
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
    bg_cost = "heavy"
    _ACTIVE_INTERVAL = 30        # refresh cadence while on screen (s)
    _BG_INTERVAL = 180           # relaxed cadence when off screen (s)
    _STD_TTL = 1800              # re-warm standings at most this often (s)

    def __init__(self, gfx, wifi):
        super().__init__(gfx, wifi)
        self.tiers = config.SPORTS_TIERS
        self.labels = ["ALL"] + [t[0] for t in self.tiers]
        self.tab = 0
        self.mode = "LIVE"           # "LIVE" | "STANDINGS"
        self.cursor = 0
        self.std_offset = 0
        self.loading = False
        # RAM holds only: the live aggregate (small) + the ONE viewed league's
        # scores and the ONE viewed league's standings. Rest lives on flash.
        self.live = cache.load("sports_live", []) or []
        self.score = []
        self.score_tier = -1
        self.std = None
        self.std_tier = -1
        self.std_time = {}

    def _tier(self):
        return self.tab - 1 if self.tab >= 1 else -1

    def _focus(self):
        t = self._tier()
        return self.tiers[t][2] if t >= 0 else None

    def _has_live(self, tier):
        lab = self.tiers[tier][0]
        return any(l == lab for l, _ in self.live)

    # -- fetching (all serialized by the HTTP layer's net lock) -------------
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

    async def _fetch_scores(self, tier):
        _label, path, _f = self.tiers[tier]
        url = "https://site.api.espn.com/apis/site/v2/sports/%s/scoreboard?limit=40" % path
        h = await self._stream(url, _Scoreboard)
        evs = h.events
        for ev in evs:
            ev["rows"].sort(key=lambda r: 0 if r[2] == "away" else 1)
            ev["rows"] = [(r[0], r[1]) for r in ev["rows"]]
        evs.sort(key=lambda e: _STATE_ORDER.get(e["state"], 3))
        return evs

    async def _load_live(self):
        if not await self.wifi.ensure():
            self.status = "no wifi"
            return
        live = []
        errs = 0
        for tier in range(len(self.tiers)):
            self.status = "scanning %s..." % self.tiers[tier][0]
            self.dirty = True
            try:
                evs = await self._fetch_scores(tier)
                cache.save("spscore_%d" % tier, evs)
                if tier == self.score_tier:
                    self.score = evs
                for ev in evs:
                    if ev["state"] == "in":
                        live.append((self.tiers[tier][0], ev))
                evs = None
            except Exception:
                errs += 1
            gc.collect()
        self.live = live
        cache.save("sports_live", live)
        self.status = "" if live else ("fetch failed" if errs == len(self.tiers) else "nothing live")

    async def _refresh_score(self, tier):
        if not await self.wifi.ensure():
            self.status = "no wifi"
            return
        evs = await self._fetch_scores(tier)
        cache.save("spscore_%d" % tier, evs)
        self.score = evs
        self.score_tier = tier
        self.status = ""

    async def _load_standings(self, tier, keep):
        if not await self.wifi.ensure():
            self.status = "no wifi"
            return
        _label, path, _f = self.tiers[tier]
        url = "https://site.api.espn.com/apis/v2/sports/%s/standings" % path
        h = await self._stream(url, _Standings)
        kind = "f1" if path.startswith("racing") else "team"
        obj = {"kind": kind, "groups": h.groups}
        self.std_time[tier] = time.ticks_ms()
        cache.save("spstd_%d" % tier, obj)
        if keep:
            self.std = obj
            self.std_tier = tier
        self.status = ""

    # -- view slots (instant load from flash) ------------------------------
    def _view_scores(self, tier):
        if self.score_tier != tier:
            self.score = cache.load("spscore_%d" % tier, []) or []
            self.score_tier = tier

    def _view_std(self, tier):
        if self.std_tier != tier:
            self.std = cache.load("spstd_%d" % tier, None)
            self.std_tier = tier

    def _prime(self):
        if self.tab == 0:
            return
        if self.mode == "LIVE":
            self._view_scores(self._tier())
        else:
            self._view_std(self._tier())

    # -- refresh (driven by main; serialized) ------------------------------
    async def refresh(self):
        self.loading = True
        try:
            if self.tab == 0:
                await self._load_live()
            elif self.mode == "LIVE":
                await self._refresh_score(self._tier())
            else:
                await self._load_standings(self._tier(), keep=True)
        except Exception as e:
            self.status = "err: %s" % str(e)[:16]
        self.loading = False
        self.dirty = True

    def due(self):
        if self.last_refresh == 0:
            return True
        interval = self._ACTIVE_INTERVAL if self.active else self._BG_INTERVAL
        return time.ticks_diff(time.ticks_ms(), self.last_refresh) >= interval * 1000

    async def prefetch_step(self):
        """Background: warm standings to FLASH (not RAM), one league per call."""
        if self.loading:
            return False
        now = time.ticks_ms()
        targets = [t for t in range(len(self.tiers)) if t not in self.std_time]
        if not targets:
            targets = [t for t in range(len(self.tiers))
                       if time.ticks_diff(now, self.std_time.get(t, 0)) > self._STD_TTL * 1000]
        if not targets:
            return False
        self.loading = True
        try:
            await self._load_standings(targets[0], keep=(targets[0] == self.std_tier))
        except Exception:
            pass
        finally:
            self.loading = False
        return True

    # -- navigation ---------------------------------------------------------
    def _scroll_len(self):
        if self.mode == "STANDINGS" and self.tab >= 1:
            return 0
        if self.tab == 0:
            return len(self.live)
        return len(self.score)

    def on_up(self):
        if self.mode == "STANDINGS" and self.tab >= 1:
            self.std_offset = max(0, self.std_offset - 1)
        else:
            n = self._scroll_len()
            if n:
                self.cursor = (self.cursor - 1) % n
        self.dirty = True

    def on_down(self):
        if self.mode == "STANDINGS" and self.tab >= 1:
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
        self._prime()
        self.last_refresh = 0       # force a fresh fetch on the next scheduler tick
        self.dirty = True

    def on_left(self):
        self._switch(-1)

    def on_right(self):
        self._switch(1)

    def on_select(self):
        self.mode = "STANDINGS" if self.mode == "LIVE" else "LIVE"
        self.cursor = 0
        self.std_offset = 0
        self._prime()
        self.last_refresh = 0
        self.dirty = True

    def on_enter(self):
        self.active = True
        self._prime()
        self.last_refresh = 0       # refresh promptly when opened
        self.dirty = True

    def on_exit(self):
        self.active = False

    # -- rendering ----------------------------------------------------------
    def render(self):
        self._tabs()
        if self.tab == 0:
            self._live_home()
            return
        if self.mode == "STANDINGS":
            self._view_std(self._tier())
            self._render_standings()
        else:
            self._view_scores(self._tier())
            if not self.score:
                self.gfx.draw_text(self.status or "loading...", 6, _SPLIT_Y - 20,
                                  g.YELLOW if self.status else g.GREY)
                return
            if self.cursor >= len(self.score):
                self.cursor = 0
            self._scoreboard(self.score[self.cursor])
            self._list(self.score)

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
        # mode indicator at far right
        pill = "LV" if self.mode == "LIVE" else "ST"
        gfx.draw_text(pill, g.WIDTH - 17, y + 2, g.RED if self.mode == "LIVE" else g.BLUE)

    def _live_home(self):
        gfx = self.gfx
        y = g.CONTENT_Y + 14
        if not self.live:
            if self.loading:
                gfx.draw_text(self.status or "scanning...", 6, y + 8, g.YELLOW)
            else:
                gfx.draw_text("No games live now", 6, y + 8, g.GREY)
                gfx.draw_text("J/L league  dblI/L table", 6, y + 22, g.DIM)
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
                gfx.draw_text(ev["name"], 40, y, g.WHITE)
            gfx.draw_text(ev["detail"], 4, y + 10, g.RED)
            y += row_h

    @staticmethod
    def _name(kind, r):
        if kind == "f1":
            return r.get("short") or r.get("abbr") or "?"
        return r.get("abbr") or r.get("short") or "?"

    def _std_lines(self, st):
        out = []
        kind = st["kind"]
        for grp in st["groups"]:
            lbl = grp["label"]
            if kind == "f1":
                hdr = "DRIVERS" if "Driver" in lbl else (
                    "CONSTRUCTORS" if "Constructor" in lbl else lbl.upper())
            else:
                hdr = lbl
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
        st = self.std
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
                col = g.YELLOW if (focus and txt.startswith(focus)) else g.WHITE
                gfx.draw_text(txt, 6, y, col)
            y += row_h
        # scroll arrows
        if self.std_offset > 0:
            gfx.draw_text("^", g.WIDTH - 9, g.CONTENT_Y + 25, g.ACCENT)
        if self.std_offset + rows_fit < len(lines):
            gfx.draw_text("v", g.WIDTH - 9, g.HEIGHT - 9, g.ACCENT)

    def _scoreboard(self, ev):
        gfx = self.gfx
        focus = self._focus()
        y = g.CONTENT_Y + 14
        state = ev["state"]
        state_col = g.RED if state == "in" else (g.GREEN if state == "post" else g.YELLOW)
        if state == "in":
            gfx.draw_text("LIVE", 6, g.CONTENT_Y + 1, g.RED)
        rows = ev["rows"]
        if len(rows) >= 2 and len(rows[0][0]) <= 5:
            for idx in range(2):
                nm, sc = rows[idx]
                ry = y + idx * 26
                col = g.YELLOW if (focus and nm == focus) else g.WHITE
                gfx.draw_text(nm, 6, ry, col, scale=2, max_w=80)
                sc = str(sc)
                gfx.draw_text(sc, g.WIDTH - 6 - 8 * 3 * len(sc), ry, col, scale=3)
            gfx.draw_text(ev["detail"], 6, y + 53, state_col)
        else:
            gfx.draw_text(ev["name"], 6, y, g.WHITE)
            gfx.draw_text(ev["detail"], 6, y + 12, state_col)
            ly = y + 26
            for i, (nm, sc) in enumerate(rows[:3]):
                gfx.draw_text("%d. %s" % (i + 1, nm), 8, ly, g.GREY)
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
            # leave room for the status tag on the right
            gfx.draw_text(line, 3, y, col, max_w=g.WIDTH - 56)
            st = ev["detail"]
            if st:
                short = st.replace("Final", "F").replace(" - ", " ")[:6]
                gfx.draw_text(short, g.WIDTH - 8 * len(short) - 2, y,
                             g.RED if ev["state"] == "in" else g.DIM)
            y += row_h
