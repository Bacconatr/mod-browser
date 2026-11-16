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
| Last restart | The launcher's first line in `logs/latest.log`, or, after log4j's midnight roll, in the newest rolled `YYYY-MM-DD-N.log.gz`; unknown rather than a wrong time when none is in reach |
| Booting since, usually ready by | While `starting`: the same start time, plus how long a boot usually takes, learned from the boots this app watched finish (`DATA_DIR/boot-times.json`, 4 min until one is). A boot past that reads as overdue, not a moving estimate; a start older than an hour is the previous run's and shows no estimate |
| Down since | While `down`: the first time this app saw the container stopped |
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

**The page's own restarts.** A 502, 503 or 504 with no JSON is Caddy answering
for an app that is restarting (or a guest that is down): the status page says
"The site is restarting" and the find page retries a read once.

## No Docker socket, and RCON is allowlisted

This runs on the Internet-facing guest, so it holds as little as it can:

- **No Docker socket.** Container state comes from DNS on the compose network.
- **RCON is read-only.** The adapter refuses every command but `list` and
  `neoforge tps` (not even `say`) before connecting (`mod_browser/rcon.py`, `ALLOWED_COMMANDS`).
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

- **Applying**: `applier.py` (the app's half) and `maintenance.py` + `dockerhost.py`
  (the runner on the guest). See "Applying requests".
- **`say`** stays off the app's allowlist: in-game warnings are the runner's (`rcon-cli`
  on the guest).

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
| `GET /admin`, `/admin/api/requests[/<id>/<action>]` | the owner: HTTP Basic, any user name, the vault password (20+ chars; a shorter or missing one leaves `/admin` a 404). The right password always gets in; 10 wrong guesses a minute lock that client out for a minute |
| `GET /admin-api/v1/requests?since=<seq>`, `POST /admin-api/v1/requests[/<id>/<action>]` | PVE, on the loopback listener only; no password, because only `docker exec` reaches it |

Actions: `approve`, `deny {reason}`, `override {version}`, `remove`, `requeue`.
`approve` and `deny` also take an optional `fingerprint`: the sha256 that
`/admin-api/v1/requests` serves with each entry (`fingerprint` in `queue.py`:
requester, ownerAdded, mod, dependencies, versionId, added, kind; sorted, compact
JSON), computed the same way as Homelab's `pve-mod-queue.py` (#1759). If the
request no longer has it, the answer is 409 with the current one and nothing
changes, so PVE never approves a request that changed after it posted it.
`since` returns every request changed after that `seq`, so a poller never
misses a change; the answer's `seq` is the next `since`.

Limits: 30 asks a minute (everyone together), 20 wrong names a minute per client
(a client is Caddy's `X-Forwarded-For`, believed only from `MOD_BROWSER_TRUSTED_PROXIES`,
default loopback; IPv6 grouped by /64), with a ceiling over all clients together, 200 open
requests in all (no cap per name: a friend may ask for as many as they like), bodies of 8 KiB. The queue is SQLite in
`MOD_BROWSER_DATA_DIR`; without that directory, the whitelist file or browsing,
requests are off and `/queue` and `/admin` are 404.

## Applying requests: the owner asks, the maintenance runner does it (off)

The app never stops, starts or backs up anything: it has no Docker socket and
never gets one. It only records the owner's ask, and a separate **maintenance
runner** (`mod_browser/maintenance.py`, real host adapter `dockerhost.py`) does
the work. The runner runs on guest 120 as root, started by PVE over
`pct exec 120` (Homelab's `aeronautics_maintenance` unit, which ships
disabled). It reaches the app on the loopback admin API through
`docker exec aeronautics-mod-browser python -m mod_browser.adminctl`, so request
states still change in one place (`applier.py`, `ApplyDesk`).

**Owner-triggered only.** `/admin` has "Apply what is queued when the server is
empty", "Restart now with what is queued" (a one-minute warning in game) and
"Cancel". There is no nightly slot. Behind `MOD_BROWSER_APPLIER=1`; while off,
every apply route answers 409 and `/api/status` has no `apply`.

One batch (everything queued, or an upstream update on its own), in order. Each
phase is written to the runner's journal (`/var/lib/mod-maintenance/journal.json`)
**before** its side effect:

1. **Stage** the candidate on a branch in a worktree beside the live pack (one
   commit per request, `Requested-by:` trailer, `index.toml`/`pack.toml` refreshed).
   A request queued by an older build without file hashes is resolved again first.
2. **Download**: packwiz-installer (the live server's own jar, in the same image)
   fills a *copy* of `mods/` for the candidate. The live server is still up.
3. **Recheck players** right before maintenance. *When empty*: anyone online, or
   no answer while the server runs, defers the batch untouched (back to `queued`,
   the ask stands). *Now*: "restarts in 1 minute" and "10 seconds" in game; a
   failed warning does not stop an owner's "now", and the result says so.
4. **Block joins**: new TCP connections to the game port are refused in
   `DOCKER-USER` (comment `mod-maintenance`). *When empty* checks once more behind
   the block and defers if anyone got in.
5. **Stop** gracefully (the backup sidecar too). It must have exited 0 with
   "All dimensions are saved" in its log, or the run halts.
6. **Cold backup** of `world/` (without the regenerable `DistantHorizons.sqlite`)
   and the server files a candidate can change (`mods`, `config`, `packwiz.json`,
   ...), to `/mnt/aeronautics-backups/maintenance/batch-N/`, verified with
   `tar --compare` and checksummed. No backup, no change.
7. **Test boot** of the candidate on a disposable copy whose world is extracted
   *from that backup* (which proves it restores), with no published port and no
   RCON secret. It fits in memory only because the live server is stopped. It
   must log Done and still be up 15 s later. A timeout kills it.
8. **Clean up** the test container and copy. If that cannot be confirmed, the run
   halts with the live server stopped: it would not fit beside a leftover.
9. **Promote**: merge the branch into the live pack, swap in the downloaded mods.
10. **Start** and require **fresh-boot evidence**: Done in `docker logs --since`
    *this* start, and the container still being that start (a restart by anyone
    else is not evidence). Then joins open and the requests are `applied`.

**On failure.** Before promote, the pack and world were never changed: the old
server is started (fresh Done required) and the requests are `test-failed`.
After promote: stop, revert the merge (the pack's tree must equal the one the run
started from), move the failed world and server files aside to
`/opt/aeronautics/maintenance/failed-N/` (kept, never deleted), restore the
verified backup, start, fresh Done, and only then `rolled-back`. Joins stay
blocked until a boot is verified. Anything that cannot be verified **halts**.

**Interrupted runs.** Before the stop, the next tick releases the batch and
tries again. From the stop on, the journal says "needs recovery", the status
page shows it, and nothing is claimed until the owner runs
`mod-maintenance recover`, which verifies before it records anything: a new
server is accepted only if the pack is the merge and the very start the runner
issued logged Done. Otherwise, after promotion or when joins may have reopened,
recovery halts for inspection: a reboot may have let players change the world,
so it does not automatically restore an older backup. Requests are never
marked `applied` or `rolled-back` without that evidence; an outcome the app did
not hear (it was down) is reported first on the next tick.

Known limits: a failed batch fails every request in it (no bisecting); two
requests in one batch that conflict with each other are caught by the test boot,
not before; a manual roll back of an applied batch is not built; between promote
and the start, `/pack/` already serves the new pack (the server is down then,
and a rollback is picked up by the next launch); the restored world has no
Distant Horizons LODs (they regenerate).

**Upstream updates** (`kind = "upstream"`, see "Upstream releases") carry `add`, `change` and `remove` entries; `change` and `remove` act on the metafile they name (a `.pw.toml` inside the pack, in any folder, and only when it names the same project), a removed metafile leaves `index.toml`, and an upstream update is applied **on its own**: a friend's request computed against the old pack waits for the next batch.

Known limits: a failed batch fails every request in it (no bisecting for the
culprit; the owner re-adds them one at a time with `add directly`); requests in
one batch are resolved against the pack individually, so two that conflict with
each other are caught by the test boot, not before; a manual "roll back" of an
already-applied batch is not built.
## Setting up the game: the CurseForge import (`/setup`)

`/curseforge/pack.zip` is a CurseForge-app modpack built on request from the
live pack: `manifest.json` names Minecraft and NeoForge from `pack.toml` and
every entry not marked `side = "server"` by its `[update.curseforge]` project
and file id (a Modrinth-fetched mod keeps its CurseForge twin's ids), with the
index's non-metafile files under `overrides/`. The CurseForge app downloads
even the mods whose authors block other launchers, so nothing is fetched by
hand. It is offered only while every player-side entry has CurseForge ids;
otherwise it is a 404 and `/api/setup`'s `curseforge.missing` names the rest.
It does not update itself: after a pack change a friend imports it again.

## Setting up the game: the Prism instance (`/setup`)

`/prism/instance.zip` is a Prism Launcher instance built on request from the
pack's own `pack.toml`: Minecraft and NeoForge components, a memory setting,
`packwiz-installer-bootstrap.jar` in `.minecraft/`, and the pre-launch command
`"$INST_JAVA" -jar packwiz-installer-bootstrap.jar -s client <pack.toml URL>`.
On every launch packwiz-installer makes the mods folder match the pack, adding,
updating and removing; `-s client` skips mods whose metafile says `side =
"server"`. `/setup` has the five steps, the server address, the base-pack mods
that must be downloaded by hand (the metafiles NOTES.md lists under a
"Distribution-blocked mods" heading), and "when did you last play?" against
`/api/pack/changes?since=YYYY-MM-DD`.

The launcher reads the pack from **`/pack/<file>`**: only `pack.toml`,
`index.toml` and the files `index.toml` lists, found through the index so
nothing else in the directory (NOTES.md, `.git`) is reachable. **Anything the
index lists is public: never put a secret in the pack.**

The bootstrap jar is not in this repository. The deploy must put a pinned
copy on disk (`MOD_BROWSER_BOOTSTRAP_JAR`); without it `/prism/instance.zip` is
a 404 and `/setup` says the instance is not published yet.

## Upstream releases (`upstream.py`)

Once a day (first tick after 04:00 `America/New_York`) the app reads the newest
**release** of CurseForge project 1518930 (the modpack), downloads its
`manifest.json`, looks every mod up (`POST /v1/mods/files`, `/v1/mods`, which
work for our key even though search does not) and compares it with the pack.
A release not seen before becomes one **owner-only** request (`kind =
"upstream"`, requester `Upstream`), shown on `/admin` with Added, Changed,
Removed and Kept lists. It is not on the public queue, and it does nothing
until the owner approves it; approving only queues it, and the applier takes it
through the same test boot, backup and rollback as anything else. A release
that changes nothing files nothing; one nobody decided on is denied as
"replaced" when a newer one arrives; a denied release is not asked again.

**What is kept.** A mod the pack holds as an extra, or that was added later, is
never changed or removed: Deep Seas 3.4.5 stays against a bundled 3.3.0
(`kept`, with `upstreamNewer` set when upstream is the newer one). Only base-pack
CurseForge projects are changed, and only base-pack mods (in `mods/`) are
removed: a resource or data pack upstream stops listing stays for the owner.

**Matching is by project, never by file name** (Homelab #1789). Every metafile
in the pack is compared, in whatever folder (`mods/`, `resourcepacks/`, the
pack root), by CurseForge project id. Only a metafile that names no CurseForge
project falls back to its jar's name without the version (the owner's pins are
often Modrinth files known upstream under another id), and only against a file
of the same kind (jar or not). v3.0HF was applied before this: it saw only
`mods/`, so 37 resource and data packs came in a second time as `mods/`
entries, and "Boatload ZH_HK Translation" matched the Boatload mod by name and
replaced it.

**Where an added project goes.** By its CurseForge class (`/v1/mods` gives
`classId`): mods in `mods/`; another type where the pack already keeps most of
that type (this pack keeps shaders and data packs at its root), else
`resourcepacks/` or `shaderpacks/`. A type the pack keeps nowhere, or a non-jar
headed for `mods/`, is refused by the applier rather than guessed.

**The applier checks identity too.** An `add` of a project the pack already
tracks in any folder is refused; a `change` or `remove` aimed at a metafile
that names a different project is refused (the batch fails before the server is
touched); a new metafile whose name another project's file has takes a numbered
name (`boatload-2.pw.toml`), never that file.

**Stale differences.** The request records the pack commit it was computed
against; if the pack has moved when the owner approves, it is held `approved`
with a note until the next daily check files a fresh one.

**A CurseForge outage** is logged once a day at most (kept in the queue's
database, so a restart does not repeat it); `GET /admin-api/v1/upstream` shows
the last result and error, and `POST .../upstream/check` (or the button on
`/admin`) checks now.

Pack operations: the request's `added` holds `{op: add | change | remove, ...}`
entries with what a metafile needs (`change` and `remove` name the pack metafile
they act on, and `change` keeps its side). Writing
them into the pack is the applier's job ("The applier"); until that is on, an
approved upstream update just sits `queued`. Added mods come in
with `side = "both"` because CurseForge's manifest does not say; the test boot
is what catches a client-only mod loaded on the server.

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
| `MOD_BROWSER_TRUSTED_PROXIES` | `127.0.0.1/32,::1/128` (whose `X-Forwarded-For` is believed) |
| `MOD_BROWSER_APPLIER` | `0` (see "Applying requests": the app records asks; the runner on the guest does the work) |
| `MOD_BROWSER_PUBLIC_URL` | `https://mods.example.com` (the pack URL in the instance is `<this>/pack/pack.toml`) |
| `MOD_BROWSER_BOOTSTRAP_JAR` | `/srv/prism/packwiz-installer-bootstrap.jar` |
| `MOD_BROWSER_PRISM_MIN_MB` / `_MAX_MB` | `2048` / `6144` |
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
