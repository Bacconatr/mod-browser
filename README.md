# mod-browser

The status page and mod list for the Aeronautics server ("Tel Aviv", guest
120 in [Homelab](https://github.com/Bacconatr/Homelab)), served at
`https://mods.example.com`. Spec: Homelab #1675. This repo: #1679
(status page and mod list) and #1680 (read-only mod search and compatibility
verdicts). The request queue, `/admin` and Discord control come in later tickets.

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

- **Applier** (next ticket): reads `queue.all()` for `queued` requests and
  moves them on (`testing`, `applying`, `applied`, `test-failed`,
  `rolled-back`). The states and the record live in `queue.py`.
- **`say` countdowns**: add the command to `ALLOWED_COMMANDS` with the
  ticket that needs it.

## Requests, the queue and /admin

Friends ask from `/add` (a name, checked against the server's `whitelist.json`
and never listed back), see the queue at `/queue`, and the owner decides on
`/admin`. States: `pending -> approved -> queued`, or `denied` (with a reason).
Approval re-resolves the mod against the pack as it is now: if it still fits
it is queued, otherwise it stays `approved` with a note (owner-only) saying
why, and "try queueing again" retries. "Remove from queue" denies an approved
or queued request with a fixed reason.

| Route | Who |
|---|---|
| `GET /queue`, `GET /api/requests` | anyone |
| `POST /api/requests` `{name, source, id, version}` | anyone with a whitelisted name; JSON only, same origin |
| `GET /admin`, `/admin/api/requests[/<id>/<action>]` | the owner: HTTP Basic, any user name, the vault password (20+ chars; a shorter or missing one leaves `/admin` a 404). 10 wrong guesses a minute lock that client out for a minute |
| `GET /admin-api/v1/requests?since=<seq>`, `POST /admin-api/v1/requests[/<id>/<action>]` | PVE, on the loopback listener only; no password, because only `docker exec` reaches it |

Actions: `approve`, `deny {reason}`, `override {version}`, `remove`, `requeue`.
`since` returns every request changed after that `seq`, so a poller never
misses a change; the answer's `seq` is the next `since`.

Limits: 30 asks a minute (everyone together), 20 wrong names a minute per client
(a client is Caddy's `X-Forwarded-For`, believed only from a private or
loopback peer), 5 open
requests per name, 200 open in all, bodies of 8 KiB. The queue is SQLite in
`MOD_BROWSER_DATA_DIR`; without that directory, the whitelist file or browsing,
requests are off and `/queue` and `/admin` are 404.

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
| `MOD_BROWSER_DATA_DIR` | `/srv/data` (writable; holds `requests.db`) |
| `MOD_BROWSER_WHITELIST_FILE` | `/srv/whitelist.json` (read-only) |
| `MOD_BROWSER_ADMIN_PASSWORD_FILE` | `/run/secrets/aeronautics/admin` |

## Develop

```bash
python3 -m unittest discover -s tests -t . -v   # needs git; Python 3.11+
```

CI (`.github/workflows/ci.yml`) runs the same on the homelab's self-hosted
runner, plus gitleaks over the full history.

## Find a mod (#1680)

`/add` searches Modrinth for NeoForge 1.21.1, shows its versions, and explains
what the requested jar and its required dependencies would add to the pack.
The verdict reads each jar's `neoforge.mods.toml` and the server's installed
mods; it does not change the pack or the game. It flags installed mods,
incompatibilities, unsatisfied ranges, client-only mods, and blocked downloads.

CurseForge text search currently returns 403 for this key (Homelab #1680).
The page accepts a CurseForge project ID pasted from the project's About panel;
project and file lookup work. A pasted slug link asks for that ID. Dependency
search may therefore miss a CurseForge-only version while text search is off;
the verdict refuses rather than claiming that an incompatible version works.
`MOD_BROWSER_CURSEFORGE_SEARCH` can be enabled when the API starts
returning 200. `MOD_BROWSER_CURSEFORGE_API_KEY_FILE` points at a guest-local key
file, and `MOD_BROWSER_MODS_DIR` points at the server's read-only mods folder.
The deploy role must explicitly supply those mounts before the live page can
search CurseForge or compare installed jars.

Catalog and jar fetches only use HTTPS hosts in `mod_browser/net.py`; redirects
are checked again, bodies are capped, and one resolve makes at most ten
dependency lookups. The browser writes upstream text with `textContent`.
