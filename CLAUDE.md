# CLAUDE.md

Small Python app, standard library only. Read README.md first.

- Tests are at the HTTP API (`App` + `Servers`) with fakes behind `ServerPort`
  (`tests/fakes.py`). Add a test there before adding behaviour; do not test
  internals. `tests/test_rcon.py` covers the one real adapter.
- No third-party packages, at runtime or in the image: this serves the
  Internet from Homelab guest 120.
- It holds no Discord, GitHub or Cloudflare credential and must never get
  one, and it has no Docker socket. Write API values into the page with
  `textContent`, never as HTML.
- RCON commands are allowlisted in `rcon.py` (`ALLOWED_COMMANDS`). Widen it
  only with the ticket that needs the command.
- Deploys only through Homelab's `aeronautics` role pinning a commit, which
  must stay reachable from `main`: merge PRs here with a merge commit, not a
  squash, when Homelab already pins a branch commit.
