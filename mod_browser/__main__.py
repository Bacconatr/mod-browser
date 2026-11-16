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
from .applier import ApplyDesk
from .intake import Intake
from .pack import PackCatalog
from .upstream import CurseForgeUpstream, UpstreamChecker
from .prism import PackFiles
from .queue import Queue
from .whitelist import FileWhitelist
from .rcon import RconServer
from .status import BootTimes, StatusMonitor
from .sources import CompositeCatalog, DirInstalled, HttpJars
from .web import App, Browse, Requests, Servers, Setup, set_trusted_proxies


def env(name: str, default: str) -> str:
    return os.environ.get(f"MOD_BROWSER_{name}", default)


def curseforge_key_from_env() -> str:
    key_file = Path(env("CURSEFORGE_API_KEY_FILE", "/run/secrets/aeronautics/curseforge"))
    return key_file.read_text().strip() if key_file.is_file() else ""


def browse_from_env(http: CachingHttp) -> Browse | None:
    """Catalog browsing needs the server's mods directory; without it, /add is off (404)."""
    mods_dir = Path(env("MODS_DIR", "/srv/mods"))
    if not mods_dir.is_dir():
        return None
    catalog = CompositeCatalog(
        ModrinthCatalog(http),
        curseforge_from_env(http),
        curseforge_search=env("CURSEFORGE_SEARCH", "0") == "1",
    )
    return Browse(catalog=catalog, jars=HttpJars(http), installed=DirInstalled(mods_dir))


def data_dir_from_env() -> Path | None:
    """The writable data directory (requests.db, boot-times.json), when it exists."""
    path = Path(env("DATA_DIR", "/srv/data"))
    return path if path.is_dir() else None


def requests_from_env(browse: Browse | None, catalog: PackCatalog) -> Requests | None:
    """Requests need browsing, a writable data directory and the server's whitelist; else they are off."""
    data_dir = data_dir_from_env()
    whitelist = Path(env("WHITELIST_FILE", "/srv/whitelist.json"))
    if browse is None or data_dir is None or not whitelist.is_file():
        return None
    queue = Queue(data_dir / "requests.db")
    return Requests(queue=queue, intake=Intake(queue, browse.resolve, FileWhitelist(whitelist),
                                                    pack_head=lambda: catalog.catalog()["pack"]["commit"]))


def upstream_from_env(requests: Requests | None, catalog: PackCatalog) -> UpstreamChecker | None:
    """The daily check of the base modpack. Needs requests (it files one) and the CurseForge key."""
    if requests is None:
        return None
    key = curseforge_key_from_env()
    source = CurseForgeUpstream(CachingHttp(FetchHttp(timeout=30.0, jar_timeout=120.0)), key) if key else None
    return UpstreamChecker(requests.queue, source, catalog, tz=env("LOG_TZ", "America/New_York"))


def curseforge_from_env(http: CachingHttp) -> CurseForgeCatalog | None:
    key = curseforge_key_from_env()
    return CurseForgeCatalog(http, key) if key else None


def setup_from_env(pack_dir: Path, http: CachingHttp) -> Setup:
    public = env("PUBLIC_URL", "https://mods.example.com").rstrip("/")
    jar = env("BOOTSTRAP_JAR", "/srv/prism/packwiz-installer-bootstrap.jar")
    curseforge = curseforge_from_env(http)
    return Setup(files=PackFiles(pack_dir), pack_url=f"{public}/pack/pack.toml", bootstrap_jar=Path(jar),
                 min_mb=int(env("PRISM_MIN_MB", "2048")), max_mb=int(env("PRISM_MAX_MB", "6144")),
                 blocked_files=curseforge.blocked_files if curseforge else None,
                 export_http=http, export_match=curseforge.match_fingerprint if curseforge else None)


def applier_from_env(requests: Requests | None) -> ApplyDesk | None:
    """OFF unless MOD_BROWSER_APPLIER=1. On, the app only records the owner's "apply" and hands batches
    to the maintenance runner, which PVE runs on the guest (`python3 -m mod_browser.maintenance`); this
    container never stops, starts or backs up anything (no Docker socket)."""
    if env("APPLIER", "0") != "1" or requests is None:
        return None
    return ApplyDesk(requests.queue, enabled=True, requeue=requests.intake.requeue)


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
    # How long a boot usually takes is kept beside the request queue, so it survives a restart.
    data_dir = data_dir_from_env()
    boots = BootTimes(data_dir / "boot-times.json" if data_dir else None)
    monitor = StatusMonitor(server, address=env("SERVER_ADDRESS", "play.example.com"),
                            max_age=float(env("STATUS_MAX_AGE", "10")), boots=boots)
    pack_dir = Path(env("PACK_DIR", "/srv/pack"))
    catalog = PackCatalog(pack_dir)
    # One HTTP client (and cache) for Modrinth and CurseForge, shared by browsing and /setup.
    http = CachingHttp(FetchHttp())
    browse = browse_from_env(http)
    requests = requests_from_env(browse, catalog)
    upstream = upstream_from_env(requests, catalog)
    app = App(monitor, catalog, name=env("SERVER_NAME", "Tel Aviv"), browse=browse, requests=requests,
              admin_password=admin_password_from_env(), applier=applier_from_env(requests),
              setup=setup_from_env(pack_dir, http), upstream=upstream)
    servers = Servers(
        app,
        public=(env("LISTEN_HOST", "0.0.0.0"), int(env("LISTEN_PORT", "8080"))),
        admin=(env("ADMIN_LISTEN_HOST", "127.0.0.1"), int(env("ADMIN_LISTEN_PORT", "8081"))),
    ).start()
    stop = threading.Event()
    if upstream is not None:
        threading.Thread(target=upstream.run_forever, args=(stop,), daemon=True).start()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    servers.stop()


if __name__ == "__main__":
    main()
