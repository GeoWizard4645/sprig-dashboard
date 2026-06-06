# finance_app.py  (button S)
# ---------------------------------------------------------------------------
# Ticker dashboard backed by Yahoo Finance's public v8 chart endpoint
# (no key, no crumb required). Two pages of symbols, a drill-down detail
# view (% change + day high/low), and a cursor-based character selector for
# searching any custom ticker.
#
# States: "list" -> "detail" / "search"
# ---------------------------------------------------------------------------

import gfx_engine as g
import uasyncio as asyncio
from app_base import App
from wifi_manager import http_json
import config

_SEARCH = "__SEARCH__"
_CHARSET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-=^.&"


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


class FinanceApp(App):
    name = "FINANCE"
    refresh_interval = config.FINANCE_REFRESH_S

    def __init__(self, gfx, wifi):
        super().__init__(gfx, wifi)
        self.pages = [list(config.FINANCE_PAGE_1), list(config.FINANCE_PAGE_2)]
        self.page = 0
        self.cursor = 0
        self.quotes = {}              # symbol -> dict
        self.state = "list"
        self.detail_sym = None
        # search state
        self.query = ["A"]
        self.qpos = 0

    # -- data ---------------------------------------------------------------
    async def _fetch(self, sym):
        url = ("https://query1.finance.yahoo.com/v8/finance/chart/%s"
               "?range=1d&interval=15m") % _enc(sym)
        status, data = await http_json(url, max_bytes=40000)
        res = data["chart"]["result"][0]
        meta = res["meta"]
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        high = meta.get("regularMarketDayHigh")
        low = meta.get("regularMarketDayLow")
        if high is None or low is None:
            try:
                q = res["indicators"]["quote"][0]
                hs = [x for x in q.get("high", []) if x is not None]
                ls = [x for x in q.get("low", []) if x is not None]
                if hs and high is None:
                    high = max(hs)
                if ls and low is None:
                    low = min(ls)
            except Exception:
                pass
        pct = ((price - prev) / prev * 100) if (price and prev) else 0.0
        return {"price": price, "prev": prev, "high": high, "low": low, "pct": pct}

    async def refresh(self):
        if not await self.wifi.ensure():
            self.status = "no wifi"
            self.dirty = True
            return
        symbols = self.pages[0] + self.pages[1]
        ok = 0
        for sym in symbols:
            try:
                self.quotes[sym] = await self._fetch(sym)
                ok += 1
            except Exception as e:
                self.quotes.setdefault(sym, {"price": None, "pct": 0, "err": str(e)})
            self.dirty = True
            await asyncio.sleep_ms(50)   # be kind to the event loop + the API
        self.status = "" if ok else "fetch failed"

    async def _search_now(self, sym):
        self.status = "searching %s..." % sym
        self.dirty = True
        try:
            self.quotes[sym] = await self._fetch(sym)
            self.detail_sym = sym
            self.state = "detail"
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
        elif self.state == "search":
            i = _CHARSET.find(self.query[self.qpos])
            self.query[self.qpos] = _CHARSET[(i + 1) % len(_CHARSET)]
        self.dirty = True

    def on_down(self):
        if self.state == "list":
            self.cursor = (self.cursor + 1) % len(self._items())
        elif self.state == "search":
            i = _CHARSET.find(self.query[self.qpos])
            self.query[self.qpos] = _CHARSET[(i - 1) % len(_CHARSET)]
        self.dirty = True

    def on_left(self):
        if self.state == "list":
            self.page = (self.page - 1) % len(self.pages)
            self.cursor = 0
        elif self.state == "search" and self.qpos > 0:
            self.qpos -= 1
        self.dirty = True

    def on_right(self):
        if self.state == "list":
            self.page = (self.page + 1) % len(self.pages)
            self.cursor = 0
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
                self.detail_sym = item
                self.state = "detail"
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
                    arrow = "+" if pct >= 0 else ""
                    gfx.draw_text("%s%.2f%%" % (arrow, pct), 112, y, col)
                else:
                    gfx.draw_text("...", 60, y, g.DIM)
            y += row_h
        if self.status:
            gfx.draw_text(self.status, 4, g.HEIGHT - 9, g.YELLOW)

    def _render_detail(self):
        gfx = self.gfx
        sym = self.detail_sym
        q = self.quotes.get(sym, {})
        y = g.CONTENT_Y + 4
        gfx.draw_text(_label(sym), 6, y, g.ACCENT, scale=2)
        price = q.get("price")
        gfx.draw_text(_fmt(price), 6, y + 22, g.WHITE, scale=3)
        pct = q.get("pct", 0)
        col = g.GREEN if pct >= 0 else g.RED
        gfx.draw_text("%s%.2f%%" % ("+" if pct >= 0 else "", pct), 6, y + 50, col, scale=2)

        prev = q.get("prev")
        high = q.get("high")
        low = q.get("low")
        gfx.draw_text("Prev  %s" % _fmt(prev), 6, y + 72, g.GREY)
        gfx.draw_text("High %s" % _fmt(high), 6, y + 82, g.GREEN)
        gfx.draw_text("Low  %s" % _fmt(low), 84, y + 82, g.RED)

        # price position within the day range
        if price is not None and high is not None and low is not None and high > low:
            frac = (price - low) / (high - low)
            bx, bw, by = 6, g.WIDTH - 12, y + 94
            gfx.draw_rect(bx, by, bw, 6, g.PANEL, fill=True)
            gfx.draw_rect(bx + int(frac * (bw - 3)), by - 1, 3, 8, g.ACCENT, fill=True)
        gfx.draw_text("J/K back", g.WIDTH - 8 * 8 - 2, g.CONTENT_Y + 1, g.DIM)

    def _render_search(self):
        gfx = self.gfx
        y = g.CONTENT_Y + 6
        gfx.draw_text("SEARCH TICKER", 6, y, g.ACCENT)
        gfx.draw_text("I/K char  J/L move", 6, y + 10, g.DIM)
        # character cells
        cx = 8
        cy = y + 30
        for i, ch in enumerate(self.query):
            sel = (i == self.qpos)
            box = g.ACCENT if sel else g.DIM
            gfx.draw_rect(cx, cy, 16, 22, box)
            gfx.draw_text(ch, cx + 4, cy + 7, g.WHITE if sel else g.GREY, scale=1)
            if sel:
                gfx.draw_text("^", cx + 4, cy + 24, g.ACCENT)
                gfx.draw_text("v", cx + 4, cy - 9, g.ACCENT)
            cx += 19
        gfx.draw_text("dbl I/L = search", 6, g.HEIGHT - 20, g.GREY)
        gfx.draw_text("dbl J/K = cancel", 6, g.HEIGHT - 10, g.GREY)
        if self.status:
            gfx.draw_text(self.status, 6, cy + 30, g.YELLOW)
