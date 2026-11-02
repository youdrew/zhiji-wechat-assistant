"""Synthetic sticker transport tests; no live DNS, CDN or private message URLs.

Copyright 2026 Know Your Friends contributors. Licensed under Apache-2.0.
"""
import socket
import ssl
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import sticker_download as downloader
from sticker_download import StickerDownloadError, download_sticker


URL = "http://mmbiz.qpic.cn/mmemoticon/synthetic?token=synthetic-private"
ADDRESS = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, ("8.8.8.8", 443))


class FakeResponse:
    def __init__(self, body=b"GIF89a-synthetic", status=200, headers=None):
        self.body = body
        self.status = status
        self.headers = headers or []
        self.closed = False
        self.read_sizes = []

    def getheader(self, name, default=None):
        return next((value for key, value in self.headers if key.lower() == name.lower()), default)

    def getheaders(self):
        return self.headers

    def read1(self, size):
        self.read_sizes.append(size)
        chunk, self.body = self.body[:size], self.body[size:]
        return chunk

    def close(self):
        self.closed = True


class DownloadTests(unittest.TestCase):
    def fetch(self, response=None, url=URL, limit=downloader.MAX_BYTES, failure=None):
        connection = MagicMock()
        connection.getresponse.return_value = response or FakeResponse()
        if failure:
            connection.getresponse.side_effect = failure
        with patch.object(downloader, "_public_address", return_value=ADDRESS) as dns, \
                patch.object(downloader, "_PinnedHTTPSConnection", return_value=connection) as constructor:
            data = download_sticker(url, limit)
        return data, connection, dns, constructor

    def test_official_http_upgrades_to_https_without_cookies_or_proxy(self):
        response = FakeResponse(headers=[("Set-Cookie", "must-not-be-persisted")])
        with patch.dict("os.environ", {"HTTPS_PROXY": "http://127.0.0.1:1", "HTTP_PROXY": "http://127.0.0.1:1"}):
            data, connection, dns, constructor = self.fetch(response)
        self.assertEqual(data, b"GIF89a-synthetic")
        dns.assert_called_once_with("mmbiz.qpic.cn")
        self.assertEqual(constructor.call_args.args[:2], ("mmbiz.qpic.cn", ADDRESS))
        connection.request.assert_called_once_with("GET", "/mmemoticon/synthetic?token=synthetic-private",
            headers={"Accept": "image/*", "Accept-Encoding": "identity"})
        self.assertTrue(response.closed)
        connection.close.assert_called()

    def test_both_official_hosts_and_paths_are_accepted(self):
        for host in ("mmemoticon.qpic.cn", "mmbiz.qpic.cn"):
            for prefix in ("mmemoticon", "mm_emoticon"):
                self.assertEqual(self.fetch(url=f"https://{host}:443/{prefix}/synthetic")[0], b"GIF89a-synthetic")

    def test_official_download_hosts_use_their_own_path_families_over_https(self):
        for host in ("vweixinf.tc.qq.com", "wxapp.tc.qq.com", "snsvideo.hk.wechat.com", "snsvideo.c2c.wechat.com"):
            for prefix in ("110", "262", "275"):
                with self.subTest(host=host, prefix=prefix):
                    data, connection, dns, constructor = self.fetch(url=f"http://{host}/{prefix}/synthetic/stodownload?token=synthetic")
                    self.assertEqual(data, b"GIF89a-synthetic")
                    selected_host = "wxapp.tc.qq.com" if host == "vweixinf.tc.qq.com" else host
                    dns.assert_called_once_with(selected_host)
                    self.assertEqual(constructor.call_args.args[:2], (selected_host, ADDRESS))
                    connection.request.assert_called_once_with("GET", f"/{prefix}/synthetic/stodownload?token=synthetic",
                        headers={"Accept": "image/*", "Accept-Encoding": "identity"})

    def test_only_verified_legacy_host_is_replaced_preserving_the_exact_resource(self):
        target = "/110/synthetic%2Bresource/stodownload?token=a%2Fb%3D&x=1&x=2&empty="
        _, connection, dns, constructor = self.fetch(url="https://vweixinf.tc.qq.com" + target)
        dns.assert_called_once_with("wxapp.tc.qq.com")
        self.assertEqual(constructor.call_args.args[:2], ("wxapp.tc.qq.com", ADDRESS))
        self.assertEqual(connection.request.call_args.args, ("GET", target))
        for host in ("wxapp.tc.qq.com", "snsvideo.hk.wechat.com", "snsvideo.c2c.wechat.com"):
            _, _, dns, constructor = self.fetch(url="https://" + host + target)
            dns.assert_called_once_with(host)
            self.assertEqual(constructor.call_args.args[0], host)

    def test_legacy_mapping_validates_the_original_host_path_and_has_no_failure_fallback(self):
        with patch.object(downloader, "_public_address") as dns:
            for url in ("https://vweixinf.tc.qq.com/mmemoticon/synthetic",
                    "https://vweixinf.tc.qq.com.evil.invalid/110/synthetic",
                    "https://vweixinf.tc.qq.com/110/../other"):
                with self.assertRaises(StickerDownloadError):
                    download_sticker(url)
            dns.assert_not_called()
        with patch.object(downloader, "_public_address", return_value=ADDRESS) as dns, \
                patch.object(downloader, "_PinnedHTTPSConnection", side_effect=ssl.SSLError("synthetic")) as constructor:
            with self.assertRaises(StickerDownloadError):
                download_sticker("https://vweixinf.tc.qq.com/110/synthetic/stodownload")
            dns.assert_called_once_with("wxapp.tc.qq.com")
            self.assertEqual(constructor.call_count, 1)

    def test_cdn_hosts_cannot_borrow_another_hosts_paths_or_expand_the_domain_allowlist(self):
        urls = [f"https://{host}/{path}" for host, paths in (
            ("vweixinf.tc.qq.com", ("mmemoticon/synthetic", "mm_emoticon/synthetic", "111/synthetic", "110/")),
            ("wxapp.tc.qq.com", ("mmemoticon/synthetic", "other/synthetic", "262/")),
            ("snsvideo.hk.wechat.com", ("mmemoticon/synthetic", "275", "1100/synthetic")),
            ("snsvideo.c2c.wechat.com", ("mmemoticon/synthetic", "download/synthetic", "275/../other")),
            ("mmbiz.qpic.cn", ("110/synthetic", "262/synthetic", "275/synthetic")),
            ("mmemoticon.qpic.cn", ("110/synthetic", "262/synthetic", "275/synthetic")),
            ("vweixinf.tc.qq.com.evil.invalid", ("110/synthetic",)),
            ("other.tc.qq.com", ("110/synthetic",)),
            ("other.wechat.com", ("110/synthetic",)),
            ("snsvideo.hk.wechat.com.", ("110/synthetic",)),
        ) for path in paths]
        with patch.object(downloader, "_public_address") as dns:
            for url in urls:
                with self.assertRaises(StickerDownloadError):
                    download_sticker(url)
            dns.assert_not_called()

    def test_invalid_urls_fail_before_dns_and_never_include_the_input(self):
        urls = [None, "", "http://127.0.0.1/mmemoticon/a", "https://mmbiz.qpic.cn.evil.invalid/mmemoticon/a",
            "https://mmbiz.qpic.cn@evil.invalid/mmemoticon/a", "https://a@mmbiz.qpic.cn/mmemoticon/a",
            "https://mmbiz.qpic.cn:80/mmemoticon/a", "http://mmbiz.qpic.cn:443/mmemoticon/a",
            "https://mmbiz.qpic.cn./mmemoticon/a", "file://mmbiz.qpic.cn/mmemoticon/a",
            "https://mmbiz.qpic.cn/other/a", "https://mmbiz.qpic.cn/mmemoticon/",
            "https://mmbiz.qpic.cn/mmemoticon/a#fragment", "https://mmbiz.qpic.cn/mmemoticon/a#",
            "https://mmbiz.qpic.cn/mmemoticon/a\r\nCookie: secret", " https://mmbiz.qpic.cn/mmemoticon/a",
            "https://mmbiz.qpic.cn/mmemoticon/../a", "https://mmbiz.qpic.cn/mmemoticon/%2e%2e/a",
            "https://mmbiz.qpic.cn/mmemoticon/%0a", "https://mmbiz.qpic.cn/mmemoticon/a\\b",
            "https://mmbiz.qpic.cn:bad/mmemoticon/a", "https://mmbiz.qpic.cn/mmemoticon/中文",
            "https://mmbiz.qpic.cn/mmemoticon/" + "a" * 17000]
        with patch.object(downloader, "_public_address") as dns:
            for url in urls:
                with self.subTest(url_type=type(url).__name__), self.assertRaises(StickerDownloadError) as caught:
                    download_sticker(url)
                self.assertEqual(str(caught.exception), "sticker-download-unavailable")
            dns.assert_not_called()

    def test_invalid_limits_are_rejected_before_connecting(self):
        with patch.object(downloader, "_public_address") as dns:
            for limit in (True, 0, -1, 1.5, "10", downloader.MAX_BYTES + 1):
                with self.assertRaises(StickerDownloadError):
                    download_sticker(URL, limit)
            dns.assert_not_called()

    def test_size_is_enforced_for_announced_unannounced_and_truncated_bodies(self):
        rejected = [FakeResponse(body=b"12345", headers=[("Content-Length", "5")]),
            FakeResponse(body=b"12345"), FakeResponse(body=b"123", headers=[("Content-Length", "4")]),
            FakeResponse(body=b"1234", headers=[("Content-Length", "-1")]),
            FakeResponse(body=b"1234", headers=[("Content-Length", "4"), ("Content-Length", "4")]),
            FakeResponse(body=b""), FakeResponse(body=b"1234", headers=[("Content-Encoding", "gzip")])]
        for response in rejected:
            with self.assertRaises(StickerDownloadError):
                self.fetch(response, limit=4)
            self.assertTrue(response.closed)
            self.assertTrue(all(size <= 5 for size in response.read_sizes))
        self.assertEqual(self.fetch(FakeResponse(body=b"1234", headers=[("Content-Length", "4")]), limit=4)[0], b"1234")

    def test_non_200_redirects_and_transport_failures_are_generic(self):
        for status in (301, 302, 307, 401, 404, 500):
            with self.assertRaises(StickerDownloadError):
                self.fetch(FakeResponse(status=status, headers=[("Location", "http://127.0.0.1/private")]))
        for failure in (TimeoutError(URL), ssl.SSLError(URL), OSError(URL)):
            with self.assertRaises(StickerDownloadError) as caught:
                self.fetch(failure=failure)
            self.assertEqual(str(caught.exception), "sticker-download-unavailable")
            self.assertTrue(caught.exception.__suppress_context__)

    def test_body_read_timeout_is_closed_and_never_exposes_transport_details(self):
        response = FakeResponse()
        with patch.object(response, "read1", side_effect=TimeoutError(URL)):
            with self.assertRaises(StickerDownloadError) as caught:
                self.fetch(response)
        self.assertEqual(str(caught.exception), "sticker-download-unavailable")
        self.assertTrue(response.closed)

    def test_total_timeout_aborts_an_established_connection_even_during_slow_headers(self):
        aborted = threading.Event()
        slots = threading.BoundedSemaphore(1)
        connection = MagicMock()
        def slow_headers():
            aborted.wait(2)
            return FakeResponse()
        connection.getresponse.side_effect = slow_headers
        connection.abort.side_effect = aborted.set
        with patch.object(downloader, "TOTAL_TIMEOUT", .05), patch.object(downloader, "_SLOTS", slots), \
                patch.object(downloader, "_public_address", return_value=ADDRESS), \
                patch.object(downloader, "_PinnedHTTPSConnection", return_value=connection):
            started = time.monotonic()
            with self.assertRaises(StickerDownloadError):
                download_sticker(URL)
            self.assertLess(time.monotonic() - started, .5)
            self.assertTrue(aborted.is_set())
            self.assertTrue(slots.acquire(timeout=1))
            slots.release()
            connection.close.assert_called()

    def test_slow_dns_returns_on_total_deadline_and_keeps_its_slot_until_finished(self):
        release_dns = threading.Event()
        slots = threading.BoundedSemaphore(1)
        def slow_dns(_host):
            release_dns.wait(2)
            return ADDRESS
        with patch.object(downloader, "TOTAL_TIMEOUT", .05), patch.object(downloader, "_SLOTS", slots), \
                patch.object(downloader, "_public_address", side_effect=slow_dns), \
                patch.object(downloader, "_PinnedHTTPSConnection") as connection:
            started = time.monotonic()
            try:
                with self.assertRaises(StickerDownloadError):
                    download_sticker(URL)
                self.assertLess(time.monotonic() - started, .5)
                self.assertFalse(slots.acquire(blocking=False))
            finally:
                release_dns.set()
                self.assertTrue(slots.acquire(timeout=1))
                slots.release()
            connection.assert_not_called()

    def test_waiting_for_a_slot_counts_against_total_deadline(self):
        slots = threading.BoundedSemaphore(1)
        slots.acquire()
        with patch.object(downloader, "TOTAL_TIMEOUT", .03), patch.object(downloader, "_SLOTS", slots), \
                patch.object(downloader, "_public_address") as dns:
            with self.assertRaises(StickerDownloadError):
                download_sticker(URL)
            dns.assert_not_called()

    def test_concurrent_downloads_never_exceed_four_workers(self):
        lock, all_started, release = threading.Lock(), threading.Event(), threading.Event()
        active = maximum = 0
        def receive(*_args):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                if active == 4:
                    all_started.set()
            release.wait(2)
            with lock:
                active -= 1
            return b"synthetic"
        with patch.object(downloader, "_receive", side_effect=receive), \
                patch.object(downloader, "_SLOTS", threading.BoundedSemaphore(4)), ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(download_sticker, URL) for _ in range(8)]
            try:
                self.assertTrue(all_started.wait(1))
                self.assertEqual(maximum, 4)
            finally:
                release.set()
            self.assertTrue(all(future.result(timeout=2) == b"synthetic" for future in futures))
            self.assertEqual(maximum, 4)


class ConnectionTests(unittest.TestCase):
    def test_dns_rejects_every_non_public_answer_even_when_first_answer_is_public(self):
        public = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443))
        for address in ("127.0.0.1", "192.168.1.1", "10.0.0.1", "169.254.169.254", "100.64.0.1",
                "224.0.0.1", "0.0.0.0", "192.0.2.1", "::1", "fd00::1", "fe80::1", "ff02::1"):
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            private = (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))
            with patch.object(socket, "getaddrinfo", return_value=[public, private]):
                with self.assertRaises(StickerDownloadError):
                    downloader._public_address("mmbiz.qpic.cn")
        with patch.object(socket, "getaddrinfo", return_value=[public]) as resolve:
            self.assertEqual(downloader._public_address("mmbiz.qpic.cn"), ADDRESS)
            resolve.assert_called_once_with("mmbiz.qpic.cn", 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)

    def test_connection_pins_ip_and_tls_validates_the_original_host_without_another_lookup(self):
        raw, tls, context = MagicMock(), MagicMock(), MagicMock()
        context.wrap_socket.return_value = tls
        with patch.object(socket, "socket", return_value=raw) as socket_factory, \
                patch.object(socket, "getaddrinfo") as resolver, \
                patch.object(ssl, "create_default_context", return_value=context):
            connection = downloader._PinnedHTTPSConnection("mmbiz.qpic.cn", ADDRESS, time.monotonic() + 15, threading.Event())
            connection.connect()
            socket_factory.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
            raw.connect.assert_called_once_with(("8.8.8.8", 443))
            context.wrap_socket.assert_called_once_with(raw, server_hostname="mmbiz.qpic.cn")
            resolver.assert_not_called()
            self.assertEqual(raw.settimeout.call_args.args, (5.0,))
            connection.abort()
            tls.shutdown.assert_called_once_with(socket.SHUT_RDWR)

    def test_real_default_tls_context_requires_certificate_and_hostname_verification(self):
        connection = downloader._PinnedHTTPSConnection("mmbiz.qpic.cn", ADDRESS, time.monotonic() + 15, threading.Event())
        self.assertEqual(connection._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(connection._context.check_hostname)
        connection.close()


if __name__ == "__main__":
    unittest.main()
