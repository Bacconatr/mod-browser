# CLAUDE.md

Small Node 24 app, no runtime dependencies. Read README.md first.

- Tests are at the HTTP API (`createApp`) with fakes behind `ServerPort` and
  `PackSource`. Add a test there before adding behaviour; do not test internals.
- Erasable TypeScript only: no `enum`, no parameter properties, imports end in `.ts`.
- This app serves the Internet from Homelab guest 120. It holds no Discord,
  GitHub or Cloudflare credential and must never get one; it has no Docker
  socket. Escape every string that reaches HTML (`render.ts`'s `esc`).
- Deploys only through Homelab's `mod_browser` role pinning a commit.
