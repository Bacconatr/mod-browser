"""HTTP: the public site and the local-only admin API, on two listeners.

Public listener (behind Caddy, Internet-facing, no login):
  GET /             the page
  GET /static/...   its CSS and JS
  GET /api/status   server state, players, TPS, last restart, address
  GET /api/mods     the pack's mod list; ?q= &side= &kind= filter it
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

Only GET and HEAD are served today; anything else is 405.
"""

from __future__ import annotations

import html
import json
import mimetypes
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .pack import PackCatalog, search
from .status import StatusMonitor

STATIC = Path(__file__).parent / "static"
_MAX_QUERY = 200

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

    # ---- public ----
    def page(self, query: dict[str, list[str]]) -> Response:
        text = (STATIC / "index.html").read_text()
        for key, value in {"name": self.name, "address": self.monitor.address}.items():
            text = text.replace("{{" + key + "}}", html.escape(value))
        return 200, "text/html; charset=utf-8", text.encode(), {"Cache-Control": "no-cache"}

    def status(self, query: dict[str, list[str]]) -> Response:
        return _json(self.monitor.snapshot())

    def mods(self, query: dict[str, list[str]]) -> Response:
        def one(name: str) -> str:
            return (query.get(name) or [""])[0][:_MAX_QUERY]

        data = self.catalog.catalog()
        q, side, kind = one("q"), one("side"), one("kind")
        if not (q or side or kind):
            return _json(data)
        found = search(data["mods"], q, side, kind)
        return _json({**data, "mods": found, "matched": len(found)})

    def health(self, query: dict[str, list[str]]) -> Response:
        return _json({"ok": True})

    @property
    def public_routes(self) -> dict[str, Callable[[dict[str, list[str]]], Response]]:
        return {"/": self.page, "/api/status": self.status, "/api/mods": self.mods, "/api/health": self.health}

    # ---- admin (loopback only) ----
    @property
    def admin_routes(self) -> dict[str, Callable[[dict[str, list[str]]], Response]]:
        return {"/admin-api/v1/health": self.health}


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

        def log_message(self, fmt: str, *args: Any) -> None:  # Caddy logs requests
            pass

        def _respond(self, response: Response, head: bool) -> None:
            status, kind, body, headers = response
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            for key, value in {**SECURITY_HEADERS, **headers}.items():
                self.send_header(key, value)
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
                response = _error(HTTPStatus.SERVICE_UNAVAILABLE, "temporarily unavailable")
            self._respond(response, head)

        def do_GET(self) -> None:
            self._dispatch(head=False)

        def do_HEAD(self) -> None:
            self._dispatch(head=True)

        def _refuse(self) -> None:
            self._respond(_error(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed"), head=False)

        do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _refuse

    return Handler


class Servers:
    """Both listeners, each on its own thread."""

    def __init__(self, app: App, public: tuple[str, int], admin: tuple[str, int]):
        self.public = ThreadingHTTPServer(public, _handler(lambda: app.public_routes, serve_static=True))
        self.admin = ThreadingHTTPServer(admin, _handler(lambda: app.admin_routes, serve_static=False))
        for server in (self.public, self.admin):
            server.daemon_threads = True
        self._threads = [threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True) for s in (self.public, self.admin)]

    def start(self) -> "Servers":
        for thread in self._threads:
            thread.start()
        return self

    def stop(self) -> None:
        for server in (self.public, self.admin):
            server.shutdown()
            server.server_close()
