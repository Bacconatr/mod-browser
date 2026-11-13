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
from .intake import Intake
from .pack import PackCatalog
from .prism import PackFiles
from .queue import Queue
from .whitelist import FileWhitelist
from .rcon import RconServer
from .status import StatusMonitor
from .sources import CompositeCatalog, DirInstalled, HttpJars
from .web import App, Browse, Requests, Servers, Setup, set_trusted_proxies


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


def requests_from_env(browse: Browse | None) -> Requests | None:
    """Requests need browsing, a writable data directory and the server's whitelist; else they are off."""
    data_dir = Path(env("DATA_DIR", "/srv/data"))
    whitelist = Path(env("WHITELIST_FILE", "/srv/whitelist.json"))
    if browse is None or not data_dir.is_dir() or not whitelist.is_file():
        return None
    queue = Queue(data_dir / "requests.db")
    return Requests(queue=queue, intake=Intake(queue, browse.resolve, FileWhitelist(whitelist)))


def setup_from_env(pack_dir: Path) -> Setup:
    public = env("PUBLIC_URL", "https://mods.example.com").rstrip("/")
    jar = env("BOOTSTRAP_JAR", "/srv/prism/packwiz-installer-bootstrap.jar")
    return Setup(files=PackFiles(pack_dir), pack_url=f"{public}/pack/pack.toml", bootstrap_jar=Path(jar),
                 min_mb=int(env("PRISM_MIN_MB", "2048")), max_mb=int(env("PRISM_MAX_MB", "6144")))


def admin_password_from_env() -> str | None:
    path = Path(env("ADMIN_PASSWORD_FILE", "/run/secrets/aeronautics/admin"))
    return path.read_text().strip() if path.is_file() else None


def main() -> None:
    # Caddy runs inside the container and reaches this app over loopback.
    set_trusted_proxies(env("TRUSTED_PROXIES", "127.0.0.1/32,::1/128").split(","))
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
    pack_dir = Path(env("PACK_DIR", "/srv/pack"))
    catalog = PackCatalog(pack_dir)
    browse = browse_from_env()
    app = App(monitor, catalog, name=env("SERVER_NAME", "Tel Aviv"), browse=browse,
              requests=requests_from_env(browse), admin_password=admin_password_from_env(),
              setup=setup_from_env(pack_dir))
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
