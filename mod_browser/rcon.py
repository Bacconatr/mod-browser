"""The real ServerPort: RCON over the game's compose network, with no Docker socket.

Container state comes from Docker's embedded DNS, not from the Docker API.
The app joins the game's compose network, where the game's service name
resolves only while its container is running: a stopped container drops off
the network and the name stops resolving. That answers "is the container
up?" without mounting /var/run/docker.sock, which would make this
Internet-facing app root on the guest. A name that resolves outside
`container_net` (a search-domain fallthrough to public DNS) counts as down.

Last start comes from the server's logs (mounted read-only). A start always
opens a fresh latest.log whose first line is the launcher's `[main/...]` line.
log4j also rolls latest.log at midnight, though, and the new file then starts
with an ordinary `[Server thread/...]` line, so when latest.log does not begin
with a start, the newest start among the rolled `YYYY-MM-DD-N.log.gz` files'
first lines is the answer, and none in reach means unknown, never midnight.

RCON commands are allowlisted. The app holds the RCON password, which can
run any console command, so the adapter itself refuses everything but the
read-only commands the page needs. A later ticket adds `say` for countdowns.
"""

from __future__ import annotations

import gzip
import ipaddress
import re
import socket
import struct
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .ports import ServerUnavailable

ALLOWED_COMMANDS = frozenset({"list", "neoforge tps"})
# In-game countdowns before a restart: `say` and plain words only, never a selector or a command-looking tail.
SAY = re.compile(r"say [A-Za-z0-9 .,:!()'-]{1,200}")

_LOGIN, _COMMAND = 3, 2
# The whole reply to one command, across all its packets. `list` with 10
# players and `neoforge tps` are a few hundred bytes; anything near this is a
# peer that never stops talking, and it would otherwise hold the status lock.
MAX_REPLY_BYTES = 64 * 1024
_COLOUR = re.compile(r"\x1b\[[0-9;]*m|§.")
# [12Nov2026 13:58:51.604] [main/INFO] ...
_LOG_STAMP = re.compile(r"^\[(\d{1,2}[A-Za-z]{3}\d{4} \d{2}:\d{2}:\d{2})[.\d]*\] \[([^/\]]+)/")
# Rolled game logs (not debug-N.log.gz). Only the newest few are read: a server up for longer than
# that many midnights shows "unknown" rather than costing a read of every old log.
_ROLLED = re.compile(r"^\d{4}-\d{2}-\d{2}-\d+\.log\.gz$")
MAX_ROLLED_LOGS = 14


class _Closed(ServerUnavailable):
    """The peer closed the connection."""


def strip_colour(text: str) -> str:
    return _COLOUR.sub("", text)


class RconClient:
    """Source RCON, as Minecraft speaks it. One connection per command."""

    def __init__(self, host: str, port: int, password: str, timeout: float = 3.0):
        self.host, self.port, self.password, self.timeout = host, port, password, timeout

    def run(self, command: str) -> str:
        """One command, answered within `timeout` seconds in all, or ServerUnavailable."""
        deadline = time.monotonic() + self.timeout
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
                self._send(sock, 1, _LOGIN, self.password)
                request_id, _ = self._read(sock, deadline)
                if request_id == -1:
                    raise ServerUnavailable("RCON refused the password")
                self._send(sock, 2, _COMMAND, command)
                parts = [self._read(sock, deadline)[1]]
                size = len(parts[0])
                # Minecraft splits replies over 4096 bytes into several
                # packets and sends no terminator, so read until it goes quiet,
                # but never past the deadline or MAX_REPLY_BYTES.
                while True:
                    try:
                        body = self._read(sock, min(deadline, time.monotonic() + 0.05))
                    except (TimeoutError, socket.timeout):
                        break
                    except _Closed:
                        break
                    size += len(body[1])
                    if size > MAX_REPLY_BYTES:
                        raise ServerUnavailable(f"RCON reply over {MAX_REPLY_BYTES} bytes")
                    parts.append(body[1])
                return "".join(parts)
        except ServerUnavailable:
            raise
        except (OSError, struct.error) as exc:
            raise ServerUnavailable(f"RCON {self.host}:{self.port}: {exc}") from exc

    @staticmethod
    def _send(sock: socket.socket, request_id: int, kind: int, body: str) -> None:
        payload = struct.pack("<ii", request_id, kind) + body.encode("utf-8") + b"\x00\x00"
        sock.sendall(struct.pack("<i", len(payload)) + payload)

    @staticmethod
    def _recv_exact(sock: socket.socket, size: int, deadline: float) -> bytes:
        data = b""
        while len(data) < size:
            left = deadline - time.monotonic()
            if left <= 0:
                if data:
                    raise ServerUnavailable("RCON reply too slow")
                raise socket.timeout("RCON quiet")
            sock.settimeout(left)
            chunk = sock.recv(size - len(data))
            if not chunk:
                raise _Closed("RCON connection closed")
            data += chunk
        return data

    @classmethod
    def _read(cls, sock: socket.socket, deadline: float) -> tuple[int, str]:
        (length,) = struct.unpack("<i", cls._recv_exact(sock, 4, deadline))
        if not 10 <= length <= 1 << 16:
            raise ServerUnavailable(f"RCON packet of {length} bytes")
        packet = cls._recv_exact(sock, length, deadline)
        request_id, _kind = struct.unpack("<ii", packet[:8])
        return request_id, packet[8:-2].decode("utf-8", "replace")


class RconServer:
    """ServerPort for the game container on the same compose network."""

    def __init__(self, host: str, port: int, password_file: Path, log_file: Path,
                 container_net: str = "172.16.0.0/12", log_tz: str = "America/New_York",
                 timeout: float = 3.0):
        self.host, self.port = host, port
        self.password_file, self.log_file = password_file, log_file
        self.container_net = ipaddress.ip_network(container_net)
        self.log_tz = ZoneInfo(log_tz)
        self.timeout = timeout

    def container_running(self) -> bool:
        try:
            infos = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
        except OSError:
            return False
        return any(ipaddress.ip_address(info[4][0]) in self.container_net for info in infos)

    def rcon(self, command: str) -> str:
        if command not in ALLOWED_COMMANDS and not SAY.fullmatch(command):
            raise ValueError(f"RCON command not allowed: {command!r}")
        try:
            password = self.password_file.read_text().strip()
        except OSError as exc:
            raise ServerUnavailable(f"cannot read the RCON password: {exc}") from exc
        reply = RconClient(self.host, self.port, password, self.timeout).run(command)
        return strip_colour(reply)

    def last_start(self) -> datetime | None:
        first = self._first_line(self.log_file, gzip_=False)
        if first is None:
            return None
        start = self._start_in(first)
        if start is not None or not _LOG_STAMP.match(first):
            return start
        # latest.log was rolled at midnight: the start is the first line of one of the rolled logs.
        try:
            rolled = [p for p in self.log_file.parent.iterdir() if _ROLLED.match(p.name)]
            rolled.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return None
        starts = [s for s in (self._start_in(self._first_line(p, gzip_=True) or "") for p in rolled[:MAX_ROLLED_LOGS]) if s]
        return max(starts, default=None)

    def _start_in(self, line: str) -> datetime | None:
        """The time on a server-start line (the launcher's `main` thread), else None."""
        match = _LOG_STAMP.match(line)
        if not match or match.group(2) != "main":
            return None
        try:
            stamp = datetime.strptime(match.group(1), "%d%b%Y %H:%M:%S")
        except ValueError:
            return None
        return stamp.replace(tzinfo=self.log_tz)

    @staticmethod
    def _first_line(path: Path, gzip_: bool) -> str | None:
        try:
            opener = gzip.open if gzip_ else open
            with opener(path, "rt", encoding="utf-8", errors="replace") as log:
                return log.readline(4096)
        except (OSError, EOFError):
            return None
