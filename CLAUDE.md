# CLAUDE.md

Small Python app, standard library only. Read README.md first.

- Tests are at the HTTP API (`App` + `Servers`) with fakes behind `ServerPort`
  (`tests/fakes.py`). Add a test there before adding behaviour; do not test
  internals. `tests/test_rcon.py` covers the one real adapter.
- No third-party packages, at runtime or in the image: this serves the
  Internet.
- It holds no Discord, GitHub or Cloudflare credential and must never get
  one, and it has no Docker socket. Write API values into the page with
  `textContent`, never as HTML.
- RCON commands are allowlisted in `rcon.py` (`ALLOWED_COMMANDS`). Widen it
  only with the ticket that needs the command.
- Deployments pin a commit of this repo, which must stay reachable from
  `main`: merge PRs here with a merge commit, not a squash, when a deployment
  already pins a branch commit.
- The repo is public: no real player names, hostnames, addresses or
  personal details in code, tests or docs (use `example.com` and made-up
  names). CI's fork guard (`if:` on every job) must stay.
