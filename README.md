# Sprig Dashboard

A multi-app information dashboard for the **Hack Club Sprig** (Raspberry Pi Pico WH
on the Sprig carrier board), written in **MicroPython** with `uasyncio` for a
non-blocking UI and networking.

Four apps, switched with the D-pad cluster:

| Button | App | What it does |
|--------|-----|--------------|
| **W** | Weather | Current temp (large), conditions, 3-day min/max bar chart (Open-Meteo) |
| **S** | Finance | One scrolling ticker list (incl. S&P 500, Dow, QQQ, futures); drill-down with a **price chart + selectable timeframe** (1D/5D/1M/6M/1Y); single-press ribbon search. All list prices fetched in **one** Yahoo *spark* request; fetches retry across query1/query2 hosts |
| **D** | Sports | Two modes (**double-tap I/L** toggles **LIVE ↔ STANDINGS**). Tabs (J/L): **ALL · F1 · NFL · NBA · MLB**. **ALL** is the universal default in both modes — all in-progress games across every league. League tabs show that league's scoreboard (LIVE) or table (STANDINGS): F1 = **drivers + constructors** (two scrollable sections), others = W-L tables. ESPN public API; all fetches retried |
| **A** | Ghost Sniffer | Three J/L views: RF signal histogram + waterfall + open-network count; **2.4 GHz channel-congestion analyzer**; and a **Sprig system monitor** (die temp, CPU clock, RAM/flash, uptime, IP/MAC, RSSI) |

All data sources are **$0-cost, key-free public endpoints**.

## Controls

The right-hand action cluster (**I J K L**) navigates inside the active app:

- **I** = up · **K** = down · **J** = left / prev tab · **L** = right / next tab
- **double-tap I or L** → select / drill-down
- **double-tap J or K** → back / cancel

The left D-pad (**W A S D**) switches apps at any time.

Per-app extras:
- **Finance list:** I/K scroll all tickers in one list; double-I/L opens detail; the last row is search.
- **Finance search:** instant single-press (no double-click) — J/L move the ribbon, **I** picks the highlighted key, **K** backspaces; scroll to **OK** to search or **CANCEL** to exit.
- **Finance detail:** J/L change the chart timeframe, I/K flip to the prev/next ticker without leaving the chart.
- **Sports:** J/L switch tab (ALL/F1/NFL/NBA/MLB); **ALL** = all live games everywhere (the universal default in both modes). **Double-tap I/L toggles LIVE ↔ STANDINGS** (the `LV`/`ST` pill at top-right shows which). I/K scroll; standings show ▲/▼ when there's more off-screen.
- **Ghost Sniffer:** J/L cycle RF scan → channel analyzer → system monitor; I/K scroll APs in RF view.

## Hardware pin map

Taken from the stock Sprig MicroPython firmware — do not change unless your
board differs.

| Function | GPIO |
|----------|------|
| Display SPI0 SCK / MOSI / MISO | 18 / 19 / 16 |
| Display CS / DC / RST | 20 / 22 / 26 |
| Buttons W A S D | 5 6 7 8 |
| Buttons I J K L | 12 13 14 15 |
| Audio (PWM piezo, configurable) | `config.AUDIO_PIN` (default 27) |

> **Audio note:** the stock Sprig uses an I²S **MAX98357A** amplifier, *not* a
> PWM piezo. This project drives a simple PWM buzzer on a configurable GPIO (a
> rear-exposed pin by default). Set `AUDIO_PIN = None` in `config.py` to mute,
> or change it to match your carrier.

## Setup

1. Flash the **latest stable MicroPython** for the Pico W / Pico 2 W onto the board.
2. Copy `config.example.py` → `config.py` and set your `WIFI_SSID` / `WIFI_PASS`
   (and optionally your `LAT` / `LON`, tickers, focus teams).
3. Upload every `.py` file to the device root (e.g. with `mpremote`):

   ```sh
   mpremote connect auto cp *.py :
   mpremote connect auto reset
   ```

   `main.py` runs automatically on boot.

## Files

| File | Role |
|------|------|
| `main.py` | App router + global input handler (double-click logic), async task loop |
| `gfx_engine.py` | ST7735 driver + framebuffer primitives (`draw_text`, `draw_rect`, sprites) |
| `wifi_manager.py` | Async Wi-Fi connect + non-blocking HTTP(S)/JSON client |
| `app_base.py` | Base `App` class and navigation contract |
| `store.py` | JSON flash cache (instant boot, survives power cycles) |
| `weather_app.py` | Open-Meteo integration |
| `finance_app.py` | Yahoo Finance tickers + drill-down + search |
| `sports_app.py` | ESPN scoreboard aggregator |
| `network_app.py` | "Ghost Sniffer" RSSI visualizer |
| `config.py` | Local secrets/config (git-ignored) |

## Performance & caching

The dashboard is built to be usable instantly and seamless after a short warm-up:

- **Boot preload.** On power-up every app starts fetching immediately (the
  active app first, then the rest in the background) — you don't have to open an
  app to make it load. Initial warm-up is roughly ~10 s on a good connection.
- **Adaptive background scheduler.** The active app always has priority; other
  apps load one at a time. "Heavy" data (sports scoreboards/standings) is only
  warmed while you're on a *light* screen (weather/finance/scanner), so spare
  capacity is used for heavy pulls and the device isn't piled with work while
  you're already on a data-heavy screen. Off-screen apps refresh on a relaxed
  cadence; a background prefetcher warms all standings (F1 drivers +
  constructors, NFL/NBA/MLB) one league at a time and then **idles once warm**,
  so it doesn't waste power.
- **Flash cache.** Each app writes its last-known data to `/cdata/*.json`
  (`store.py`) and reloads it on boot, so subsequent power-ups paint real values
  instantly and just refresh in the background. Writes are atomic and throttled
  (≥2 min/file) to spare flash.
- **Placeholders.** Apps ship with built-in placeholder values so the very
  first boot (before any cache exists) is never a blank screen.
- **Fewer/cheaper requests.** Finance pulls the whole list in one *spark*
  request; sports streams + parses on the fly (never buffering the 100-350 KB
  payloads) and only the visible/needed league is fetched. The RF scanner is
  the only app that doesn't run in the background (its `wlan.scan()` blocks).
- **One connection at a time.** All network I/O is serialized behind a single
  lock — a Pico W can only afford one TLS connection's buffers at once, so
  concurrent fetches were the main cause of `[Errno 12]` (out-of-memory). Sports
  also keeps only the *currently viewed* league's data in RAM (everything else
  on flash), freeing heap for the TLS buffer so refreshes stop failing.
- **Always-on fetching.** Whatever screen you're on refreshes on its own
  cadence (sports every 30 s while watching), so live scores keep updating.
- **No overflow.** All text is truncated to the screen width (no overlap / no
  half-glyphs cut at the edge); long lists scroll with I/K.

## Design notes & constraints

- **Memory:** a full 160×128 RGB565 framebuffer is 40 KB. The HTTP client caps
  responses at `HTTP_MAX_BYTES` (90 KB) and `gc.collect()`s aggressively around
  every fetch. Sports only loads the **active** league tab at a time, since ESPN
  scoreboard JSON is the largest payload. Very busy game days can still approach
  the limit — if a tab fails to parse, switch away and back to retry.
- **Networking:** all requests use `Connection: close` and handle chunked
  transfer-encoding. TLS uses `uasyncio.open_connection(..., ssl=True)`, which
  requires a recent MicroPython build.
- **Clock:** the header clock is **UTC** via `ntptime` (no timezone math).
- **Colour order:** `gfx_engine.color565()` pre-swaps bytes so framebuffer
  colours render correctly on the ST7735. Flip the swap there if your panel
  shows red/blue inverted.

## License

MIT — do whatever you like.
