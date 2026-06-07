# config.example.py
# ---------------------------------------------------------------------------
# Copy this file to `config.py` and fill in your own values.
# `config.py` is git-ignored so your secrets never get committed.
# ---------------------------------------------------------------------------

# --- Wi-Fi credentials -----------------------------------------------------
WIFI_SSID = "YOUR_SSID"
WIFI_PASS = "YOUR_PASSWORD"
WIFI_TIMEOUT_S = 20          # seconds before a connect attempt is abandoned

# --- Location (Weather app, Open-Meteo) ------------------------------------
# Defaults to New York City. Look up your own lat/long.
LAT = 40.7128
LON = -74.0060
TEMP_UNIT = "fahrenheit"     # "fahrenheit" or "celsius"
WIND_UNIT = "mph"            # "mph" | "kmh" | "ms" | "kn"

# --- Finance app -----------------------------------------------------------
# Yahoo Finance symbols. ^ and = characters are URL-encoded automatically.
# NOTE: silver futures is SI=F on Yahoo (SL=F is not a valid symbol).
FINANCE_PAGE_1 = ["BTC-USD", "SI=F", "CL=F", "^IXIC"]
# ^GSPC = S&P 500, ^DJI = Dow Jones, QQQ = Nasdaq-100 ETF, SGU=F = futures
FINANCE_PAGE_2 = ["GC=F", "MSTR", "AAPL", "NVDA", "ETH-USD", "MSFT", "JPY=X",
                  "^GSPC", "^DJI", "QQQ", "SGU=F"]

# --- Sports app ------------------------------------------------------------
# Priority tiers (tab order). Each entry: (label, espn_path, focus_team_abbr)
SPORTS_TIERS = [
    ("F1",  "racing/f1",       None),
    ("NFL", "football/nfl",    "NYG"),   # Giants
    ("NBA", "basketball/nba",  "NY"),    # Knicks
    ("MLB", "baseball/mlb",    "NYY"),   # Yankees
]

# --- Hardware --------------------------------------------------------------
# The Sprig display/buttons/LED pins are fixed in gfx_engine.py.
# Only the audio pin is configurable here because carrier boards vary.
# The stock Sprig uses an I2S MAX98357A amp; a PWM piezo on a free GPIO
# (e.g. one of the rear-exposed pins) is assumed here. Set to None to mute.
AUDIO_PIN = 27               # PWM-capable GPIO for a piezo buzzer, or None

# --- Tuning ----------------------------------------------------------------
WEATHER_REFRESH_S = 900      # 15 min
FINANCE_REFRESH_S = 60       # 1 min
SPORTS_REFRESH_S = 60        # 1 min
NET_SCAN_S = 4               # Ghost Sniffer rescan interval
HTTP_TIMEOUT_S = 12
HTTP_MAX_BYTES = 90000       # hard cap to protect the Pico's RAM
