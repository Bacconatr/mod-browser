"""Run the site: `python -m mod_browser`, configured from the environment."""

from __future__ import annotations

import logging
import os
import signal
import threading
from pathlib import Path

from .curseforge import CurseForgeCatalog
from .modrinth import ModrinthCatalog
from .net import CachingHttp, FetchHttp
from .pack import PackCatalog
from .rcon import RconServer
from .status import StatusMonitor
from .sources import CompositeCatalog, DirInstalled, HttpJars
from .web import App, Browse, Servers


def env(name: str, default: str) -> str:
    return os.environ.get(f"MOD_BROWSER_{name}", default)


def browse_from_env() -> Browse | None:
    """Catalog browsing needs the server's mods directory; without it, /add is off (404)."""
    mods_dir = Path(env("MODS_DIR", "/srv/mods"))
    if not mods_dir.is_dir():
        return None
    http = CachingHttp(FetchHttp())
    key_file = Path(env("CURSEFORGE_API_KEY_FILE", "/run/secrets/aeronautics/curseforge"))
    key = key_file.read_text().strip() if key_file.is_file() else ""
    catalog = CompositeCatalog(
        ModrinthCatalog(http),
        CurseForgeCatalog(http, key) if key else None,
        curseforge_search=env("CURSEFORGE_SEARCH", "0") == "1",
    )
    return Browse(catalog=catalog, jars=HttpJars(http), installed=DirInstalled(mods_dir))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="mod-browser: %(levelname)s %(message)s")
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
    app = App(monitor, catalog, name=env("SERVER_NAME", "Tel Aviv"), browse=browse_from_env())
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
