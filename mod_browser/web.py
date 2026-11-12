"""HTTP: the public site and the local-only admin API, on two listeners.

Public listener (behind Caddy, Internet-facing, no login):
  GET /             the page
  GET /static/...   its CSS and JS
  GET /api/status   server state, players, TPS, last restart, address
  GET /api/mods     the pack's mod list; ?q= &side= &kind= filter it (the
                    page's search box uses these, so matching lives here only)
  GET /api/health   liveness

Admin listener (bound to loopback inside the container; nothing proxies it):
  GET /admin-api/v1/health

Seams for the next tickets, deliberately not built yet:
  - The request queue: POST /api/requests and GET /api/requests go in
    PUBLIC_ROUTES once Queue exists. Requests carry only a Minecraft name.
  - /admin: the owner's page, on the public listener behind the vault
    password. Until then /admin is a plain 404 like any unknown path.
  - The admin API: PVE polls ADMIN_ROUTES through `pct exec` + `docker exec`
    for new requests and queue changes. It is never published or proxied,
    so it is the one place a write endpoint may go without a password.

Only GET and HEAD are served today; anything else is 405. A request that
carries a body (which no route reads) closes its connection after the
answer, so the unread body can never be parsed as the next request on a
connection Caddy reuses.
"""

from __future__ import annotations

import html
import json
import mimetypes
import sys
import threading
import traceback
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .pack import PackCatalog, search
from .status import StatusMonitor

STATIC = Path(__file__).parent / "static"
_MAX_QUERY = 200
# Connections served at once per listener. Caddy keeps a small pool of
# upstream connections; anything past this is closed at once rather than
# given a thread, so a burst cannot exhaust the container's pids limit.
MAX_CONNECTIONS = 48
# Seconds a connection may sit idle or half-sent before it is dropped.
SOCKET_TIMEOUT = 15

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
}

Response = tuple[int, str, bytes, dict[str, str]]


def _json(body: Any, status: int = 200) -> Response:
    return status, "application/json", json.dumps(body, separators=(",", ":")).encode(), {"Cache-Control": "no-store"}


def _error(status: HTTPStatus, message: str) -> Response:
    return _json({"error": message}, status.value)


class App:
    """The routes, with the ports wired in. Tests build one with fakes."""

    def __init__(self, monitor: StatusMonitor, catalog: PackCatalog, name: str = "Tel Aviv"):
        self.monitor, self.catalog, self.name = monitor, catalog, name
        text = (STATIC / "index.html").read_text()
        for key, value in {"name": name, "address": monitor.address}.items():
            text = text.replace("{{" + key + "}}", html.escape(value))
        self._page = text.encode()
        self._mods_json: tuple[str, bytes] | None = None

    # ---- public ----
    def page(self, query: dict[str, list[str]]) -> Response:
        return 200, "text/html; charset=utf-8", self._page, {"Cache-Control": "no-cache"}

    def status(self, query: dict[str, list[str]]) -> Response:
        return _json(self.monitor.snapshot())

    def mods(self, query: dict[str, list[str]]) -> Response:
        def one(name: str) -> str:
            return (query.get(name) or [""])[0][:_MAX_QUERY]

        data = self.catalog.catalog()
        q, side, kind = one("q"), one("side"), one("kind")
        if q or side or kind:
            found = search(data["mods"], q, side, kind)
            return _json({**data, "mods": found, "matched": len(found)})
        # The whole list is the common answer: encode it once per pack commit.
        cached = self._mods_json
        if cached is None or cached[0] != data["pack"]["commit"]:
            cached = self._mods_json = (data["pack"]["commit"], _json(data)[2])
        return 200, "application/json", cached[1], {"Cache-Control": "no-store"}

    def health(self, query: dict[str, list[str]]) -> Response:
        return _json({"ok": True})

    @property
    def public_routes(self) -> dict[str, Callable[[dict[str, list[str]]], Response]]:
        return {"/": self.page, "/api/status": self.status, "/api/mods": self.mods, "/api/health": self.health}

    # ---- admin (loopback only) ----
    @property
    def admin_routes(self) -> dict[str, Callable[[dict[str, list[str]]], Response]]:
        return {"/admin-api/v1/health": self.health}


@lru_cache(maxsize=32)
def _static(path: str) -> Response | None:
    if not path.startswith("/static/"):
        return None
    name = path.removeprefix("/static/")
    target = (STATIC / name).resolve()
    if STATIC.resolve() not in target.parents or not target.is_file():
        return None
    kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    if kind.startswith("text/") or kind == "application/javascript":
        kind += "; charset=utf-8"
    return 200, kind, target.read_bytes(), {"Cache-Control": "public, max-age=300"}


def _handler(routes: Callable[[], dict[str, Callable[..., Response]]], serve_static: bool):
    class Handler(BaseHTTPRequestHandler):
        server_version = "mod-browser"
        sys_version = ""
        protocol_version = "HTTP/1.1"
        timeout = SOCKET_TIMEOUT

        def log_message(self, fmt: str, *args: Any) -> None:  # Caddy logs requests
            pass

        def log_error(self, fmt: str, *args: Any) -> None:
            sys.stderr.write("mod-browser: " + (fmt % args) + "\n")

        def _has_body(self) -> bool:
            length = self.headers.get("Content-Length", "0").strip() or "0"
            return length != "0" or "Transfer-Encoding" in self.headers

        def _respond(self, response: Response, head: bool, extra: dict[str, str] | None = None) -> None:
            status, kind, body, headers = response
            if self._has_body():
                self.close_connection = True
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            for key, value in {**SECURITY_HEADERS, **headers, **(extra or {})}.items():
                self.send_header(key, value)
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def _dispatch(self, head: bool) -> None:
            url = urlsplit(self.path)
            route = routes().get(url.path)
            try:
                if route:
                    response = route(parse_qs(url.query))
                else:
                    response = (_static(url.path) if serve_static else None) or _error(HTTPStatus.NOT_FOUND, "not found")
            except Exception:  # noqa: BLE001 - one bad read must not kill the listener
                sys.stderr.write(f"mod-browser: {url.path} failed\n{traceback.format_exc()}")
                response = _error(HTTPStatus.SERVICE_UNAVAILABLE, "temporarily unavailable")
            self._respond(response, head)

        def do_GET(self) -> None:
            self._dispatch(head=False)

        def do_HEAD(self) -> None:
            self._dispatch(head=True)

        def _refuse(self) -> None:
            # The body is never read, so the connection must not be reused.
            self.close_connection = True
            self._respond(_error(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed"), head=False,
                          extra={"Allow": "GET, HEAD"})

        do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _refuse

    return Handler


class _BoundedServer(ThreadingHTTPServer):
    """A thread per connection, up to MAX_CONNECTIONS; past that, close at once."""

    daemon_threads = True

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def process_request(self, request, client_address):  # type: ignore[override]
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):  # type: ignore[override]
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class Servers:
    """Both listeners, each on its own thread."""

    def __init__(self, app: App, public: tuple[str, int], admin: tuple[str, int]):
        self.public = _BoundedServer(public, _handler(lambda: app.public_routes, serve_static=True))
        self.admin = _BoundedServer(admin, _handler(lambda: app.admin_routes, serve_static=False))
        self._threads = [threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
                         for s in (self.public, self.admin)]

    def start(self) -> "Servers":
        for thread in self._threads:
            thread.start()
        return self

    def stop(self) -> None:
        for server in (self.public, self.admin):
            server.shutdown()
            server.server_close()
