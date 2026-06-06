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


# --- streaming (low-memory) HTTP for huge JSON payloads --------------------
class _Dechunker:
    """Feed raw HTTP/1.1 chunked bytes; emits decoded body to `sink`."""
    def __init__(self, sink):
        self.sink = sink
        self.state = 0          # 0=size line, 1=data, 2=trailing CRLF
        self.rem = 0
        self.hexbuf = bytearray()
        self.skip = 2
        self.done = False

    def feed(self, data):
        if self.done:
            return
        i = 0
        n = len(data)
        while i < n:
            if self.state == 0:
                j = data.find(b"\n", i)
                if j < 0:
                    self.hexbuf += data[i:]
                    return
                line = (bytes(self.hexbuf) + data[i:j]).strip()
                self.hexbuf = bytearray()
                i = j + 1
                k = line.find(b";")
                if k >= 0:
                    line = line[:k]
                try:
                    self.rem = int(line, 16) if line else 0
                except Exception:
                    self.rem = 0
                if self.rem == 0:
                    self.done = True
                    return
                self.state = 1
            elif self.state == 1:
                take = self.rem if self.rem < (n - i) else (n - i)
                if take:
                    self.sink(data[i:i + take])
                    i += take
                    self.rem -= take
                if self.rem == 0:
                    self.state = 2
                    self.skip = 2
            else:
                while i < n and self.skip > 0:
                    i += 1
                    self.skip -= 1
                if self.skip == 0:
                    self.state = 0


class JsonSax:
    """Incremental JSON scanner. Calls handler.start/end/value as it goes so
    callers can extract a few fields from a giant document without ever
    building the full object tree in RAM."""
    _WS = b" \t\r\n"

    def __init__(self, handler):
        self.h = handler
        self.stack = []
        self.pending_key = None
        self.in_str = False
        self.esc = False
        self.sbuf = bytearray()
        self.in_num = False
        self.nbuf = bytearray()
        self.has_pend = False
        self.pend = None

    def feed(self, data):
        ch = self._char
        for c in data:
            ch(c)

    def _endstr(self):
        try:
            self.pend = bytes(self.sbuf).decode()
        except Exception:
            self.pend = str(bytes(self.sbuf))
        self.has_pend = True

    def _flush(self):
        if self.has_pend:
            self.h.value(self.stack, self.pending_key, self.pend)
            self.has_pend = False

    def _endnum(self):
        self.in_num = False
        try:
            self.pend = bytes(self.nbuf).decode()
        except Exception:
            self.pend = ""
        self.has_pend = True

    def _open(self, kind):
        parent = self.stack[-1] if self.stack else None
        self.h.start(parent, kind, self.pending_key)
        self.stack.append((kind, self.pending_key))
        self.pending_key = None
        self.has_pend = False

    def _close(self, kind):
        item = self.stack.pop() if self.stack else (kind, None)
        parent = self.stack[-1] if self.stack else None
        self.h.end(parent, item[0], item[1])
        self.pending_key = None

    def _char(self, c):
        if self.in_str:
            if self.esc:
                self.sbuf.append(c)
                self.esc = False
            elif c == 92:        # backslash
                self.esc = True
            elif c == 34:        # closing quote
                self.in_str = False
                self._endstr()
            else:
                self.sbuf.append(c)
            return
        if self.in_num:
            if (48 <= c <= 57) or c in (43, 45, 46, 69, 101) or (97 <= c <= 122):
                self.nbuf.append(c)
                return
            self._endnum()      # number ended; fall through to handle c
        if c == 34:
            self.in_str = True
            self.sbuf = bytearray()
            return
        if c == 58:             # ':'
            if self.has_pend:
                self.pending_key = self.pend
                self.has_pend = False
            return
        if c in JsonSax._WS:
            return
        if c == 44:             # ','
            self._flush()
            self.pending_key = None
            return
        if c == 123:            # '{'
            self._open("o")
            return
        if c == 91:             # '['
            self._open("a")
            return
        if c == 125:            # '}'
            self._flush()
            self._close("o")
            return
        if c == 93:             # ']'
            self._flush()
            self._close("a")
            return
        # start of a number / true / false / null
        self.in_num = True
        self.nbuf = bytearray()
        self.nbuf.append(c)


async def http_stream(url, sink, headers=None, timeout=None):
    """GET `url` and stream the (dechunked) response body to `sink(bytes)`.
    Never buffers the whole body, so it's safe for very large JSON."""
    timeout = timeout or config.HTTP_TIMEOUT_S
    proto, host, port, path = _parse_url(url)
    use_ssl = proto == "https"
    gc.collect()
    reader = writer = None
    try:
        conn = asyncio.open_connection(host, port, ssl=use_ssl) if use_ssl \
            else asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(conn, timeout)
        req = ("GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: %s\r\n"
               "Accept: application/json\r\nConnection: close\r\n\r\n") % (path, host, _UA)
        writer.write(req.encode())
        await asyncio.wait_for(writer.drain(), timeout)

        # read until end of headers
        hbuf = bytearray()
        body0 = b""
        head = b""
        while True:
            ch = await asyncio.wait_for(reader.read(256), timeout)
            if not ch:
                break
            hbuf += ch
            idx = hbuf.find(b"\r\n\r\n")
            if idx >= 0:
                head = bytes(hbuf[:idx])
                body0 = bytes(hbuf[idx + 4:])
                break
        chunked = head.lower().find(b"transfer-encoding: chunked") >= 0
        deliver = _Dechunker(sink).feed if chunked else sink
        if body0:
            deliver(body0)
        while True:
            try:
                ch = await asyncio.wait_for(reader.read(512), timeout)
            except Exception:
                break
            if not ch:
                break
            deliver(ch)
    finally:
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        gc.collect()


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
