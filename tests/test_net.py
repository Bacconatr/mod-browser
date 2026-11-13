"""The fetch adapter's hardening, against a real local server (the allow-list is widened to 127.0.0.1)."""

from __future__ import annotations

import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mod_browser.net import CachingHttp, FetchError, FetchHttp


class Server:
    def __init__(self, handler):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def local(max_bytes: int = 1000, timeout: float = 2.0) -> FetchHttp:
    return FetchHttp(hosts=("127.0.0.1",), schemes=("http",), timeout=timeout, jar_timeout=timeout,
                     max_json=max_bytes, max_jar=max_bytes)


class FetchTest(unittest.TestCase):
    def serve(self, handler) -> str:
        server = Server(handler)
        self.addCleanup(server.close)
        return server.base

    def test_refuses_a_host_that_is_not_on_the_allow_list_or_a_plain_http_scheme(self):
        with self.assertRaisesRegex(FetchError, "refusing to fetch example.com"):
            FetchHttp().json("https://example.com/x")
        with self.assertRaisesRegex(FetchError, "refusing to fetch"):
            FetchHttp().json("http://api.modrinth.com/x")

    def test_does_not_follow_a_redirect_to_a_host_that_is_not_allowed(self):
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "http://localhost:1/steal")
                self.end_headers()
            log_message = lambda *a: None  # noqa: E731
        with self.assertRaisesRegex(FetchError, "refusing to fetch localhost"):
            local().json(self.serve(H) + "/x")

    def test_follows_a_redirect_to_an_allowed_host_but_only_a_few_times(self):
        class Loop(BaseHTTPRequestHandler):
            n = 0
            def do_GET(self):
                Loop.n += 1
                self.send_response(302)
                self.send_header("Location", f"/loop{Loop.n}")
                self.end_headers()
            log_message = lambda *a: None  # noqa: E731

        class Once(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/ok":
                    body = b'{"ok": true}'
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(302)
                    self.send_header("Location", "/ok")
                    self.end_headers()
            log_message = lambda *a: None  # noqa: E731
        with self.assertRaisesRegex(FetchError, "too many redirects"):
            local().json(self.serve(Loop) + "/start")
        self.assertEqual(local().json(self.serve(Once) + "/go"), {"ok": True})

    def test_stops_reading_a_body_once_it_passes_the_cap_even_with_no_content_length(self):
        sent = []

        class Big(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"  # no Content-Length, body ends when the connection closes
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                try:
                    for _ in range(400):
                        self.wfile.write(b"x" * 500)
                        self.wfile.flush()
                        sent.append(500)
                        time.sleep(0.005)
                except OSError:
                    pass
            log_message = lambda *a: None  # noqa: E731
        with self.assertRaisesRegex(FetchError, "too large"):
            local(1000).fetch_jar(self.serve(Big) + "/big")
        time.sleep(0.1)
        self.assertLess(sum(sent), 20_000, f"kept sending: {sum(sent)} bytes")

    def test_refuses_a_declared_length_over_the_cap_without_reading(self):
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "5000")
                self.end_headers()
                self.wfile.write(b"x" * 5000)
            log_message = lambda *a: None  # noqa: E731
        with self.assertRaisesRegex(FetchError, "too large"):
            local(1000).fetch_jar(self.serve(H) + "/x")

    def test_a_server_that_drips_bytes_is_cut_off_by_the_overall_deadline(self):
        class Drip(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                try:
                    for _ in range(100):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        time.sleep(0.1)
                except OSError:
                    pass
            log_message = lambda *a: None  # noqa: E731
        started = time.monotonic()
        with self.assertRaises(FetchError):
            local(10_000, timeout=0.5).fetch_jar(self.serve(Drip) + "/slow")
        self.assertLess(time.monotonic() - started, 3)

    def test_a_non_200_answer_is_an_error(self):
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(403)
                self.end_headers()
            log_message = lambda *a: None  # noqa: E731
        with self.assertRaisesRegex(FetchError, "answered 403"):
            local().json(self.serve(H) + "/x")


class CachingTest(unittest.TestCase):
    def test_a_repeated_question_is_answered_from_memory_until_the_ttl_passes(self):
        class Inner:
            calls = 0
            def json(self, url, headers=None):
                Inner.calls += 1
                return {"n": Inner.calls}
        now = [0.0]
        cache = CachingHttp(Inner(), ttl=10, clock=lambda: now[0])
        self.assertEqual(cache.json("https://x/a"), {"n": 1})
        self.assertEqual(cache.json("https://x/a"), {"n": 1})
        self.assertEqual(cache.json("https://x/b"), {"n": 2})
        now[0] = 11
        self.assertEqual(cache.json("https://x/a"), {"n": 3})


if __name__ == "__main__":
    unittest.main()
