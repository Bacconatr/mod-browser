"""The only network the app needs: JSON from the two catalogs, jars from their CDNs.

The URLs fetched here come out of API responses, so a poisoned response must
not turn the app into a request forger for the guest's own network:

- only the allow-listed hosts, over https;
- redirects are followed by hand and re-checked against the allow-list at
  every hop, at most `MAX_REDIRECTS` of them;
- a body is streamed and abandoned the moment it passes the cap (a missing
  Content-Length proves nothing), and the whole read has one deadline, so a
  server that drips bytes cannot hold a thread.
"""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import time
from collections import OrderedDict
from typing import IO, Any, Callable
from urllib.parse import urljoin, urlsplit

ALLOWED_HOSTS = (
    "api.modrinth.com",
    "cdn.modrinth.com",
    "api.curseforge.com",
    "edge.forgecdn.net",
    "mediafilez.forgecdn.net",
)
MAX_JSON = 4 * 1024 * 1024
MAX_JAR = 100 * 1024 * 1024
MAX_REDIRECTS = 3
USER_AGENT = "mod-browser/0.2"


class FetchError(Exception):
    """A fetch was refused or failed; the message is safe to log, never to show verbatim."""


class FetchHttp:
    def __init__(self, hosts: tuple[str, ...] = ALLOWED_HOSTS, schemes: tuple[str, ...] = ("https",),
                 timeout: float = 15.0, jar_timeout: float = 60.0,
                 max_json: int = MAX_JSON, max_jar: int = MAX_JAR):
        self.hosts, self.schemes = hosts, schemes
        self.timeout, self.jar_timeout = timeout, jar_timeout
        self.max_json, self.max_jar = max_json, max_jar

    # ---- public ----
    def json(self, url: str, headers: dict[str, str] | None = None) -> Any:
        body = self._get(url, headers or {}, self.max_json, self.timeout)
        try:
            return json.loads(body.read().decode("utf-8"))
        except ValueError as exc:
            raise FetchError("the answer was not JSON") from exc
        finally:
            body.close()

    def post_json(self, url: str, body: Any, headers: dict[str, str] | None = None) -> Any:
        """POST a JSON body to an allow-listed host (no redirects are followed for a POST) and parse the answer."""
        payload = json.dumps(body).encode()
        scheme, host, port, path = self._check(url)
        conn = self._connect(scheme, host, port, self.timeout)
        try:
            conn.request("POST", path, body=payload, headers={
                "User-Agent": USER_AGENT, "Accept-Encoding": "identity", "Content-Type": "application/json",
                "Accept": "application/json", **(headers or {})})
            response = conn.getresponse()
            if response.status != 200:
                raise FetchError(f"{host} answered {response.status}")
            data = self._read(response, self.max_json, time.monotonic() + self.timeout)
            try:
                return json.loads(data.read().decode("utf-8"))
            except ValueError as exc:
                raise FetchError("the answer was not JSON") from exc
            finally:
                data.close()
        except (OSError, http.client.HTTPException) as exc:
            raise FetchError(f"{host}: {exc}") from exc
        finally:
            conn.close()

    def fetch_jar(self, url: str) -> IO[bytes]:
        """The body as a seekable file (in memory up to 1 MiB, on disk past that). Caller closes it."""
        return self._get(url, {}, self.max_jar, self.jar_timeout)

    # ---- internals ----
    def _check(self, url: str) -> tuple[str, str, int | None, str]:
        parts = urlsplit(url)
        if parts.scheme not in self.schemes or parts.hostname not in self.hosts:
            raise FetchError(f"refusing to fetch {parts.hostname}")
        path = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
        return parts.scheme, parts.hostname, parts.port, path

    def _connect(self, scheme: str, host: str, port: int | None, timeout: float) -> http.client.HTTPConnection:
        cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        return cls(host, port, timeout=timeout)

    def _get(self, url: str, headers: dict[str, str], cap: int, timeout: float) -> IO[bytes]:
        deadline = time.monotonic() + timeout
        for hop in range(MAX_REDIRECTS + 1):
            scheme, host, port, path = self._check(url)
            conn = self._connect(scheme, host, port, min(timeout, 15.0))
            try:
                conn.request("GET", path, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity", **headers})
                response = conn.getresponse()
                location = response.getheader("Location")
                if 300 <= response.status < 400 and location:
                    if hop >= MAX_REDIRECTS:
                        raise FetchError("too many redirects")
                    next_url = urljoin(url, location)
                    if urlsplit(next_url).netloc != urlsplit(url).netloc:
                        headers = {}  # never forward a catalog API key to a CDN or another origin
                    url = next_url
                    continue
                if response.status != 200:
                    raise FetchError(f"{host} answered {response.status}")
                return self._read(response, cap, deadline)
            except (OSError, http.client.HTTPException) as exc:
                raise FetchError(f"{host}: {exc}") from exc
            finally:
                conn.close()
        raise FetchError("too many redirects")

    def _read(self, response: http.client.HTTPResponse, cap: int, deadline: float) -> IO[bytes]:
        declared = response.getheader("Content-Length")
        if declared and declared.isdigit() and int(declared) > cap:
            raise FetchError("body is too large")
        # 1 MiB in memory, then the file is on disk (/tmp, which Homelab mounts
        # from the guest disk): a jar is read from it once, so RAM buys nothing.
        out = tempfile.SpooledTemporaryFile(max_size=1024 * 1024)
        total = 0
        try:
            while True:
                chunk = response.read1(64 * 1024)  # read() would wait for all 64 KiB, hiding a slow drip from the deadline
                if not chunk:
                    break
                total += len(chunk)
                if total > cap:
                    raise FetchError("body is too large")
                if time.monotonic() > deadline:
                    raise FetchError("body took too long")
                out.write(chunk)
        except BaseException:
            out.close()
            raise
        out.seek(0)
        return out


class CachingHttp:
    """Remembers JSON answers briefly, so a busy page cannot hammer the catalogs.

    Jars are not cached here (their parsed metadata is, in `HttpJars`).
    """

    def __init__(self, inner: Any, ttl: float = 300.0, size: int = 256,
                 clock: Callable[[], float] = time.monotonic):
        self.inner, self.ttl, self.size, self._clock = inner, ttl, size, clock
        self._lock = threading.Lock()
        self._seen: OrderedDict[tuple[str, tuple[tuple[str, str], ...]], tuple[float, Any]] = OrderedDict()

    def json(self, url: str, headers: dict[str, str] | None = None) -> Any:
        key = (url, tuple(sorted((headers or {}).items())))
        with self._lock:
            hit = self._seen.get(key)
            if hit and self._clock() - hit[0] < self.ttl:
                return hit[1]
        value = self.inner.json(url, headers)
        with self._lock:
            self._seen[key] = (self._clock(), value)
            self._seen.move_to_end(key)
            while len(self._seen) > self.size:
                self._seen.popitem(last=False)
        return value

    def fetch_jar(self, url: str) -> IO[bytes]:
        return self.inner.fetch_jar(url)

    def post_json(self, url: str, body: Any, headers: dict[str, str] | None = None) -> Any:
        return self.inner.post_json(url, body, headers)  # a POST is never remembered
