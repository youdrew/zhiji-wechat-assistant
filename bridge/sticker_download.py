"""Bounded downloads from WeChat's official sticker CDN, without proxy or redirects.

Copyright 2026 Know Your Friends contributors. Licensed under Apache-2.0.
URLs and transport errors stay private; callers receive only a generic failure.
"""
from __future__ import annotations

import http.client
import ipaddress
import queue
import socket
import ssl
import threading
import time
from urllib.parse import unquote, urlsplit


MAX_BYTES = 8 * 1024 * 1024
TOTAL_TIMEOUT = 15.0
IO_TIMEOUT = 5.0
_SLOTS = threading.BoundedSemaphore(4)
_HOST_PATHS = {
    "mmemoticon.qpic.cn": ("/mmemoticon/", "/mm_emoticon/"),
    "mmbiz.qpic.cn": ("/mmemoticon/", "/mm_emoticon/"),
    "vweixinf.tc.qq.com": ("/110/", "/262/", "/275/"),
    "wxapp.tc.qq.com": ("/110/", "/262/", "/275/"),
    "snsvideo.hk.wechat.com": ("/110/", "/262/", "/275/"),
    "snsvideo.c2c.wechat.com": ("/110/", "/262/", "/275/"),
}


class StickerDownloadError(RuntimeError):
    """A safe, URL-free error for every unavailable remote sticker."""

    def __init__(self):
        super().__init__("sticker-download-unavailable")


def _target(url):
    if (not isinstance(url, str) or not 0 < len(url) <= 16384 or not url.isascii()
            or any(ord(char) <= 32 or ord(char) == 127 for char in url)
            or "\\" in url or "#" in url):
        raise StickerDownloadError()
    parts = urlsplit(url)
    if (parts.scheme not in {"http", "https"} or parts.hostname not in _HOST_PATHS
            or parts.username is not None or parts.password is not None
            or parts.port not in ({None, 80} if parts.scheme == "http" else {None, 443})):
        raise StickerDownloadError()
    path = unquote(parts.path)
    prefixes = _HOST_PATHS[parts.hostname]
    if (not any(parts.path.startswith(prefix) and len(parts.path) > len(prefix) for prefix in prefixes)
            or not path.startswith(prefixes) or "\\" in path
            or any(segment in {".", ".."} for segment in path.split("/"))
            or any(ord(char) <= 32 or ord(char) == 127 for char in path)):
        raise StickerDownloadError()
    # The legacy endpoint serves the same signed resources at this verified host;
    # keep certificate validation enabled and preserve the opaque resource exactly.
    host = "wxapp.tc.qq.com" if parts.hostname == "vweixinf.tc.qq.com" else parts.hostname
    return host, parts.path + ("?" + parts.query if parts.query else "")


def _public_address(host):
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    if not addresses:
        raise StickerDownloadError()
    for family, kind, protocol, _name, address in addresses:
        ip = ipaddress.ip_address(address[0])
        if (family not in {socket.AF_INET, socket.AF_INET6} or kind != socket.SOCK_STREAM
                or protocol not in {0, socket.IPPROTO_TCP} or "%" in address[0]
                or not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            raise StickerDownloadError()
    # All answers were checked; connect directly to this literal address, never resolve again.
    family, kind, protocol, _name, address = addresses[0]
    return family, kind, protocol, address


def _remaining(deadline, cancelled):
    left = deadline - time.monotonic()
    if left <= 0 or cancelled.is_set():
        raise StickerDownloadError()
    return min(IO_TIMEOUT, left)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address, deadline, cancelled):
        super().__init__(host, 443, timeout=IO_TIMEOUT, context=ssl.create_default_context())
        self.address = address
        self.deadline = deadline
        self.cancelled = cancelled
        self.transport = None

    def connect(self):
        family, kind, protocol, address = self.address
        raw = socket.socket(family, kind, protocol)
        self.transport = self.sock = raw
        try:
            raw.settimeout(_remaining(self.deadline, self.cancelled))
            raw.connect(address)
            raw.settimeout(_remaining(self.deadline, self.cancelled))
            # The host, not the pinned IP, supplies SNI and certificate hostname validation.
            self.transport = self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
            self.sock.settimeout(_remaining(self.deadline, self.cancelled))
        except Exception:
            raw.close()
            raise

    def abort(self):
        transport = self.transport
        if transport is not None:
            try:
                transport.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            transport.close()
        self.close()


def _receive(host, target, limit, deadline, cancelled, transfer):
    address = _public_address(host)
    _remaining(deadline, cancelled)
    connection = _PinnedHTTPSConnection(host, address, deadline, cancelled)
    transfer[0] = connection
    response = None
    try:
        connection.request("GET", target, headers={"Accept": "image/*", "Accept-Encoding": "identity"})
        connection.transport.settimeout(_remaining(deadline, cancelled))
        response = connection.getresponse()
        if response.status != 200 or response.getheader("Content-Encoding", "identity").lower() != "identity":
            raise StickerDownloadError()
        lengths = [value.strip() for key, value in response.getheaders() if key.lower() == "content-length"]
        if (len(lengths) > 1 or (lengths and (not lengths[0].isascii() or not lengths[0].isdigit()
                or int(lengths[0]) > limit))):
            raise StickerDownloadError()
        content = bytearray()
        while True:
            connection.transport.settimeout(_remaining(deadline, cancelled))
            chunk = response.read1(min(65536, limit + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > limit:
                raise StickerDownloadError()
        _remaining(deadline, cancelled)
        if not content or (lengths and len(content) != int(lengths[0])):
            raise StickerDownloadError()
        return bytes(content)
    finally:
        if response is not None:
            response.close()
        connection.close()


def download_sticker(url, max_bytes=MAX_BYTES):
    """Fetch at most 8 MiB over verified HTTPS, with four slots and a 15s total budget.

    A timed-out DNS lookup retains its slot until it returns, so blocked resolver
    calls cannot create unbounded threads. No URL, cookie, proxy or redirect is exposed.
    """
    deadline = time.monotonic() + TOTAL_TIMEOUT
    try:
        host, target = _target(url)
        if type(max_bytes) is not int or not 0 < max_bytes <= MAX_BYTES:
            raise StickerDownloadError()
        if not _SLOTS.acquire(timeout=max(0, deadline - time.monotonic())):
            raise StickerDownloadError()
        result = queue.Queue(maxsize=1)
        cancelled = threading.Event()
        transfer = [None]

        def run():
            try:
                data = _receive(host, target, max_bytes, deadline, cancelled, transfer)
            except Exception:
                data = None
            finally:
                _SLOTS.release()
            result.put(data)

        worker = threading.Thread(target=run, name="sticker-download", daemon=True)
        try:
            worker.start()
        except Exception:
            _SLOTS.release()
            raise
        try:
            data = result.get(timeout=max(0, deadline - time.monotonic()))
            if data is None:
                raise StickerDownloadError()
            return data
        finally:
            cancelled.set()
            if transfer[0] is not None:
                transfer[0].abort()
    except Exception:
        raise StickerDownloadError() from None
