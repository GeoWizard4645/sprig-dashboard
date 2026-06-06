# wifi_manager.py
# ---------------------------------------------------------------------------
# Async Wi-Fi connection handling + a small non-blocking HTTP(S) client built
# on uasyncio.open_connection. Everything here yields to the event loop so the
# UI never freezes while the radio is busy.
#
# The HTTP client requests `Connection: close` and reads to EOF, decoding
# chunked transfer-encoding when present. A hard byte cap protects the Pico's
# limited RAM against oversized responses.
# ---------------------------------------------------------------------------

import network
import uasyncio as asyncio
import time
import json
import gc

import config

_UA = "Mozilla/5.0 (Linux; PicoW) SprigDashboard/1.0"


class WifiManager:
    def __init__(self, ssid=None, pwd=None):
        self.ssid = ssid or config.WIFI_SSID
        self.pwd = pwd or config.WIFI_PASS
        self.wlan = network.WLAN(network.STA_IF)
        self.status = "off"

    async def connect(self, timeout=None):
        timeout = timeout or config.WIFI_TIMEOUT_S
        self.wlan.active(True)
        if self.wlan.isconnected():
            self.status = "connected"
            return True
        self.status = "connecting"
        try:
            self.wlan.connect(self.ssid, self.pwd)
        except Exception as e:
            self.status = "error: %s" % e
            return False
        t0 = time.ticks_ms()
        while not self.wlan.isconnected():
            if time.ticks_diff(time.ticks_ms(), t0) > timeout * 1000:
                self.status = "timeout"
                return False
            await asyncio.sleep_ms(250)
        self.status = "connected"
        return True

    async def ensure(self):
        """Reconnect if the link dropped. Safe to call before every request."""
        if self.wlan.isconnected():
            return True
        return await self.connect()

    @property
    def connected(self):
        return self.wlan.isconnected()

    def ifconfig(self):
        try:
            return self.wlan.ifconfig()
        except Exception:
            return ("0.0.0.0",) * 4


# --- low-level HTTP --------------------------------------------------------
def _parse_url(url):
    if url.startswith("https://"):
        proto, rest = "https", url[8:]
    elif url.startswith("http://"):
        proto, rest = "http", url[7:]
    else:
        raise ValueError("bad url")
    if "/" in rest:
        hostport, path = rest.split("/", 1)
        path = "/" + path
    else:
        hostport, path = rest, "/"
    if ":" in hostport:
        host, port = hostport.split(":")
        port = int(port)
    else:
        host = hostport
        port = 443 if proto == "https" else 80
    return proto, host, port, path


async def http_get_raw(url, headers=None, timeout=None, max_bytes=None):
    """Return the full raw HTTP response (headers + body) as bytes."""
    timeout = timeout or config.HTTP_TIMEOUT_S
    max_bytes = max_bytes or config.HTTP_MAX_BYTES
    proto, host, port, path = _parse_url(url)
    use_ssl = proto == "https"
    gc.collect()

    reader = writer = None
    try:
        conn = asyncio.open_connection(host, port, ssl=use_ssl) if use_ssl \
            else asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(conn, timeout)

        req = ("GET %s HTTP/1.1\r\n"
               "Host: %s\r\n"
               "User-Agent: %s\r\n"
               "Accept: application/json,*/*\r\n"
               "Connection: close\r\n") % (path, host, _UA)
        if headers:
            for k, v in headers.items():
                req += "%s: %s\r\n" % (k, v)
        req += "\r\n"
        writer.write(req.encode())
        await asyncio.wait_for(writer.drain(), timeout)

        buf = bytearray()
        while True:
            try:
                chunk = await asyncio.wait_for(reader.read(512), timeout)
            except Exception:
                break
            if not chunk:
                break
            buf += chunk
            if len(buf) > max_bytes:
                break
        return bytes(buf)
    finally:
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        gc.collect()


def _split_response(resp):
    i = resp.find(b"\r\n\r\n")
    if i < 0:
        return 0, False, b""
    head = resp[:i]
    body = resp[i + 4:]
    try:
        status = int(head.split(b" ")[1])
    except Exception:
        status = 0
    chunked = head.lower().find(b"transfer-encoding: chunked") >= 0
    return status, chunked, body


def _dechunk(body):
    out = bytearray()
    i = 0
    n = len(body)
    while i < n:
        j = body.find(b"\r\n", i)
        if j < 0:
            break
        try:
            size = int(body[i:j], 16)
        except Exception:
            break
        if size == 0:
            break
        start = j + 2
        out += body[start:start + size]
        i = start + size + 2
    return bytes(out)


async def http_json(url, headers=None, timeout=None, max_bytes=None):
    """GET `url` and parse JSON. Returns (status, data). Raises on parse fail."""
    resp = await http_get_raw(url, headers, timeout, max_bytes)
    status, chunked, body = _split_response(resp)
    del resp
    gc.collect()
    if chunked:
        body = _dechunk(body)
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        # some MicroPython ports want a str, not bytes
        data = json.loads(body.decode())
    del body
    gc.collect()
    return status, data
