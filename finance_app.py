# finance_app.py  (button S)
# ---------------------------------------------------------------------------
# Ticker dashboard backed by Yahoo Finance's public v8 chart endpoint
# (no key, no crumb required).
#   - Two pages of symbols with live price + % change
#   - Drill-down detail with a price CHART and selectable timeframe
#     (1D / 5D / 1M / 6M / 1Y), plus % change and day high/low
#   - Cursor-based character selector for searching any custom ticker
#
# States: "list" -> "detail" / "search"
# Detail nav:  J/L = change timeframe   I/K = prev/next ticker
#              dbl I/L = refresh         dbl J/K = back
# ---------------------------------------------------------------------------

import gfx_engine as g
import uasyncio as asyncio
from app_base import App
from wifi_manager import http_json
import config

_SEARCH = "__SEARCH__"
_CHARSET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-=^.&"

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


async def _fetch(sym, rng="1d", itv="15m", want_closes=False):
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/%s"
           "?range=%s&interval=%s") % (_enc(sym), rng, itv)
    status, data = await http_json(url, max_bytes=45000)
    res = data["chart"]["result"][0]
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
    pct = ((price - prev) / prev * 100) if (price and prev) else 0.0
    return {"price": price, "prev": prev, "high": high, "low": low,
            "pct": pct, "closes": closes}


class FinanceApp(App):
    name = "FINANCE"
    refresh_interval = config.FINANCE_REFRESH_S

    def __init__(self, gfx, wifi):
        super().__init__(gfx, wifi)
        self.pages = [list(config.FINANCE_PAGE_1), list(config.FINANCE_PAGE_2)]
        self.page = 0
        self.cursor = 0
        self.quotes = {}
        self.state = "list"
        self.detail_sym = None
        self.detail_idx = -1
        self.tf = 0
        self.chart = None          # dict for current sym+tf, or None while loading
        self._tok = 0
        # search state
        self.query = ["A"]
        self.qpos = 0

    # -- data ---------------------------------------------------------------
    async def refresh(self):
        if not await self.wifi.ensure():
            self.status = "no wifi"
            self.dirty = True
            return
        ok = 0
        for sym in self.pages[0] + self.pages[1]:
            try:
                self.quotes[sym] = await _fetch(sym)
                ok += 1
            except Exception as e:
                self.quotes.setdefault(sym, {"price": None, "pct": 0, "err": str(e)})
            self.dirty = True
            await asyncio.sleep_ms(40)
        self.status = "" if ok else "fetch failed"

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
            if tok == self._tok:        # ignore stale loads
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
        asyncio.create_task(self._load_chart())

    async def _search_now(self, sym):
        self.status = "searching %s..." % sym
        self.dirty = True
        try:
            await _fetch(sym)            # validate the symbol exists
            self._open_detail(sym, -1)
            self.status = ""
        except Exception:
            self.status = "not found: %s" % sym
        self.dirty = True

    # -- navigation ---------------------------------------------------------
    def _items(self):
        return self.pages[self.page] + [_SEARCH]

    def on_up(self):
        if self.state == "list":
            self.cursor = (self.cursor - 1) % len(self._items())
        elif self.state == "detail":
            syms = self.pages[self.page]
            if self.detail_idx >= 0 and syms:
                self.detail_idx = (self.detail_idx - 1) % len(syms)
                self._open_detail(syms[self.detail_idx], self.detail_idx)
        elif self.state == "search":
            i = _CHARSET.find(self.query[self.qpos])
            self.query[self.qpos] = _CHARSET[(i + 1) % len(_CHARSET)]
        self.dirty = True

    def on_down(self):
        if self.state == "list":
            self.cursor = (self.cursor + 1) % len(self._items())
        elif self.state == "detail":
            syms = self.pages[self.page]
            if self.detail_idx >= 0 and syms:
                self.detail_idx = (self.detail_idx + 1) % len(syms)
                self._open_detail(syms[self.detail_idx], self.detail_idx)
        elif self.state == "search":
            i = _CHARSET.find(self.query[self.qpos])
            self.query[self.qpos] = _CHARSET[(i - 1) % len(_CHARSET)]
        self.dirty = True

    def on_left(self):
        if self.state == "list":
            self.page = (self.page - 1) % len(self.pages)
            self.cursor = 0
        elif self.state == "detail":
            self.tf = (self.tf - 1) % len(_TFS)
            asyncio.create_task(self._load_chart())
        elif self.state == "search" and self.qpos > 0:
            self.qpos -= 1
        self.dirty = True

    def on_right(self):
        if self.state == "list":
            self.page = (self.page + 1) % len(self.pages)
            self.cursor = 0
        elif self.state == "detail":
            self.tf = (self.tf + 1) % len(_TFS)
            asyncio.create_task(self._load_chart())
        elif self.state == "search":
            if self.qpos < len(self.query) - 1:
                self.qpos += 1
            elif len(self.query) < 8:
                self.query.append("A")
                self.qpos += 1
        self.dirty = True

    def on_select(self):
        if self.state == "list":
            item = self._items()[self.cursor]
            if item == _SEARCH:
                self.state = "search"
                self.query = ["A"]
                self.qpos = 0
            else:
                self._open_detail(item, self.cursor)
        elif self.state == "detail":
            asyncio.create_task(self._load_chart())     # refresh
        elif self.state == "search":
            sym = "".join(self.query).strip()
            if sym:
                asyncio.create_task(self._search_now(sym))
        self.dirty = True

    def on_back(self):
        if self.state in ("detail", "search"):
            self.state = "list"
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
        y = g.CONTENT_Y + 2
        gfx.draw_text("PAGE %d/%d" % (self.page + 1, len(self.pages)), 4, y, g.ACCENT)
        gfx.draw_text("J/L page", g.WIDTH - 8 * 8 - 2, y, g.DIM)
        y += 12
        row_h = 13
        for i, item in enumerate(items):
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
        if self.status:
            gfx.draw_text(self.status, 4, g.HEIGHT - 9, g.YELLOW)

    def _tf_tabs(self, y):
        gfx = self.gfx
        x = 2
        for i, tf in enumerate(_TFS):
            label = tf[0]
            w = 8 * len(label) + 4
            active = (i == self.tf)
            if active:
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
        # dotted baseline at the first price (open reference)
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
            gfx.draw_text("loading...", g.WIDTH - 8 * 10 - 2, y + 4, g.YELLOW)
            self._tf_tabs(y + 22)
            if self.status:
                gfx.draw_text(self.status, 4, g.HEIGHT - 9, g.YELLOW)
            return
        price = q.get("price")
        pct = q.get("pct", 0)
        col = g.GREEN if pct >= 0 else g.RED
        gfx.draw_text(_fmt(price), g.WIDTH - 8 * len(_fmt(price)) - 2, y, g.WHITE, scale=2)
        gfx.draw_text("%s%.2f%% day" % ("+" if pct >= 0 else "", pct), 4, y + 18, col)
        # period change from the chart series
        closes = q.get("closes", [])
        if len(closes) >= 2 and closes[0]:
            ppct = (closes[-1] - closes[0]) / closes[0] * 100
            pc = g.GREEN if ppct >= 0 else g.RED
            txt = "%s %s%.1f%%" % (_TFS[self.tf][0], "+" if ppct >= 0 else "", ppct)
            gfx.draw_text(txt, g.WIDTH - 8 * len(txt) - 2, y + 18, pc)
        self._tf_tabs(y + 28)
        self._draw_chart(closes, 2, y + 42, g.WIDTH - 4, 52)
        # stat footer
        s = "H %s  L %s  Prev %s" % (_fmt(q.get("high")), _fmt(q.get("low")), _fmt(q.get("prev")))
        gfx.draw_text(s, 3, g.HEIGHT - 9, g.GREY)

    def _render_search(self):
        gfx = self.gfx
        y = g.CONTENT_Y + 6
        gfx.draw_text("SEARCH TICKER", 6, y, g.ACCENT)
        gfx.draw_text("I/K char  J/L move", 6, y + 10, g.DIM)
        cx = 8
        cy = y + 30
        for i, ch in enumerate(self.query):
            sel = (i == self.qpos)
            gfx.draw_rect(cx, cy, 16, 22, g.ACCENT if sel else g.DIM)
            gfx.draw_text(ch, cx + 4, cy + 7, g.WHITE if sel else g.GREY)
            if sel:
                gfx.draw_text("^", cx + 4, cy + 24, g.ACCENT)
                gfx.draw_text("v", cx + 4, cy - 9, g.ACCENT)
            cx += 19
        gfx.draw_text("dbl I/L = search", 6, g.HEIGHT - 20, g.GREY)
        gfx.draw_text("dbl J/K = cancel", 6, g.HEIGHT - 10, g.GREY)
        if self.status:
            gfx.draw_text(self.status, 6, cy + 32, g.YELLOW)
