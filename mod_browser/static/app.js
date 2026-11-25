// The status page. Every value from the API is written with textContent,
// never as HTML: mod names and player names are other people's text.
"use strict";

const STATUS_EVERY_MS = 15000;
const $ = (id) => document.getElementById(id);

const STATES = {
  online: { label: "Online", detail: "" },
  starting: { label: "Starting up", detail: "The server is booting or restarting. It usually takes a few minutes." },
  down: { label: "Down or restarting", detail: "The server isn't running right now. It comes back by itself after a restart; if it stays down, tell the owner." },
  unknown: { label: "Can't reach the status service", detail: "This page couldn't ask the server for its state. It tries again every 15 seconds." },
  site: { label: "The site is restarting", detail: "This page's service, or the machine it runs on, is restarting. If it is only the page, it is back in under a minute. This page tries again every 15 seconds." },
};

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "text") node.textContent = value;
    else if (key === "class") node.className = value;
    else node.setAttribute(key, value);
  }
  for (const child of children) if (child) node.append(child);
  return node;
}

function ago(iso) {
  if (!iso) return null;
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.round(hours / 24)} days ago`;
}

const when = new Intl.DateTimeFormat(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
const day = new Intl.DateTimeFormat(undefined, { year: "numeric", month: "short", day: "numeric" });

// ---- status ----
let lastStatus = null;

const clockOf = (at) => at.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
const valid = (iso) => { const at = iso ? new Date(iso) : null; return at && !Number.isNaN(at.getTime()) ? at : null; };

// "Back online around 4:12 AM (in about 6 min)": the server's estimate, learned from past restarts.
// A batch being applied has its own estimate; any other boot (a crash, a host reboot) uses `boot`.
const inMinutes = (at) => Math.max(1, Math.round((at.getTime() - Date.now()) / 60000));

function backOnline(status) {
  const batch = valid(status && status.apply && status.apply.backOnlineBy);
  if (batch) return `Back online around ${clockOf(batch)} (in about ${inMinutes(batch)} min).`;
  const boot = status && status.state === "starting" ? status.boot : null;
  const since = valid(boot && boot.since);
  if (!since) return "";
  const began = `Booting since ${clockOf(since)} (${ago(boot.since)}).`;
  const usual = Math.max(1, Math.round(boot.typicalSeconds / 60));
  if (boot.overdue) return `${began} That is longer than the usual ${usual} min; if it stays this way, tell the owner.`;
  const ready = valid(boot.readyBy);
  return ready ? `${began} Usually ready around ${clockOf(ready)} (in about ${inMinutes(ready)} min).` : began;
}

// "Down since 2:16 AM (3 min ago)": the first time the site saw it stopped.
function downSince(status) {
  const at = status && status.state === "down" && valid(status.downSince);
  return at ? `Down since ${clockOf(at)} (${ago(status.downSince)}).` : "";
}

function renderStatus(status, unreachable = "unknown") {
  const state = status ? status.state : unreachable;
  const band = document.querySelector(".band");
  band.dataset.state = state;
  // Later changes of state fade (app.css, body[data-live]); this first one does not.
  if (!("live" in document.body.dataset)) requestAnimationFrame(() => requestAnimationFrame(() => { document.body.dataset.live = ""; }));
  $("state-label").textContent = STATES[state].label;
  // A finished update ("online") is old news once the label says Online or Starting up: say nothing.
  const phase = status && status.apply ? status.apply.phase : "idle";
  const apply = phase !== "idle" && phase !== "online" ? status.apply.detail : "";
  // A real estimate ("Booting since 3:58 AM… usually ready around 4:02 AM") replaces the generic sentence.
  const eta = backOnline(status);
  const generic = eta && state === "starting" ? "" : STATES[state].detail;
  $("state-detail").textContent = [generic, apply, eta, downSince(status)].filter(Boolean).join(" ");
  $("checked").textContent = status && status.checkedAt ? `checked ${ago(status.checkedAt)}` : "";
  if (status && status.address) $("address").value = status.address;

  const players = status && status.players;
  $("players-count").textContent = players ? String(players.online) : "–";
  $("players-max").textContent = players ? `of ${players.max}` : "";
  const list = $("players");
  list.replaceChildren();
  if (players && players.names.length) {
    for (const name of players.names) list.append(el("li", { text: name }));
  } else {
    list.append(el("li", { class: "none", text: players ? "Nobody is on right now." : "Shown once the server is up." }));
  }

  const tps = status && status.tps;
  $("tps").textContent = tps ? tps.tps.toFixed(1) : "–";
  $("mspt").textContent = tps ? `${tps.msPerTick.toFixed(1)} ms per tick${tps.tps < 18 ? ": the server is lagging" : ""}` : "";
  const ticks = $("ticks");
  ticks.replaceChildren();
  const filled = tps ? Math.round(tps.tps) : 0;
  for (let i = 0; i < 20; i++) ticks.append(el("i", { class: i < filled ? "on" : "" }));
  ticks.classList.toggle("low", Boolean(tps && tps.tps < 18));
  ticks.setAttribute("aria-label", tps ? `${tps.tps.toFixed(1)} of 20 ticks per second` : "TPS not known");

  const restart = status && status.lastRestart;
  $("restart-ago").textContent = restart ? ago(restart) : "–";
  $("restart-at").textContent = restart ? when.format(new Date(restart)) : (state === "down" ? "Not running" : "");
}

async function refreshStatus() {
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    // 502/503/504 come from the proxy in front of the app: the app itself is restarting.
    if ([502, 503, 504].includes(response.status)) { renderStatus(null, "site"); return; }
    if (!response.ok) throw new Error(String(response.status));
    lastStatus = await response.json();
    renderStatus(lastStatus);
  } catch {
    renderStatus(null);
  }
}

// ---- mods ----

function addedCell(mod) {
  if (mod.origin === "base") return [el("span", { class: "added-by", text: "Base pack" })];
  const by = el("span", { class: "added-by", text: mod.addedBy || "Unknown" });
  const on = mod.addedOn
    ? el("span", { class: "added-on", text: day.format(new Date(`${mod.addedOn}T12:00:00`)) })
    : el("span", { class: "added-on", text: mod.origin === "extra" ? "before 12 Nov 2026" : "" });
  return [by, on];
}

function row(mod) {
  const source = mod.source.url
    ? el("a", { href: mod.source.url, rel: "noopener noreferrer", target: "_blank", text: mod.source.site })
    : el("span", { text: mod.source.site });
  const sideText = { both: "Both", client: "Client", server: "Server" }[mod.side] || mod.side;
  return el("tr", {}, [
    el("td", { class: "c-name" }, [
      el("span", { class: "mod-name", text: mod.name }),
      el("span", { class: "mod-file", text: mod.kind === "mod" ? mod.filename : `${mod.kind} · ${mod.filename}` }),
    ]),
    el("td", { class: "c-version" }, [el("span", { class: "version", text: mod.version || "–" })]),
    el("td", { class: "c-side" }, [el("span", { class: `side side-${mod.side}`, text: sideText })]),
    el("td", { class: "c-source" }, [source]),
    el("td", { class: "c-added" }, addedCell(mod)),
  ]);
}

// Matching happens on the server (/api/mods?q=&side=&kind=), so the page and
// the API can never disagree about what a search finds.
let total = 0;
let searchSeq = 0;

function renderMods(found) {
  const body = $("mod-rows");
  body.replaceChildren(...found.map(row));
  if (!found.length) {
    const query = $("q").value.trim();
    body.append(el("tr", {}, [el("td", { colspan: "5", class: "empty",
      text: query ? `Nothing in the pack matches “${query}”. It isn't installed, or it goes by another name.` : "Nothing matches these filters." })]));
  }
  $("result-line").textContent = found.length === total
    ? `Showing all ${total}.`
    : `${found.length} of ${total} match.`;
}

async function search() {
  const seq = ++searchSeq;
  const params = new URLSearchParams();
  for (const id of ["q", "side", "kind"]) {
    const value = $(id).value.trim();
    if (value) params.set(id, value);
  }
  try {
    const response = await fetch(`/api/mods${params.size ? `?${params}` : ""}`);
    if (!response.ok) throw new Error(String(response.status));
    const data = await response.json();
    if (seq !== searchSeq) return; // a newer keystroke's answer wins
    total = data.count;
    $("mod-count").textContent = String(data.count);
    const pack = data.pack;
    const parts = [`${pack.name}${pack.version ? ` ${pack.version}` : ""}`];
    if (pack.minecraft) parts.push(`Minecraft ${pack.minecraft}`);
    if (pack.neoforge) parts.push(`NeoForge ${pack.neoforge}`);
    $("pack-line").textContent = parts.join(", ");
    renderMods(data.mods);
  } catch {
    if (seq === searchSeq) $("result-line").textContent = "The mod list couldn't be read just now. Try again in a minute.";
  }
}

let searchTimer = 0;
function searchSoon() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(search, 150);
}

// ---- wiring ----
function copyAddress() {
  const input = $("address");
  const button = $("copy");
  const done = () => {
    button.querySelector(".button-text").textContent = "Copied";
    setTimeout(() => { button.querySelector(".button-text").textContent = "Copy"; }, 1800);
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(input.value).then(done, () => { input.select(); });
  } else {
    input.select();
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("copy").addEventListener("click", copyAddress);
  $("address").addEventListener("focus", (event) => event.target.select());
  $("filters").addEventListener("submit", (event) => event.preventDefault());
  $("q").addEventListener("input", searchSoon);
  for (const id of ["side", "kind"]) $(id).addEventListener("change", search);
  refreshStatus();
  search();
  setInterval(() => { if (!document.hidden) refreshStatus(); }, STATUS_EVERY_MS);
  // The relative times keep moving between checks.
  setInterval(() => { if (lastStatus) renderStatus(lastStatus); }, 30000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshStatus(); });
});
