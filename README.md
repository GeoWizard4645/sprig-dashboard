# Sprig Dashboard

A multi-app information dashboard for the **Hack Club Sprig** (Raspberry Pi Pico WH
on the Sprig carrier board), written in **MicroPython** with `uasyncio` for a
non-blocking UI and networking.

Four apps, switched with the D-pad cluster:

| Button | App | What it does |
|--------|-----|--------------|
| **W** | Weather | Current temp (large), conditions, 3-day min/max bar chart (Open-Meteo) |
| **S** | Finance | Ticker dashboard, 2 pages, drill-down + custom ticker search (Yahoo Finance) |
| **D** | Sports | F1 / NFL / NBA / MLB scores & league board (ESPN public API) |
| **A** | Ghost Sniffer | Live WLAN RSSI histogram + signal waterfall (`wlan.scan()`) |

All data sources are **$0-cost, key-free public endpoints**.

## Controls

The right-hand action cluster (**I J K L**) navigates inside the active app:

- **I** = up · **K** = down · **J** = left / prev tab · **L** = right / next tab
- **double-tap I or L** → select / drill-down
- **double-tap J or K** → back / cancel

The left D-pad (**W A S D**) switches apps at any time.

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
| `weather_app.py` | Open-Meteo integration |
| `finance_app.py` | Yahoo Finance tickers + drill-down + search |
| `sports_app.py` | ESPN scoreboard aggregator |
| `network_app.py` | "Ghost Sniffer" RSSI visualizer |
| `config.py` | Local secrets/config (git-ignored) |

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
