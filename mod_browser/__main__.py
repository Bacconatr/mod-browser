"""Run the site: `python -m mod_browser`, configured from the environment."""

from __future__ import annotations

import os
import signal
import threading
from pathlib import Path

from .pack import PackCatalog
from .rcon import RconServer
from .status import StatusMonitor
from .web import App, Servers


def env(name: str, default: str) -> str:
    return os.environ.get(f"MOD_BROWSER_{name}", default)


def main() -> None:
    server = RconServer(
        host=env("RCON_HOST", "mc"),
        port=int(env("RCON_PORT", "25575")),
        password_file=Path(env("RCON_PASSWORD_FILE", "/run/secrets/aeronautics/rcon")),
        log_file=Path(env("LOG_FILE", "/srv/logs/latest.log")),
        container_net=env("CONTAINER_NET", "172.16.0.0/12"),
        log_tz=env("LOG_TZ", "America/New_York"),
    )
    monitor = StatusMonitor(server, address=env("SERVER_ADDRESS", "play.example.com"),
                            max_age=float(env("STATUS_MAX_AGE", "10")))
    catalog = PackCatalog(Path(env("PACK_DIR", "/srv/pack")))
    app = App(monitor, catalog, name=env("SERVER_NAME", "Tel Aviv"))
    servers = Servers(
        app,
        public=(env("LISTEN_HOST", "0.0.0.0"), int(env("LISTEN_PORT", "8080"))),
        admin=(env("ADMIN_LISTEN_HOST", "127.0.0.1"), int(env("ADMIN_LISTEN_PORT", "8081"))),
    ).start()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    servers.stop()


if __name__ == "__main__":
    main()
