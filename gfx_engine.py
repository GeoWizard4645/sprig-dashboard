# gfx_engine.py
# ---------------------------------------------------------------------------
# ST7735 (1.8" 160x128 landscape) graphics engine for the Hack Club Sprig.
#
# Bundles the low-level ST7735 SPI driver together with a framebuffer-backed
# primitive API: fill, draw_rect, draw_text (with integer scaling), draw_sprite,
# bar/line helpers and a single show() blit.
#
# Pin map taken from the stock Sprig MicroPython firmware:
#   SPI0  SCK=18  MOSI=19  MISO=16
#   LCD   CS=20   DC=22    RST=26
#   (SD card CS=21 lives on the same bus; unused here)
# ---------------------------------------------------------------------------

import framebuf
import time
from machine import Pin, SPI

WIDTH = 160
HEIGHT = 128
HEADER_H = 13          # reserved top status bar
CONTENT_Y = HEADER_H + 1

# --- Pins (fixed on Sprig hardware) ----------------------------------------
_PIN_SCK = 18
_PIN_MOSI = 19
_PIN_MISO = 16
_PIN_CS = 20
_PIN_DC = 22
_PIN_RST = 26

# --- ST7735 command set ----------------------------------------------------
_SWRESET = 0x01
_SLPOUT = 0x11
_NORON = 0x13
_INVOFF = 0x20
_DISPON = 0x29
_CASET = 0x2A
_RASET = 0x2B
_RAMWR = 0x2C
_MADCTL = 0x36
_COLMOD = 0x3A
_FRMCTR1 = 0xB1
_FRMCTR2 = 0xB2
_FRMCTR3 = 0xB3
_INVCTR = 0xB4
_PWCTR1 = 0xC0
_PWCTR2 = 0xC1
_PWCTR3 = 0xC2
_PWCTR4 = 0xC3
_PWCTR5 = 0xC4
_VMCTR1 = 0xC5
_GMCTRP1 = 0xE0
_GMCTRN1 = 0xE1


def color565(r, g, b):
    """Pack RGB into the byte order the panel wants.

    MicroPython's framebuf stores RGB565 little-endian, but the ST7735 reads
    big-endian, so we pre-swap the two bytes here. Define every theme colour
    through this helper and colours render correctly straight from the buffer.
    """
    c = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
    return ((c & 0xFF) << 8) | (c >> 8)


# --- Retro-Modern high-contrast palette ------------------------------------
BLACK = 0x0000
WHITE = 0xFFFF
BG = color565(6, 8, 18)
PANEL = color565(18, 22, 40)
HEADER_BG = color565(28, 32, 60)
FG = WHITE
ACCENT = color565(0, 230, 180)      # teal
ACCENT2 = color565(255, 120, 40)    # orange
GREEN = color565(40, 220, 120)
RED = color565(245, 60, 70)
YELLOW = color565(250, 210, 40)
BLUE = color565(70, 150, 255)
GREY = color565(120, 124, 150)
DIM = color565(70, 74, 100)


class GFX:
    def __init__(self):
        self.spi = SPI(0, baudrate=30000000, polarity=0, phase=0,
                       sck=Pin(_PIN_SCK), mosi=Pin(_PIN_MOSI), miso=Pin(_PIN_MISO))
        self.cs = Pin(_PIN_CS, Pin.OUT, value=1)
        self.dc = Pin(_PIN_DC, Pin.OUT, value=0)
        self.rst = Pin(_PIN_RST, Pin.OUT, value=1)
        # Full-screen RGB565 framebuffer (160*128*2 = 40 KB).
        self.buf = bytearray(WIDTH * HEIGHT * 2)
        self.fb = framebuf.FrameBuffer(self.buf, WIDTH, HEIGHT, framebuf.RGB565)
        # 8x8 scratch glyph for scaled text.
        self._gbuf = bytearray(8)
        self._glyph = framebuf.FrameBuffer(self._gbuf, 8, 8, framebuf.MONO_HLSB)
        self._init_panel()

    # -- low level ----------------------------------------------------------
    def _cmd(self, c):
        self.dc(0)
        self.cs(0)
        self.spi.write(bytes((c,)))
        self.cs(1)

    def _data(self, *vals):
        self.dc(1)
        self.cs(0)
        self.spi.write(bytes(vals))
        self.cs(1)

    def _init_panel(self):
        self.rst(1); time.sleep_ms(50)
        self.rst(0); time.sleep_ms(50)
        self.rst(1); time.sleep_ms(120)
        self._cmd(_SWRESET); time.sleep_ms(150)
        self._cmd(_SLPOUT); time.sleep_ms(255)
        self._cmd(_FRMCTR1); self._data(0x01, 0x2C, 0x2D)
        self._cmd(_FRMCTR2); self._data(0x01, 0x2C, 0x2D)
        self._cmd(_FRMCTR3); self._data(0x01, 0x2C, 0x2D, 0x01, 0x2C, 0x2D)
        self._cmd(_INVCTR); self._data(0x07)
        self._cmd(_PWCTR1); self._data(0xA2, 0x02, 0x84)
        self._cmd(_PWCTR2); self._data(0xC5)
        self._cmd(_PWCTR3); self._data(0x0A, 0x00)
        self._cmd(_PWCTR4); self._data(0x8A, 0x2A)
        self._cmd(_PWCTR5); self._data(0x8A, 0xEE)
        self._cmd(_VMCTR1); self._data(0x0E)
        self._cmd(_INVOFF)
        self._cmd(_MADCTL); self._data(0x60)   # landscape, 160x128
        self._cmd(_COLMOD); self._data(0x05)    # 16-bit colour
        self._cmd(_GMCTRP1)
        self._data(0x02, 0x1C, 0x07, 0x12, 0x37, 0x32, 0x29, 0x2D,
                   0x29, 0x25, 0x2B, 0x39, 0x00, 0x01, 0x03, 0x10)
        self._cmd(_GMCTRN1)
        self._data(0x03, 0x1D, 0x07, 0x06, 0x2E, 0x2C, 0x29, 0x2D,
                   0x2E, 0x2E, 0x37, 0x3F, 0x00, 0x00, 0x02, 0x10)
        self._cmd(_NORON); time.sleep_ms(10)
        self._cmd(_DISPON); time.sleep_ms(120)

    def show(self):
        """Blit the whole framebuffer to the panel."""
        self._cmd(_CASET); self._data(0x00, 0x00, 0x00, 0x9F)
        self._cmd(_RASET); self._data(0x00, 0x00, 0x00, 0x7F)
        self._cmd(_RAMWR)
        self.dc(1)
        self.cs(0)
        self.spi.write(self.buf)
        self.cs(1)

    # -- primitives ---------------------------------------------------------
    def fill(self, c=BG):
        self.fb.fill(c)

    def draw_rect(self, x, y, w, h, c, fill=False):
        if fill:
            self.fb.fill_rect(x, y, w, h, c)
        else:
            self.fb.rect(x, y, w, h, c)

    def hline(self, x, y, w, c):
        self.fb.hline(x, y, w, c)

    def vline(self, x, y, h, c):
        self.fb.vline(x, y, h, c)

    def pixel(self, x, y, c):
        self.fb.pixel(x, y, c)

    def line(self, x0, y0, x1, y1, c):
        self.fb.line(x0, y0, x1, y1, c)

    def draw_text(self, s, x, y, c=FG, scale=1, bg=None):
        """Draw text. scale=1 uses the native 8x8 font; scale>1 upscales it.

        Returns the x just past the last glyph (handy for chaining).
        """
        s = str(s)
        if scale <= 1:
            if bg is not None:
                self.fb.fill_rect(x, y, 8 * len(s), 8, bg)
            self.fb.text(s, x, y, c)
            return x + 8 * len(s)
        cx = x
        fill_rect = self.fb.fill_rect
        glyph = self._glyph
        for ch in s:
            glyph.fill(0)
            glyph.text(ch, 0, 0, 1)
            if bg is not None:
                fill_rect(cx, y, 8 * scale, 8 * scale, bg)
            for yy in range(8):
                for xx in range(8):
                    if glyph.pixel(xx, yy):
                        fill_rect(cx + xx * scale, y + yy * scale, scale, scale, c)
            cx += 8 * scale
        return cx

    def text_center(self, s, y, c=FG, scale=1, bg=None):
        w = 8 * scale * len(str(s))
        self.draw_text(s, (WIDTH - w) // 2, y, c, scale, bg)

    def draw_sprite(self, buf, x, y, w, h, key=-1):
        """Blit a raw RGB565 sprite buffer (already in panel byte order)."""
        spr = framebuf.FrameBuffer(buf, w, h, framebuf.RGB565)
        if key >= 0:
            self.fb.blit(spr, x, y, key)
        else:
            self.fb.blit(spr, x, y)

    # -- composite widgets --------------------------------------------------
    def header(self, app_name, wifi_ok, clock=""):
        self.fb.fill_rect(0, 0, WIDTH, HEADER_H, HEADER_BG)
        self.fb.text(app_name, 3, 3, ACCENT)
        dot = GREEN if wifi_ok else RED
        self.fb.fill_rect(WIDTH - 9, 4, 5, 5, dot)
        if clock:
            self.fb.text(clock, WIDTH - 9 - 8 * len(clock) - 4, 3, GREY)
        self.fb.hline(0, HEADER_H, WIDTH, DIM)

    def vbar(self, x, y, w, h, frac, c, bgc=PANEL):
        """Vertical bar filled bottom-up to `frac` (0..1)."""
        if frac < 0:
            frac = 0
        elif frac > 1:
            frac = 1
        self.fb.fill_rect(x, y, w, h, bgc)
        fh = int(h * frac)
        self.fb.fill_rect(x, y + (h - fh), w, fh, c)
