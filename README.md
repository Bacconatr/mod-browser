# mod-browser

The mod site for the Aeronautics server (Homelab guest 120): live status, who is
online, TPS, and the pack's mod list with who added each mod and when. Later
slices add searching Modrinth/CurseForge, friend requests, an admin page, and
an applier that restarts the server with approved mods. The plan is spec
[Bacconatr/Homelab#1675](https://github.com/Bacconatr/Homelab/issues/1675).

Deployed only through the Homelab repo's `mod_browser` role, which pins a
commit of this repo. Merging here deploys nothing.

## Run the tests

    npm ci
    npm run typecheck && npm test

Node 24 (see `.nvmrc`) runs the TypeScript directly: no build step and no
runtime dependencies. `typescript` is a dev dependency, for `npm run typecheck`.
Only erasable syntax is allowed (no `enum`, no constructor parameter
properties), because type stripping cannot erase them.

## Shape

- `src/ports.ts` the two seams: `ServerPort` (the game: RCON and the log) and
  `PackSource` (the packwiz git repository).
- `src/app.ts` the HTTP API and pages. **Tests drive this**, with fakes behind
  both ports (`test/fakes.ts`). They assert what a caller sees, never internals.
- `src/rcon-server.ts`, `src/pack.ts` the real adapters, each tested against a
  stand-in for what is on the other side (a fake RCON server, a throwaway git repo).

## Configuration (environment)

| Variable | Default | Meaning |
|---|---|---|
| `RCON_PASSWORD_FILE` | required | file holding the server's RCON password |
| `RCON_HOST` / `RCON_PORT` | `mc` / `25575` | the game container on the compose network |
| `PACK_DIR` | `/pack` | the packwiz repository, mounted read-only |
| `LATEST_LOG` | unset | the server's `latest.log`; its creation time is the "last restart" |
| `SERVER_ADDRESS` | `play.example.com` | shown to friends |
| `PORT` / `HOST` | `3000` / `0.0.0.0` | listen address |

No secret belongs in this repo; CI scans the whole history with gitleaks.
