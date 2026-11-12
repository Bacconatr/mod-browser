import type { ModEntry } from "./ports.ts";
import type { Status } from "./status.ts";

const esc = (s: string): string =>
  s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);

const SOURCE: Record<string, string> = { modrinth: "Modrinth", curseforge: "CurseForge" };

export function renderStatus(s: Status): string {
  const up = s.state === "online";
  const names = s.players.names.length
    ? `<p class="names">${s.players.names.map(esc).join(", ")}</p>`
    : up
      ? `<p class="names quiet">Nobody is on.</p>`
      : "";
  const restart = s.lastRestart ? `<time datetime="${esc(s.lastRestart)}">${esc(s.lastRestart.slice(0, 16).replace("T", " "))} UTC</time>` : "unknown";
  return `<section id="status" aria-live="polite" data-state="${s.state}">
  <h2 class="state"><span class="dot" aria-hidden="true"></span>${up ? "Online" : "Down or restarting"}</h2>
  ${
    up
      ? `<dl>
    <div><dt>Players</dt><dd>${s.players.online} of ${s.players.max ?? "?"}</dd></div>
    <div><dt>TPS</dt><dd>${s.tps === null ? "unknown" : s.tps.toFixed(1)}</dd></div>
    <div><dt>Last restart</dt><dd>${restart}</dd></div>
  </dl>
  ${names}`
      : `<p class="quiet">The server is not answering. If it is restarting, it is back in a few minutes. This page checks again by itself.</p>`
  }
  <p class="address">Join at <code>${esc(s.address)}</code></p>
</section>`;
}

function renderMods(mods: ModEntry[], total: number, q: string, error: boolean): string {
  if (error) {
    return `<p class="quiet" role="status">The mod list is unavailable right now. The server's status above is still current.</p>`;
  }
  const rows = mods
    .map((m) => {
      const origin = m.basePack
        ? `<span class="base">Shipped with the base pack</span>`
        : `Added by ${esc(m.addedBy)}${m.addedOn ? ` on <time>${esc(m.addedOn)}</time>` : ""}`;
      return `<li>
      <div class="mod"><strong>${esc(m.name)}</strong> <span class="ver">${esc(m.version)}</span></div>
      <div class="meta"><span>${esc(SOURCE[m.source] ?? m.source)}</span><span>${m.side === "both" ? "Both sides" : m.side === "client" ? "Client only" : "Server only"}</span><span>${origin}</span></div>
    </li>`;
    })
    .join("\n");
  const count = q ? `${mods.length} of ${total} mods match` : `${total} mods`;
  return `<p class="count" role="status">${count}</p>
  ${mods.length ? `<ul class="mods">${rows}</ul>` : `<p class="quiet">No mod matches “${esc(q)}”. If you want it added, requests open soon.</p>`}`;
}

export function renderPage(opts: { status: Status; mods: ModEntry[]; total: number; q: string; packError: boolean }): string {
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tel Aviv: the Aeronautics server</title>
<link rel="stylesheet" href="/style.css">
<script src="/app.js" defer></script>
</head>
<body>
<main>
  <header><h1>Tel Aviv</h1><p class="sub">All of Create: Aeronautics, NeoForge 1.21.1</p></header>
  ${renderStatus(opts.status)}
  <section aria-labelledby="mods-h">
    <h2 id="mods-h">Mods on the server</h2>
    <form method="get" action="/" role="search">
      <label for="q">Search the pack</label>
      <div class="row"><input id="q" name="q" type="search" value="${esc(opts.q)}" placeholder="Create Connected" autocomplete="off"><button>Search</button></div>
    </form>
    ${renderMods(opts.mods, opts.total, opts.q, opts.packError)}
  </section>
</main>
</body>
</html>`;
}
