# weather_app.py  (button W)
# ---------------------------------------------------------------------------
# Open-Meteo current conditions + 3-day forecast. No API key required.
#   Big current temperature, short condition text, wind/humidity,
#   and a 3-day min/max range bar chart.
# ---------------------------------------------------------------------------
#3
import gfx_engine as g
from app_base import App
from wifi_manager import http_json
import config
import store

# shown instantly on the very first boot (before any fetch / cache exists)
_PLACEHOLDER = {
    "current": {"temperature_2m": None, "weather_code": 0, "wind_speed_10m": None,
                "relative_humidity_2m": None, "apparent_temperature": None},
    "daily": {"temperature_2m_max": [], "temperature_2m_min": [], "weather_code": []},
}

# WMO weather interpretation codes -> short labels.
_WMO = {
    0: "Clear", 1: "Mostly Clear", 2: "Part Cloud", 3: "Overcast",
    45: "Fog", 48: "Rime Fog", 51: "Drizzle", 53: "Drizzle", 55: "Drizzle",
    56: "Frz Drizzle", 57: "Frz Drizzle", 61: "Rain", 63: "Rain", 65: "Hvy Rain",
    66: "Frz Rain", 67: "Frz Rain", 71: "Snow", 73: "Snow", 75: "Hvy Snow",
    77: "Snow Grains", 80: "Showers", 81: "Showers", 82: "Vlt Showers",
    85: "Snow Show", 86: "Snow Show", 95: "Thunder", 96: "Thunderstorm",
    99: "Thunderstorm",
}


def _code_text(code):
    return _WMO.get(code, "Code %s" % code)


class WeatherApp(App):
    name = "WEATHER"
    refresh_interval = config.WEATHER_REFRESH_S

    def __init__(self, gfx, wifi):
        super().__init__(gfx, wifi)
        self.data = store.load("weather", _PLACEHOLDER)
        self.unit = "F" if config.TEMP_UNIT == "fahrenheit" else "C"

    async def refresh(self):
        if not await self.wifi.ensure():
            self.status = "no wifi"
            return
        url = ("https://api.open-meteo.com/v1/forecast"
               "?latitude=%s&longitude=%s"
               "&current=temperature_2m,weather_code,wind_speed_10m,relative_humidity_2m,apparent_temperature"
               "&daily=temperature_2m_max,temperature_2m_min,weather_code"
               "&forecast_days=3&timezone=auto"
               "&temperature_unit=%s&wind_speed_unit=%s") % (
                   config.LAT, config.LON, config.TEMP_UNIT, config.WIND_UNIT)
        try:
            status, data = await http_json(url)
            if status == 200 and "current" in data:
                self.data = data
                self.status = ""
                store.save("weather", data)
            else:
                self.status = "http %s" % status
        except Exception as e:
            self.status = "err: %s" % e
        self.dirty = True

    def render(self):
        gfx = self.gfx
        if self.data is None:
            self.msg(self.status or "loading...", g.YELLOW if self.status else g.GREY)
            return
        cur = self.data["current"]
        daily = self.data["daily"]
        temp = cur.get("temperature_2m")
        code = cur.get("weather_code", 0)
        wind = cur.get("wind_speed_10m")
        hum = cur.get("relative_humidity_2m")
        feels = cur.get("apparent_temperature")

        # --- big current temperature ---
        tstr = "--" if temp is None else "%d" % round(temp)
        gfx.draw_text(tstr, 4, g.CONTENT_Y + 4, g.WHITE, scale=5)
        tw = 8 * 5 * len(tstr)
        # degree ring + unit
        gfx.draw_rect(4 + tw + 3, g.CONTENT_Y + 6, 5, 5, g.ACCENT)
        gfx.draw_text(self.unit, 4 + tw + 10, g.CONTENT_Y + 8, g.ACCENT, scale=2)

        # condition + a single compact stats line that fits the 160px width
        gfx.draw_text(_code_text(code), 6, g.CONTENT_Y + 46, g.ACCENT)
        f = "--" if feels is None else "%d" % round(feels)
        h = "--" if hum is None else "%d" % round(hum)
        w = "--" if wind is None else "%d" % round(wind)
        gfx.draw_text("Feels %s%s H%s%% W%s" % (f, self.unit, h, w),
                     4, g.CONTENT_Y + 56, g.GREY)

        # --- 3-day min/max range chart ---
        self._chart(daily)

    def _chart(self, daily):
        gfx = self.gfx
        mx = daily.get("temperature_2m_max", [])
        mn = daily.get("temperature_2m_min", [])
        codes = daily.get("weather_code", [])
        n = min(3, len(mx), len(mn))
        if n == 0:
            return
        vals = [v for v in (mx[:n] + mn[:n]) if v is not None]
        if not vals:
            return
        lo, hi = min(vals), max(vals)
        rng = (hi - lo) or 1
        base_y = g.HEIGHT - 6
        top_y = g.CONTENT_Y + 70
        ch_h = base_y - top_y
        col_w = g.WIDTH // n
        days = ("TODAY", "DAY+1", "DAY+2")
        for i in range(n):
            cx = i * col_w + col_w // 2
            dmax, dmin = mx[i], mn[i]
            if dmax is None or dmin is None:
                continue
            y_max = base_y - int((dmax - lo) / rng * ch_h)
            y_min = base_y - int((dmin - lo) / rng * ch_h)
            col = g.RED if dmax >= (lo + hi) / 2 else g.BLUE
            # range bar
            gfx.draw_rect(cx - 5, y_max, 10, max(2, y_min - y_max), col, fill=True)
            gfx.draw_text("%d" % round(dmax), cx - 8, y_max - 9, g.WHITE)
            gfx.draw_text("%d" % round(dmin), cx - 8, y_min + 2, g.GREY)
            gfx.draw_text(days[i] if i < 3 else "+%d" % i, cx - len(days[i]) * 4, base_y + 0, g.DIM)
