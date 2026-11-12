# mod-browser

The status page and mod list for the Aeronautics server ("Tel Aviv", guest
120 in [Homelab](https://github.com/Bacconatr/Homelab)), served at
`https://mods.example.com`. Spec: Homelab #1675. This repo: #1679
(status page and mod list). The request queue, `/admin` and Discord control
come in later tickets.

Deployed by Homelab's `aeronautics` role (`tasks/mod-browser.yml`), pinned to
a commit of this repo. Bump the pin in a Homelab PR, like an image tag.

## What it shows, and where each value comes from

| Value | Source |
|---|---|
| State: online / starting / down | Docker's embedded DNS on the game's compose network, then RCON `list` |
| Who is online, the max | RCON `list` |
| TPS, ms per tick | RCON `neoforge tps` (the `Overall` line) |
| Last restart | First line of the server's `logs/latest.log`, rewritten on every start |
| Mods: name, jar, side, source | The packwiz metafiles (`*.pw.toml`) in the pack repo; the search box asks `/api/mods?q=`, so matching lives on the server only |
| Version | Parsed from the jar's filename (packwiz stores no version string) |
| Added by / added on | The pack repo's git history; see below |
| Server address | `MOD_BROWSER_SERVER_ADDRESS` |

**Added by / on.** The latest commit that added the metafile names who and
when. A `Requested-by: <name>` trailer in that commit's body wins over its
author. The repo's first commit is the migrated pack (Homelab #1677): its
metafiles are "base pack" unless the pack's `NOTES.md`, section "Mods added on
top of", lists them. Those are the owner's pre-packwiz extras, dated by the
first date their note mentions.

**Down within a minute.** Status is cached for `MOD_BROWSER_STATUS_MAX_AGE`
(10 s) and refreshed on the next request; the page polls every 15 s. A
stopped container drops off the compose network, its name stops resolving,
and the next check reads "down".

## No Docker socket, and RCON is allowlisted

This runs on the Internet-facing guest, so it holds as little as it can:

- **No Docker socket.** Container state comes from DNS on the compose network.
- **RCON is read-only.** The adapter refuses every command but `list` and
  `neoforge tps` before connecting (`mod_browser/rcon.py`, `ALLOWED_COMMANDS`).
  The RCON password is generated on the guest and never leaves it.
- **No Discord or GitHub credential**, ever (spec story 40). PVE does the
  Discord side by polling the admin API.
- **Standard library only.** No third-party Python packages in the image.
- Strict CSP (`default-src 'none'`, scripts and styles from self only), and
  every API value is written into the page with `textContent`.
- GET and HEAD only. A request with a body closes its connection after the
  answer, so an unread body is never parsed as a second request on a
  connection Caddy reuses. At most 48 connections at once, 15 s idle timeout.
- A broken pack is retried once per 30 s, not per request, and the last good
  list keeps being served. Route failures are logged to stderr with a traceback.

## Layout

```
mod_browser/
  ports.py     ServerPort: container_running(), rcon(), last_start()
  rcon.py      the real ServerPort: RCON client, DNS probe, log reader
  status.py    StatusMonitor: online / starting / down, players, TPS
  pack.py      PackCatalog: metafiles + git history -> the mod list
  web.py       App routes; the public and admin listeners
  static/      the page (index.html, app.css, app.js)
tests/
  fakes.py     FakeServer, and a throwaway packwiz git repo
  test_api.py  the HTTP API with the fakes behind it
  test_rcon.py the RCON adapter against a fake RCON socket
docs/design-brief.md   the page's design contract (Impeccable); not served
```

### Seams for the next tickets

- **Request queue**: a `Queue` module and `POST/GET /api/requests` in
  `App.public_routes`. Requests take only a whitelisted Minecraft name.
- **Catalog port** (Modrinth + CurseForge search): a second Protocol in
  `ports.py`, a real adapter beside `rcon.py`, a fake in `tests/fakes.py`.
- **`/admin`**: on the public listener behind the vault password. Today it
  is a 404 like any unknown path.
- **Admin API**: `App.admin_routes`, on the second listener, bound to
  127.0.0.1 inside the container and never published or proxied. PVE reaches
  it with `pct exec 120 -- docker exec aeronautics-mod-browser ...`.
- **`say` countdowns**: add the command to `ALLOWED_COMMANDS` with the
  ticket that needs it.

## Configuration (environment)

| Variable | Default |
|---|---|
| `MOD_BROWSER_LISTEN_HOST` / `_PORT` | `0.0.0.0` / `8080` |
| `MOD_BROWSER_ADMIN_LISTEN_HOST` / `_PORT` | `127.0.0.1` / `8081` |
| `MOD_BROWSER_RCON_HOST` / `_PORT` | `mc` / `25575` |
| `MOD_BROWSER_RCON_PASSWORD_FILE` | `/run/secrets/aeronautics/rcon` |
| `MOD_BROWSER_CONTAINER_NET` | `172.16.0.0/12` (a name resolving outside it counts as down) |
| `MOD_BROWSER_LOG_FILE`, `_LOG_TZ` | `/srv/logs/latest.log`, `America/New_York` |
| `MOD_BROWSER_PACK_DIR` | `/srv/pack` |
| `MOD_BROWSER_SERVER_NAME`, `_SERVER_ADDRESS` | `Tel Aviv`, `play.example.com` |
| `MOD_BROWSER_STATUS_MAX_AGE` | `10` (seconds) |

## Develop

```bash
python3 -m unittest discover -s tests -t . -v   # needs git; Python 3.11+
```

CI (`.github/workflows/ci.yml`) runs the same on the homelab's self-hosted
runner, plus gitleaks over the full history.
