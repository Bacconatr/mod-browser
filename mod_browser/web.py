"""HTTP: the public site and the local-only admin API, on two listeners.

Public listener (behind Caddy, Internet-facing, no login):
  GET /             the page
  GET /static/...   its CSS and JS
  GET /api/status   server state, players, TPS, last restart, address
  GET /api/mods     the pack's mod list; ?q= &side= &kind= filter it (the
                    page's search box uses these, so matching lives here only)
  GET /api/health   liveness

  GET /add          the "find a mod" page (search, version picker, verdict)
  GET /api/catalog/search?q=            Modrinth (and CurseForge when enabled), or a pasted CurseForge id
  GET /api/catalog/versions?source=&id= the versions for NeoForge 1.21.1 and the default
  GET /api/catalog/resolve?source=&id=&version=   what adding it would do

  GET /queue        what friends asked for, by whom, and where each stands
  GET /api/requests the same as JSON; POST /api/requests {name, source, id, version?} asks
  GET /admin        the owner's page; every /admin route needs the vault password (401 without it)
  /admin/api/requests[...]   list, add, approve, deny, override, remove, requeue (JSON)

Admin listener (bound to loopback inside the container; nothing proxies it):
  GET  /admin-api/v1/health
  GET  /admin-api/v1/requests?since=<seq>     every request changed since a poll
  POST /admin-api/v1/requests                 add as the owner
  POST /admin-api/v1/requests/<id>/<action>   approve | deny | override | remove | requeue
  POST /admin-api/v1/restart-now              apply what is queued without waiting (still test-boots and backs up)
  PVE reaches it with `pct exec` + `docker exec`, so it needs no password and
  must never be published or proxied.

Writes are POST with a JSON body of at most 8 KiB, `Content-Type:
application/json` (which a cross-site form cannot send without a preflight we
never answer), and, when the browser names an Origin, the same origin as the
Host. A body no route reads closes its connection after the answer, so it can
never be parsed as the next request on a connection Caddy reuses.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import html
import ipaddress
import json
import mimetypes
import re
import sys
import threading
import traceback
from dataclasses import dataclass, field
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .catalog import SOURCES, camel, default_version
from .intake import Intake, Refused
from .limits import KeyedLimiter
from .pack import PackCatalog, search
from .ports import CatalogPort, InstalledPort, JarPort
from .queue import InvalidTransition, NoSuchRequest, Queue
from .resolver import Busy, NotFound, Verdict, resolve
from .status import StatusMonitor

STATIC = Path(__file__).parent / "static"
_MAX_QUERY = 200
_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
# Resolving can download jars from a CDN, so only a few may run at once.
MAX_RESOLVES = 3
MIN_ADMIN_PASSWORD = 20
# Connections served at once per listener. Caddy keeps a small pool of
# upstream connections; anything past this is closed at once rather than
# given a thread, so a burst cannot exhaust the container's pids limit.
MAX_CONNECTIONS = 48
# Seconds a connection may sit idle or half-sent before it is dropped.
SOCKET_TIMEOUT = 15

# A request body is a small JSON object: a name, a mod id, a version id, a reason.
MAX_BODY = 8 * 1024
_ROUTE_ID = r"(\d{1,9})"
ADMIN_ACTIONS = ("approve", "deny", "override", "remove", "requeue")

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


@dataclass
class Browse:
    """The three ports the catalog routes read through, and the cap on concurrent resolves."""

    catalog: CatalogPort
    jars: JarPort
    installed: InstalledPort
    _gate: threading.BoundedSemaphore = field(default_factory=lambda: threading.BoundedSemaphore(MAX_RESOLVES))

    def resolve(self, source: str, project_id: str, version_id: str | None) -> Verdict:
        """Raises Busy when MAX_RESOLVES are already running."""
        if not self._gate.acquire(blocking=False):
            raise Busy()
        try:
            return resolve(self.catalog, self.jars, self.installed, source, project_id, version_id)
        finally:
            self._gate.release()


@dataclass
class Requests:
    """The request queue and the rules that move it."""

    queue: Queue
    intake: Intake


_TRUSTED: list[Any] = [ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128")]


def set_trusted_proxies(networks: list[str]) -> None:
    """Replace the addresses whose X-Forwarded-For is believed (default: loopback only)."""
    _TRUSTED[:] = [ipaddress.ip_network(n.strip(), strict=False) for n in networks if n.strip()]


def is_trusted(peer: str) -> bool:
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(address in net for net in _TRUSTED if net.version == address.version)


_WARNED: set[str] = set()


def _warn_untrusted_once(peer: str) -> None:
    if peer not in _WARNED and len(_WARNED) < 64:
        _WARNED.add(peer)
        sys.stderr.write(f"mod-browser: X-Forwarded-For from {peer} ignored; add it to MOD_BROWSER_TRUSTED_PROXIES if it is the proxy\n")


def client_key(address: str) -> str:
    try:
        ip = ipaddress.ip_address(address[:64])
    except ValueError:
        return "unknown"
    return str(ipaddress.ip_network(f"{ip}/64", strict=False)) if ip.version == 6 else str(ip)


@dataclass
class Req:
    """One request to a dynamic route."""

    method: str
    path: str
    query: dict[str, list[str]]
    headers: Any
    body: bytes
    match: re.Match[str]
    peer: str = ""

    def json(self) -> dict[str, Any]:
        if not self.body:
            return {}
        try:
            data = json.loads(self.body)
        except ValueError as exc:
            raise Refused(400, "The body is not JSON.") from exc
        if not isinstance(data, dict):
            raise Refused(400, "The body must be a JSON object.")
        return data

    @property
    def client(self) -> str:
        """Who is asking, for rate limits. Caddy runs beside this app and sets X-Forwarded-For; its
        last entry is believed only when the connection itself comes from a trusted proxy address
        (loopback unless MOD_BROWSER_TRUSTED_PROXIES says otherwise), so no one else can choose
        their own identity. IPv6 clients are grouped by /64, so rotating the low bits buys nothing."""
        forwarded = (self.headers.get("X-Forwarded-For") or "").split(",")[-1].strip()
        if forwarded and not is_trusted(self.peer):
            # Either a caller choosing its own identity (ignored), or the proxy is not where we think:
            # then every friend shares the proxy's address and one limit. Say so, once per peer.
            _warn_untrusted_once(self.peer)
            return client_key(self.peer)
        return client_key(forwarded) if forwarded else client_key(self.peer)


DynamicRoute = tuple[str, "re.Pattern[str]", Callable[[Req], "Response"]]


def _json(body: Any, status: int = 200) -> Response:
    return status, "application/json", json.dumps(body, separators=(",", ":")).encode(), {"Cache-Control": "no-store"}


def _error(status: HTTPStatus, message: str) -> Response:
    return _json({"error": message}, status.value)


class App:
    """The routes, with the ports wired in. Tests build one with fakes."""

    def __init__(self, monitor: StatusMonitor, catalog: PackCatalog, name: str = "Tel Aviv",
                 browse: "Browse | None" = None, requests: "Requests | None" = None,
                 admin_password: str | None = None, applier: Any | None = None):
        self.applier = applier
        self.monitor, self.catalog, self.name, self.browse = monitor, catalog, name, browse
        self.requests = requests if browse is not None else None
        # An unset or short password leaves /admin shut (404), never open.
        self._admin_password = admin_password if admin_password and len(admin_password) >= MIN_ADMIN_PASSWORD else None
        self._bad_logins = KeyedLimiter(10, 60.0, ceiling=300)
        pages = ("index.html", "add.html", "queue.html", "admin.html")
        self._page, self._add_page, self._queue_page, self._admin_page = (
            self._render(f, name, monitor.address, browse is not None, self.requests is not None) for f in pages)
        self._mods_json: tuple[str, bytes] | None = None

    @staticmethod
    def _render(filename: str, name: str, address: str, browsing: bool, queueing: bool = False) -> bytes:
        text = (STATIC / filename).read_text()
        links = ""
        if browsing:
            links += '<p><a href="/add">Looking for a mod that is not in the pack? Find one.</a>'
            links += ' <a href="/queue">See what friends have asked for.</a></p>' if queueing else "</p>"
        text = text.replace("{{addlink}}", links)
        text = text.replace("{{requesting}}", "true" if queueing else "false")
        for key, value in {"name": name, "address": address}.items():
            text = text.replace("{{" + key + "}}", html.escape(value))
        return text.encode()

    # ---- public ----
    def page(self, query: dict[str, list[str]]) -> Response:
        return 200, "text/html; charset=utf-8", self._page, {"Cache-Control": "no-cache"}

    def status(self, query: dict[str, list[str]]) -> Response:
        snapshot = self.monitor.snapshot()
        if self.applier is not None and self.applier.enabled:
            snapshot = {**snapshot, "apply": self.applier.status()}
        return _json(snapshot)

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

    # ---- catalog: read-only browsing; requests come with the queue ----
    def add_page(self, query: dict[str, list[str]]) -> Response:
        return 200, "text/html; charset=utf-8", self._add_page, {"Cache-Control": "no-cache"}

    def _failed(self, exc: Exception) -> Response:
        if isinstance(exc, NotFound):
            return _error(HTTPStatus.NOT_FOUND, str(exc))
        sys.stderr.write("mod-browser: catalog request failed\n" + "".join(traceback.format_exception(exc)))
        return _error(HTTPStatus.BAD_GATEWAY, "Modrinth or CurseForge did not answer; try again shortly.")

    def catalog_search(self, query: dict[str, list[str]]) -> Response:
        q = ((query.get("q") or [""])[0]).strip()[:100]
        if len(q) < 2:
            return _error(HTTPStatus.BAD_REQUEST, "search needs at least two characters")
        projects, warnings = self.browse.catalog.search(q)
        return _json({"query": q, "results": camel(projects), "warnings": warnings})

    def _source_and_id(self, query: dict[str, list[str]]) -> tuple[str, str] | None:
        source, ident = ((query.get(k) or [""])[0] for k in ("source", "id"))
        return (source, ident) if source in SOURCES and _ID.match(ident) else None

    def catalog_versions(self, query: dict[str, list[str]]) -> Response:
        want = self._source_and_id(query)
        if want is None:
            return _error(HTTPStatus.BAD_REQUEST, "source and id are required")
        try:
            versions = self.browse.catalog.versions(*want)
        except Exception as exc:  # noqa: BLE001
            return self._failed(exc)
        pick = default_version(versions)
        return _json({
            "default": {"id": pick[0].id, "fallback": pick[1]} if pick else None,
            "versions": [{"id": v.id, "versionNumber": v.version_number, "channel": v.channel,
                          "publishedAt": v.published_at, "fileName": v.file_name, "blocked": v.blocked}
                         for v in versions],
        })

    def catalog_resolve(self, query: dict[str, list[str]]) -> Response:
        want = self._source_and_id(query)
        if want is None:
            return _error(HTTPStatus.BAD_REQUEST, "source and id are required")
        version = (query.get("version") or [None])[0]
        if version is not None and not _ID.match(version):
            return _error(HTTPStatus.BAD_REQUEST, "bad version")
        try:
            return _json(camel(self.browse.resolve(*want, version)))
        except Busy:
            return _error(HTTPStatus.TOO_MANY_REQUESTS, "busy, try again in a moment")
        except Exception as exc:  # noqa: BLE001
            return self._failed(exc)

    # ---- requests: friends ----
    def _page_of(self, body: bytes) -> Callable[[Req], Response]:
        return lambda req: (200, "text/html; charset=utf-8", body, {"Cache-Control": "no-cache"})

    def _same_origin(self, req: Req) -> None:
        """A write must be JSON from this site: a form on another site can send neither."""
        if not (req.headers.get("Content-Type") or "").lower().startswith("application/json"):
            raise Refused(415, "Send JSON.")
        origin, host = req.headers.get("Origin"), req.headers.get("Host")
        if origin and host and urlsplit(origin).netloc != host:
            raise Refused(403, "Cross-site writes are refused.")

    def _answering(self, fn: Callable[[Req], Response]) -> Callable[[Req], Response]:
        """Turns the domain's refusals into answers, and an unexpected failure into a 503."""
        def run(req: Req) -> Response:
            try:
                return fn(req)
            except Refused as r:
                return _json({"error": r.message, **r.details}, r.status)
            except InvalidTransition as exc:
                return _error(HTTPStatus.CONFLICT, str(exc))
            except NoSuchRequest:
                return _error(HTTPStatus.NOT_FOUND, "no such request")
            except Busy:
                return _error(HTTPStatus.TOO_MANY_REQUESTS, "busy, try again in a moment")
            except NotFound as exc:
                return _error(HTTPStatus.NOT_FOUND, str(exc))
        return run

    def list_public(self, req: Req) -> Response:
        return _json({"requests": [r.public() for r in reversed(self.requests.queue.all())]})

    def submit(self, req: Req) -> Response:
        self._same_origin(req)
        body = req.json()
        source, ident, version = (str(body.get(k) or "") for k in ("source", "id", "version"))
        if source not in SOURCES or not _ID.match(ident) or (version and not _ID.match(version)):
            raise Refused(400, "source and id are required")
        made = self.requests.intake.submit(str(body.get("name") or ""), source, ident, version or None, req.client)
        return _json({"request": made.public()}, 201)

    # ---- requests: the owner (same operations on /admin/api, behind the password, and on the loopback API) ----
    def _owner_list(self, req: Req) -> Response:
        raw = (req.query.get("since") or ["0"])[0]
        since = int(raw) if raw.isdigit() else 0
        found = self.requests.queue.all(since)
        return _json({"requests": [r.admin() for r in found], "seq": max((r.seq for r in found), default=since)})

    def _owner_add(self, req: Req) -> Response:
        self._same_origin(req)
        body = req.json()
        source, ident, version = (str(body.get(k) or "") for k in ("source", "id", "version"))
        if source not in SOURCES or not _ID.match(ident) or (version and not _ID.match(version)):
            raise Refused(400, "source and id are required")
        return _json({"request": self.requests.intake.add_directly(source, ident, version or None).admin()}, 201)

    def _owner_act(self, req: Req) -> Response:
        self._same_origin(req)
        request_id, action = int(req.match.group(1)), req.match.group(2)
        intake, body = self.requests.intake, req.json()
        if action == "approve":
            done = intake.approve(request_id)
        elif action == "deny":
            done = intake.deny(request_id, str(body.get("reason") or ""))
        elif action == "override":
            version = str(body.get("version") or "")
            if not _ID.match(version):
                raise Refused(400, "version is required")
            done = intake.override(request_id, version)
        elif action == "remove":
            done = intake.remove(request_id)
        else:
            done = intake.requeue(request_id)
        return _json({"request": done.admin()})

    def _restart_now(self, req: Req) -> Response:
        self._same_origin(req)
        if self.applier is None or not self.applier.enabled:
            raise Refused(409, "The applier is switched off.")
        if not self.requests.queue.queued():
            raise Refused(409, "Nothing is queued.")
        return _json({"apply": self.applier.request_restart_now()})

    def _password_ok(self, req: Req) -> bool:
        header = req.headers.get("Authorization") or ""
        if not header.startswith("Basic "):
            return False
        try:
            _, _, given = base64.b64decode(header[6:], validate=True).decode("utf-8").partition(":")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return False
        return hmac.compare_digest(given.encode(), self._admin_password.encode())

    def _behind_password(self, fn: Callable[[Req], Response]) -> Callable[[Req], Response]:
        def guarded(req: Req) -> Response:
            if self._admin_password is None:
                return _error(HTTPStatus.NOT_FOUND, "not found")
            if self._password_ok(req):
                return fn(req)  # the right password always gets in, and is never counted
            if self._bad_logins.full(req.client):
                return _error(HTTPStatus.TOO_MANY_REQUESTS, "too many wrong passwords; wait a minute")
            self._bad_logins.record(req.client)
            status, kind, body, headers = _error(HTTPStatus.UNAUTHORIZED, "password required")
            return status, kind, body, {**headers, "WWW-Authenticate": 'Basic realm="Tel Aviv admin", charset="UTF-8"'}
            return fn(req)
        return guarded

    @property
    def public_dynamic(self) -> list[DynamicRoute]:
        if self.requests is None:
            return []
        answer = self._answering
        root = re.compile(r"^/admin/api/requests$")
        act = re.compile(rf"^/admin/api/requests/{_ROUTE_ID}/({'|'.join(ADMIN_ACTIONS)})$")
        guard = self._behind_password
        return [
            ("GET", re.compile(r"^/queue$"), self._page_of(self._queue_page)),
            ("GET", re.compile(r"^/api/requests$"), answer(self.list_public)),
            ("POST", re.compile(r"^/api/requests$"), answer(self.submit)),
            ("GET", re.compile(r"^/admin$"), guard(self._page_of(self._admin_page))),
            ("GET", root, guard(answer(self._owner_list))),
            ("POST", root, guard(answer(self._owner_add))),
            ("POST", act, guard(answer(self._owner_act))),
            ("POST", re.compile(r"^/admin/api/restart-now$"), guard(answer(self._restart_now))),
        ]

    @property
    def admin_dynamic(self) -> list[DynamicRoute]:
        if self.requests is None:
            return []
        answer = self._answering
        return [
            ("GET", re.compile(r"^/admin-api/v1/requests$"), answer(self._owner_list)),
            ("POST", re.compile(r"^/admin-api/v1/requests$"), answer(self._owner_add)),
            ("POST", re.compile(rf"^/admin-api/v1/requests/{_ROUTE_ID}/({'|'.join(ADMIN_ACTIONS)})$"), answer(self._owner_act)),
            ("POST", re.compile(r"^/admin-api/v1/restart-now$"), answer(self._restart_now)),
        ]

    @property
    def public_routes(self) -> dict[str, Callable[[dict[str, list[str]]], Response]]:
        routes = {"/": self.page, "/api/status": self.status, "/api/mods": self.mods, "/api/health": self.health}
        if self.browse:
            routes.update({"/add": self.add_page, "/api/catalog/search": self._guarded(self.catalog_search),
                           "/api/catalog/versions": self.catalog_versions, "/api/catalog/resolve": self.catalog_resolve})
        return routes

    def _guarded(self, route: Callable[[dict[str, list[str]]], Response]) -> Callable[[dict[str, list[str]]], Response]:
        def wrapped(query: dict[str, list[str]]) -> Response:
            try:
                return route(query)
            except Exception as exc:  # noqa: BLE001
                return self._failed(exc)
        return wrapped

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


def _handler(routes: Callable[[], dict[str, Callable[..., Response]]], dynamic: Callable[[], list[DynamicRoute]],
             serve_static: bool):
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

        def _body(self) -> bytes | None:
            """The request body, or None after answering 411/413 (the connection is then closed)."""
            length = self.headers.get("Content-Length", "")
            if "Transfer-Encoding" in self.headers or not length.isdigit():
                if length == "" and "Transfer-Encoding" not in self.headers:
                    return b""
                self.close_connection = True
                self._respond(_error(HTTPStatus.LENGTH_REQUIRED, "send a Content-Length"), head=False)
                return None
            if int(length) > MAX_BODY:
                self.close_connection = True
                self._respond(_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body too large"), head=False)
                return None
            return self.rfile.read(int(length))

        def _dispatch(self, head: bool, method: str = "GET") -> None:
            url = urlsplit(self.path)
            for verb, pattern, fn in dynamic():
                match = pattern.match(url.path)
                if verb == method and match:
                    body = self._body()
                    if body is None:
                        return
                    try:
                        response = fn(Req(method, url.path, parse_qs(url.query), self.headers, body, match, self.client_address[0]))
                    except Exception:  # noqa: BLE001
                        sys.stderr.write(f"mod-browser: {url.path} failed\n{traceback.format_exc()}")
                        response = _error(HTTPStatus.SERVICE_UNAVAILABLE, "temporarily unavailable")
                    self._respond(response, head)
                    return
            if method not in ("GET", "HEAD"):
                self._refuse()
                return
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

        def do_POST(self) -> None:
            self._dispatch(head=False, method="POST")

        def _refuse(self) -> None:
            # The body is never read, so the connection must not be reused.
            self.close_connection = True
            self._respond(_error(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed"), head=False,
                          extra={"Allow": "GET, HEAD"})

        def do_PUT(self) -> None:
            self._refuse()

        do_PATCH = do_DELETE = do_OPTIONS = do_PUT

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
        self.public = _BoundedServer(public, _handler(lambda: app.public_routes, lambda: app.public_dynamic, serve_static=True))
        self.admin = _BoundedServer(admin, _handler(lambda: app.admin_routes, lambda: app.admin_dynamic, serve_static=False))
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
