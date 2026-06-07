# finance_app.py  (button S)
# ---------------------------------------------------------------------------
# Ticker dashboard backed by Yahoo Finance's public v8 chart endpoint
# (no key, no crumb required).
#   - ONE scrolling list of all tickers (I/K scroll, viewport auto-follows)
#   - Drill-down detail with a price CHART + selectable timeframe
#     (1D/5D/1M/6M/1Y); J/L change timeframe, I/K browse tickers
#   - Single-press ribbon search for any custom ticker (no double-click traps)
#
# Yahoo occasionally rate-limits a symbol (AAPL flaking in/out): _fetch retries
# with backoff and alternates the query1/query2 hosts.
# ---------------------------------------------------------------------------

import gfx_engine as g
import uasyncio as asyncio
from app_base import App
from wifi_manager import http_json
import config
import store

_SEARCH = "__SEARCH__"
# ribbon cells for the search keyboard
_RIBBON = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-=^.&") + ["DEL", "OK", "CANCEL"]

# (label, range, interval)
_TFS = [("1D", "1d", "15m"), ("5D", "5d", "60m"), ("1M", "1mo", "1d"),
        ("6M", "6mo", "1d"), ("1Y", "1y", "1wk")]


def _enc(sym):
    return sym.replace("^", "%5E").replace("=", "%3D")


def _label(sym):
    s = sym.replace("^", "")
    for suf in ("-USD", "=F", "=X"):
        if s.endswith(suf):
            s = s[:-len(suf)]
            break
    return s


def _fmt(p):
    if p is None:
        return "--"
    ap = abs(p)
    if ap >= 1000:
        return "%.0f" % p
    if ap >= 1:
        return "%.2f" % p
    return "%.4f" % p


async def _fetch(sym, rng="1d", itv="15m", want_closes=False, tries=3):
    last = None
    for attempt in range(tries):
        host = "query1" if attempt % 2 == 0 else "query2"
        url = ("https://%s.finance.yahoo.com/v8/finance/chart/%s"
               "?range=%s&interval=%s") % (host, _enc(sym), rng, itv)
        try:
            status, data = await http_json(url, max_bytes=45000)
            ch = data.get("chart", {})
            if ch.get("error"):
                raise ValueError(ch["error"].get("description", "error"))
            res = ch["result"][0]
            meta = res["meta"]
            price = meta.get("regularMarketPrice")
            prev = meta.get("chartPreviousClose") or meta.get("previousClose")
            high = meta.get("regularMarketDayHigh")
            low = meta.get("regularMarketDayLow")
            closes = []
            if want_closes or high is None or low is None:
                try:
                    q = res["indicators"]["quote"][0]
                    closes = [x for x in q.get("close", []) if x is not None]
                    if closes:
                        if high is None:
                            high = max(closes)
                        if low is None:
                            low = min(closes)
                except Exception:
                    pass
            if price is None:
                raise ValueError("no price")
            pct = ((price - prev) / prev * 100) if (price and prev) else 0.0
            return {"price": price, "prev": prev, "high": high, "low": low,
                    "pct": pct, "closes": closes}
        except Exception as e:
            last = e
            await asyncio.sleep_ms(300 * (attempt + 1))
    raise last


class FinanceApp(App):
    name = "FINANCE"
    refresh_interval = config.FINANCE_REFRESH_S

    def __init__(self, gfx, wifi):
        super().__init__(gfx, wifi)
        # one combined, scrolling list
        self.symbols = list(config.FINANCE_PAGE_1) + list(config.FINANCE_PAGE_2)
        self.cursor = 0
        self.top = 0               # viewport scroll offset
        self.quotes = store.load("finance", {})   # last-known prices, instant on boot
        self.state = "list"
        self.detail_sym = None
        self.detail_idx = -1
        self.tf = 0
        self.chart = None
        self._tok = 0
        # search ribbon
        self.query = ""
        self.hpos = 0

    # -- data ---------------------------------------------------------------
    async def _spark_all(self):
        """Fetch every list symbol in ONE request via Yahoo's spark endpoint."""
        syms = ",".join(_enc(s) for s in self.symbols)
        last = None
        for attempt in range(3):
            host = "query1" if attempt % 2 == 0 else "query2"
            url = ("https://%s.finance.yahoo.com/v8/finance/spark"
                   "?symbols=%s&range=1d&interval=15m") % (host, syms)
            try:
                status, data = await http_json(url, max_bytes=50000)
                hit = 0
                for sym in self.symbols:
                    d = data.get(sym)
                    if not d:
                        continue
                    closes = [c for c in d.get("close", []) if c is not None]
                    price = closes[-1] if closes else None
                    prev = d.get("previousClose") or d.get("chartPreviousClose")
                    if price is None:
                        continue
                    pct = ((price - prev) / prev * 100) if (price and prev) else 0.0
                    self.quotes[sym] = {"price": price, "prev": prev, "pct": pct}
                    hit += 1
                if hit:
                    return hit
            except Exception as e:
                last = e
            await asyncio.sleep_ms(300 * (attempt + 1))
        if last:
            raise last
        return 0

    async def refresh(self):
        if not await self.wifi.ensure():
            self.status = "no wifi"
            self.dirty = True
            return
        try:
            ok = await self._spark_all()
            self.status = "" if ok else "fetch failed"
        except Exception:
            # spark down -> fall back to per-symbol chart fetches
            ok = 0
            for sym in self.symbols:
                try:
                    self.quotes[sym] = await _fetch(sym)
                    ok += 1
                except Exception as e:
                    self.quotes.setdefault(sym, {"price": None, "pct": 0, "err": str(e)})
                self.dirty = True
                await asyncio.sleep_ms(40)
            self.status = "" if ok else "fetch failed"
        if self.quotes:
            store.save("finance", self.quotes)
        self.dirty = True

    async def _load_chart(self):
        self._tok += 1
        tok = self._tok
        sym = self.detail_sym
        _lbl, rng, itv = _TFS[self.tf]
        self.chart = None
        self.status = ""
        self.dirty = True
        try:
            data = await _fetch(sym, rng, itv, want_closes=True)
            if tok == self._tok:
                self.chart = data
        except Exception as e:
            if tok == self._tok:
                self.status = "load err: %s" % e
        self.dirty = True

    def _open_detail(self, sym, idx):
        self.detail_sym = sym
        self.detail_idx = idx
        self.tf = 0
        self.state = "detail"
        self.raw_input = False
        asyncio.create_task(self._load_chart())

    async def _search_now(self, sym):
        self.status = "searching %s..." % sym
        self.dirty = True
        try:
            await _fetch(sym)
            self._open_detail(sym, -1)
            self.status = ""
        except Exception:
            self.status = "not found: %s" % sym
        self.dirty = True

    # -- navigation ---------------------------------------------------------
    def _items(self):
        return self.symbols + [_SEARCH]

    def _scroll_into_view(self, rows_fit):
        if self.cursor < self.top:
            self.top = self.cursor
        elif self.cursor >= self.top + rows_fit:
            self.top = self.cursor - rows_fit + 1

    def on_up(self):
        if self.state == "list":
            self.cursor = (self.cursor - 1) % len(self._items())
        elif self.state == "detail":
            if self.detail_idx >= 0 and self.symbols:
                self.detail_idx = (self.detail_idx - 1) % len(self.symbols)
                self._open_detail(self.symbols[self.detail_idx], self.detail_idx)
        elif self.state == "search":
            self._activate()
        self.dirty = True

    def on_down(self):
        if self.state == "list":
            self.cursor = (self.cursor + 1) % len(self._items())
        elif self.state == "detail":
            if self.detail_idx >= 0 and self.symbols:
                self.detail_idx = (self.detail_idx + 1) % len(self.symbols)
                self._open_detail(self.symbols[self.detail_idx], self.detail_idx)
        elif self.state == "search":
            self.query = self.query[:-1]      # backspace
        self.dirty = True

    def on_left(self):
        if self.state == "detail":
            self.tf = (self.tf - 1) % len(_TFS)
            asyncio.create_task(self._load_chart())
        elif self.state == "search":
            self.hpos = (self.hpos - 1) % len(_RIBBON)
        self.dirty = True

    def on_right(self):
        if self.state == "detail":
            self.tf = (self.tf + 1) % len(_TFS)
            asyncio.create_task(self._load_chart())
        elif self.state == "search":
            self.hpos = (self.hpos + 1) % len(_RIBBON)
        self.dirty = True

    def _activate(self):
        cell = _RIBBON[self.hpos]
        if cell == "DEL":
            self.query = self.query[:-1]
        elif cell == "CANCEL":
            self.state = "list"
            self.raw_input = False
        elif cell == "OK":
            sym = self.query.strip()
            if sym:
                asyncio.create_task(self._search_now(sym))
        elif len(self.query) < 10:
            self.query += cell

    def on_select(self):
        if self.state == "list":
            item = self._items()[self.cursor]
            if item == _SEARCH:
                self.state = "search"
                self.query = ""
                self.hpos = 0
                self.raw_input = True       # switch to instant single-press mode
            else:
                self._open_detail(item, self.cursor)
        elif self.state == "detail":
            asyncio.create_task(self._load_chart())
        self.dirty = True

    def on_back(self):
        if self.state in ("detail", "search"):
            self.state = "list"
            self.raw_input = False
        self.dirty = True

    # -- rendering ----------------------------------------------------------
    def render(self):
        if self.state == "detail":
            self._render_detail()
        elif self.state == "search":
            self._render_search()
        else:
            self._render_list()

    def _render_list(self):
        gfx = self.gfx
        items = self._items()
        gfx.draw_text("MARKETS  %d" % len(self.symbols), 4, g.CONTENT_Y, g.ACCENT)
        gfx.draw_text("I/K", g.WIDTH - 8 * 3 - 2, g.CONTENT_Y, g.DIM)
        y0 = g.CONTENT_Y + 12
        row_h = 13
        rows_fit = (g.HEIGHT - y0) // row_h
        self._scroll_into_view(rows_fit)
        y = y0
        for i in range(self.top, min(len(items), self.top + rows_fit)):
            item = items[i]
            sel = (i == self.cursor)
            if sel:
                gfx.draw_rect(0, y - 2, g.WIDTH, row_h, g.PANEL, fill=True)
                gfx.vline(0, y - 2, row_h, g.ACCENT)
            if item == _SEARCH:
                gfx.draw_text("[ + SEARCH TICKER ]", 6, y, g.ACCENT if sel else g.GREY)
            else:
                q = self.quotes.get(item)
                gfx.draw_text(_label(item), 6, y, g.WHITE if sel else g.GREY)
                if q and q.get("price") is not None:
                    gfx.draw_text(_fmt(q["price"]), 60, y, g.WHITE)
                    pct = q.get("pct", 0)
                    col = g.GREEN if pct >= 0 else g.RED
                    gfx.draw_text("%s%.2f%%" % ("+" if pct >= 0 else "", pct), 112, y, col)
                else:
                    gfx.draw_text("...", 60, y, g.DIM)
            y += row_h
        # scrollbar
        n = len(items)
        if n > rows_fit:
            bar_h = max(6, (g.HEIGHT - y0) * rows_fit // n)
            bar_y = y0 + (g.HEIGHT - y0 - bar_h) * self.top // (n - rows_fit)
            gfx.draw_rect(g.WIDTH - 2, bar_y, 2, bar_h, g.ACCENT, fill=True)
        if self.status:
            gfx.draw_text(self.status, 4, g.HEIGHT - 9, g.YELLOW)

    def _tf_tabs(self, y):
        gfx = self.gfx
        x = 2
        for i, tf in enumerate(_TFS):
            label = tf[0]
            w = 8 * len(label) + 4
            if i == self.tf:
                gfx.draw_rect(x, y, w, 11, g.ACCENT, fill=True)
                gfx.draw_text(label, x + 2, y + 2, g.BLACK)
            else:
                gfx.draw_rect(x, y, w, 11, g.DIM)
                gfx.draw_text(label, x + 2, y + 2, g.GREY)
            x += w + 2

    def _draw_chart(self, closes, x, y, w, h):
        gfx = self.gfx
        gfx.draw_rect(x, y, w, h, g.PANEL)
        pts = closes
        if not pts or len(pts) < 2:
            gfx.draw_text("no chart data", x + 6, y + h // 2 - 4, g.DIM)
            return
        lo = min(pts)
        hi = max(pts)
        rng = (hi - lo) or 1
        n = len(pts)
        col = g.GREEN if pts[-1] >= pts[0] else g.RED
        base_y = y + h - 1 - int((pts[0] - lo) / rng * (h - 2))
        for bx in range(x + 1, x + w - 1, 4):
            gfx.pixel(bx, base_y, g.DIM)
        prev = None
        for i, v in enumerate(pts):
            px = x + 1 + int(i / (n - 1) * (w - 3))
            py = y + h - 1 - int((v - lo) / rng * (h - 2))
            if prev is not None:
                gfx.line(prev[0], prev[1], px, py, col)
            prev = (px, py)
        gfx.draw_text(_fmt(hi), x + 2, y + 1, g.GREEN)
        gfx.draw_text(_fmt(lo), x + 2, y + h - 9, g.RED)

    def _render_detail(self):
        gfx = self.gfx
        sym = self.detail_sym
        q = self.chart
        y = g.CONTENT_Y + 2
        gfx.draw_text(_label(sym), 4, y, g.ACCENT, scale=2)
        if q is None:
            gfx.draw_text("loading", g.WIDTH - 8 * 7 - 2, y + 4, g.YELLOW)
            self._tf_tabs(y + 22)
            if self.status:
                gfx.draw_text(self.status, 4, g.HEIGHT - 9, g.YELLOW)
            return
        price = q.get("price")
        pct = q.get("pct", 0)
        col = g.GREEN if pct >= 0 else g.RED
        pstr = _fmt(price)
        # price right-aligned (scale 2 = 16px/char), clamped so it can't hit the label
        px = max(8 * 2 * 4, g.WIDTH - 16 * len(pstr) - 2)
        gfx.draw_text(pstr, px, y, g.WHITE, scale=2)
        gfx.draw_text("%s%.2f%% today" % ("+" if pct >= 0 else "", pct), 4, y + 18, col,
                     max_w=g.WIDTH - 8)
        self._tf_tabs(y + 28)
        closes = q.get("closes", [])
        cy = y + 42
        self._draw_chart(closes, 2, cy, g.WIDTH - 4, 50)
        # period change overlaid in the chart's free top-right corner
        if len(closes) >= 2 and closes[0]:
            ppct = (closes[-1] - closes[0]) / closes[0] * 100
            pc = g.GREEN if ppct >= 0 else g.RED
            txt = "%s %s%.1f%%" % (_TFS[self.tf][0], "+" if ppct >= 0 else "", ppct)
            gfx.draw_text(txt, g.WIDTH - 8 * len(txt) - 5, cy + 2, pc, bg=g.PANEL)
        s = "H%s L%s Prev%s" % (_fmt(q.get("high")), _fmt(q.get("low")), _fmt(q.get("prev")))
        gfx.draw_text(s, 3, g.HEIGHT - 9, g.GREY)

    def _render_search(self):
        gfx = self.gfx
        y = g.CONTENT_Y + 4
        gfx.draw_text("SEARCH", 4, y, g.ACCENT)
        # typed query
        shown = self.query + "_"
        gfx.draw_text(shown, 4, y + 14, g.WHITE, scale=2)
        # windowed ribbon centered on highlight
        cy = y + 44
        win = 9
        half = win // 2
        gfx.draw_text("J/L move   I pick   K bksp", 4, cy - 11, g.DIM)
        cx = 4
        for off in range(-half, half + 1):
            idx = (self.hpos + off) % len(_RIBBON)
            cell = _RIBBON[idx]
            txt = cell if len(cell) > 1 else cell
            sel = (off == 0)
            w = 8 * len(txt) + 4
            if sel:
                gfx.draw_rect(cx, cy, w, 13, g.ACCENT, fill=True)
                gfx.draw_text(txt, cx + 2, cy + 3, g.BLACK)
            else:
                gfx.draw_text(txt, cx + 2, cy + 3, g.GREY)
            cx += w + 2
            if cx > g.WIDTH - 6:
                break
        gfx.draw_text("scroll to OK to search", 4, g.HEIGHT - 10, g.GREY)
        if self.status:
            gfx.draw_text(self.status, 4, g.HEIGHT - 20, g.YELLOW)
