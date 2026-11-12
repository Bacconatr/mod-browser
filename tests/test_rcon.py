"""The real ServerPort adapter against a fake RCON server on a loopback socket."""

from __future__ import annotations

import socket
import struct
import tempfile
import threading
import unittest
from pathlib import Path

from mod_browser.pack import version_from_filename
from mod_browser.ports import ServerUnavailable
from mod_browser.rcon import RconClient, RconServer, strip_colour


class FakeRcon:
    """Minimal Minecraft RCON: login, then one reply per command, optionally split."""

    def __init__(self, password: str, replies: dict[str, list[str]]):
        self.password, self.replies = password, replies
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.seen: list[str] = []
        threading.Thread(target=self._serve, daemon=True).start()

    def close(self) -> None:
        self.sock.close()

    @staticmethod
    def _packet(request_id: int, kind: int, body: str) -> bytes:
        payload = struct.pack("<ii", request_id, kind) + body.encode() + b"\x00\x00"
        return struct.pack("<i", len(payload)) + payload

    @staticmethod
    def _read(conn: socket.socket) -> tuple[int, int, str]:
        (length,) = struct.unpack("<i", conn.recv(4, socket.MSG_WAITALL))
        data = conn.recv(length, socket.MSG_WAITALL)
        request_id, kind = struct.unpack("<ii", data[:8])
        return request_id, kind, data[8:-2].decode()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                request_id, _, password = self._read(conn)
                if password != self.password:
                    conn.sendall(self._packet(-1, 2, ""))
                    continue
                conn.sendall(self._packet(request_id, 2, ""))
                request_id, _, command = self._read(conn)
                self.seen.append(command)
                for part in self.replies.get(command, ["Unknown command"]):
                    conn.sendall(self._packet(request_id, 0, part))


class RconTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "rcon").write_text("hunter2\n")
        self.fake = FakeRcon("hunter2", {
            "list": ["§6There are 1 of a max of 10 players online: §rSteve"],
            "neoforge tps": ["Overworld: 20.000 TPS (1.0 ms/tick)\n", "Overall: 20.000 TPS (1.2 ms/tick)\n"],
        })
        self.addCleanup(self.fake.close)

    def server(self, **kwargs) -> RconServer:
        options = {"host": "127.0.0.1", "port": self.fake.port, "password_file": self.tmp / "rcon",
                   "log_file": self.tmp / "latest.log", "container_net": "127.0.0.0/8"}
        options.update(kwargs)
        return RconServer(**options)

    def test_command_reply_has_colour_codes_removed(self):
        self.assertEqual(self.server().rcon("list"), "There are 1 of a max of 10 players online: Steve")

    def test_split_reply_is_joined(self):
        self.assertIn("Overall: 20.000 TPS", self.server().rcon("neoforge tps"))

    def test_wrong_password_is_unavailable(self):
        with self.assertRaises(ServerUnavailable):
            RconClient("127.0.0.1", self.fake.port, "wrong").run("list")

    def test_closed_port_is_unavailable(self):
        free = socket.create_server(("127.0.0.1", 0))
        port = free.getsockname()[1]
        free.close()
        with self.assertRaises(ServerUnavailable):
            self.server(port=port).rcon("list")

    def test_missing_password_file_is_unavailable(self):
        with self.assertRaises(ServerUnavailable):
            self.server(password_file=self.tmp / "absent").rcon("list")

    def test_anything_but_read_only_commands_is_refused_before_connecting(self):
        for command in ("stop", "op Steve", "say hi", "list; stop"):
            with self.assertRaises(ValueError):
                self.server().rcon(command)
        self.assertEqual(self.fake.seen, [])

    def test_container_is_running_only_when_its_name_resolves_inside_the_network(self):
        self.assertTrue(self.server(host="localhost").container_running())
        self.assertFalse(self.server(host="localhost", container_net="172.16.0.0/12").container_running())
        self.assertFalse(self.server(host="no-such-container.invalid").container_running())

    def test_last_start_is_the_first_log_line(self):
        (self.tmp / "latest.log").write_text(
            "[12Nov2026 13:58:51.604] [main/INFO] [cpw.mods.modlauncher.Launcher/MODLAUNCHER]: ModLauncher running\n"
            "[12Nov2026 14:54:42.000] [Server thread/INFO]: later\n")
        self.assertEqual(self.server().last_start().isoformat(), "2026-11-12T13:58:51-05:00")

    def test_last_start_unknown_without_a_log(self):
        self.assertIsNone(self.server().last_start())
        (self.tmp / "latest.log").write_text("garbage\n")
        self.assertIsNone(self.server().last_start())

    def test_strip_colour(self):
        self.assertEqual(strip_colour("\x1b[0mOverall: §a20.000"), "Overall: 20.000")


class VersionTest(unittest.TestCase):
    def test_versions_from_real_jar_names(self):
        cases = {
            "create_connected-1.3.3-mc1.21.1.jar": "1.3.3",
            "DnDesires-1.21.1-2.3a-BETA.jar": "2.3a-BETA",
            "create_train_parts-0.5.2-1.21.1-6.0.10-281.jar": "0.5.2",
            "kaleidoscope_lib-1.1.1-neoforge-mc1.21.1.jar": "1.1.1",
            "create_submarine-3.4.5.jar": "3.4.5",
            "DistantHorizons-3.3.3-1.21.1-fabric-neoforge.jar": "3.3.3",
            "loconautics-0.1.1BETA.jar": "0.1.1BETA",
            "ComplementaryReimagined_r5.5.1.zip": "r5.5.1",
            "forgified-fabric-api-0.116.15+2.3.6+1.21.1.jar": "0.116.15+2.3.6+1.21.1",
        }
        for filename, version in cases.items():
            self.assertEqual(version_from_filename(filename), version, filename)


if __name__ == "__main__":
    unittest.main()
