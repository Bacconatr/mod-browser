// Find a mod: search, pick a version, read the verdict. Read-only.
// Every value from the API is written with textContent, never as HTML:
// mod names and descriptions are other people's text.
"use strict";

const $ = (id) => document.getElementById(id);
const SOURCE = { modrinth: "Modrinth", curseforge: "CurseForge" };
const SIDE = { both: "Every player needs it", client: "Client only: your game, not the server", server: "Server only: players do not install it", unknown: "Side not stated" };

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

const compact = new Intl.NumberFormat(undefined, { notation: "compact" });
const dayFormat = new Intl.DateTimeFormat(undefined, { year: "numeric", month: "short", day: "numeric" });
const day = (iso) => (iso && !Number.isNaN(Date.parse(iso)) ? dayFormat.format(new Date(iso)) : "unknown");

// 502/503/504 with no JSON is the proxy, not the app: the app is restarting.
const PROXY_DOWN = new Set([502, 503, 504]);
const failure = (status, body) => (body && body.error)
  || (PROXY_DOWN.has(status) ? "The site is restarting. Try again in a few seconds." : `The server answered ${status}.`);

// A read is retried once after a proxy answer, so a restart does not show as an error, and up to
// three times after a 429: the server checks three mods at a time, and opening several at once
// queued the rest behind "busy, try again in a moment" (measured on the live site, 2026-11-14).
async function api(path, tries = { busy: 0, restart: 0 }) {
  const response = await fetch(path, { headers: { accept: "application/json" } });
  let body = null;
  try { body = await response.json(); } catch { /* not JSON */ }
  const restarting = !body && PROXY_DOWN.has(response.status) && tries.restart < 1;
  const busy = response.status === 429 && tries.busy < 3;
  if (!response.ok && (restarting || busy)) {
    await new Promise((done) => setTimeout(done, busy ? 1500 + 1000 * tries.busy + Math.random() * 1000 : 3000));
    return api(path, { busy: tries.busy + (busy ? 1 : 0), restart: tries.restart + (restarting ? 1 : 0) });
  }
  if (!response.ok) throw new Error(failure(response.status, body));
  return body;
}

let searchToken = 0;

async function search(query) {
  const token = ++searchToken;
  const list = $("results");
  list.replaceChildren();
  $("warnings").replaceChildren();
  $("detail").hidden = true;
  $("result-line").textContent = "Searching…";
  try {
    const data = await api(`/api/catalog/search?q=${encodeURIComponent(query)}`);
    if (token !== searchToken) return;
    for (const warning of data.warnings) $("warnings").append(el("li", { text: warning }));
    $("result-line").textContent = data.results.length
      ? `${data.results.length} found for NeoForge 1.21.1`
      : `Nothing for “${query}” on NeoForge 1.21.1.`;
    for (const project of data.results) list.append(resultRow(project));
  } catch (error) {
    if (token === searchToken) $("result-line").textContent = error.message;
  }
}

function resultRow(project) {
  const open = el("button", { type: "button", class: "link-button", text: project.name });
  open.addEventListener("click", () => showProject(project));
  const icon = project.iconUrl && /^https:\/\/(cdn\.modrinth\.com|media\.forgecdn\.net|cdn\.curseforge\.com)\//.test(project.iconUrl)
    ? el("img", { src: project.iconUrl, alt: "", width: "48", height: "48", loading: "lazy", referrerpolicy: "no-referrer" })
    : el("span", { class: "noicon", "aria-hidden": "true" });
  return el("li", { class: "found" }, [
    icon,
    el("div", {}, [
      el("strong", {}, [open]),
      el("div", { class: "meta" }, [
        el("span", { text: SOURCE[project.source] || project.source }),
        el("span", { text: `${compact.format(project.downloads)} downloads` }),
        el("span", { text: `Updated ${day(project.updatedAt)}` }),
      ]),
      el("p", { class: "muted", text: project.description }),
    ]),
  ]);
}

async function showProject(project) {
  const panel = $("detail");
  panel.hidden = false;
  panel.replaceChildren(el("h2", { text: project.name }), el("p", { class: "muted", text: "Reading its versions…" }));
  panel.scrollIntoView({ block: "nearest" });
  try {
    const data = await api(`/api/catalog/versions?source=${project.source}&id=${encodeURIComponent(project.id)}`);
    renderPanel(project, data);
  } catch (error) {
    panel.replaceChildren(el("h2", { text: project.name }), el("p", { class: "problem", text: error.message }));
  }
}

function renderPanel(project, data) {
  const panel = $("detail");
  const head = [el("h2", { text: project.name }), el("p", { class: "muted", text: project.description })];
  if (!data.versions.length) {
    panel.replaceChildren(...head, el("p", { text: "No version of this mod is published for NeoForge 1.21.1." }));
    return;
  }
  const select = el("select", { id: "version" });
  for (const v of data.versions) {
    const label = v.versionNumber + (v.channel === "release" ? "" : ` (${v.channel})`) + (v.blocked ? " (download blocked)" : "");
    const option = el("option", { value: v.id, text: label });
    if (data.default && v.id === data.default.id) option.selected = true;
    select.append(option);
  }
  let verdict = el("div", { id: "verdict", "aria-live": "polite" });
  const show = (node) => { verdict.replaceWith(node); verdict = node; };
  const run = async () => {
    // With the default selected, send no version so the server labels a beta fallback.
    const chosen = data.default && select.value === data.default.id ? "" : `&version=${encodeURIComponent(select.value)}`;
    show(el("div", { id: "verdict", "aria-live": "polite" }, [el("p", { class: "muted", text: "Checking it against the pack…" })]));
    try {
      show(renderVerdict(project, await api(`/api/catalog/resolve?source=${project.source}&id=${encodeURIComponent(project.id)}${chosen}`)));
    } catch (error) {
      show(el("div", { id: "verdict", "aria-live": "polite" }, [el("p", { class: "problem", text: error.message })]));
    }
  };
  select.addEventListener("change", run);
  panel.replaceChildren(
    ...head,
    el("p", { class: "meta" }, [el("span", { text: SOURCE[project.source] || project.source }), el("span", { text: SIDE[project.side] || "" })]),
    el("div", { class: "field" }, [el("label", { for: "version", text: "Version" }), select]),
    verdict,
  );
  run();
}

function renderVerdict(project, v) {
  const heads = { ok: "This can be added.", "already-installed": "Already on the server.", refused: "This cannot be added.", unavailable: "This cannot be added." };
  const box = el("div", { id: "verdict", "aria-live": "polite", "data-status": v.status }, [el("h3", { text: heads[v.status] || v.status })]);
  if (v.version && v.version.fallback === "beta") {
    box.append(el("p", { class: "muted" }, [document.createTextNode("There is no release of this mod, so this is its newest "), el("strong", { text: "beta" }), document.createTextNode(".")]));
  }
  if (v.installedAs) box.append(el("p", { text: `It is in the pack as ${v.installedAs.modId} ${v.installedAs.version}.` }));
  if (v.side === "client") box.append(el("p", { class: "muted", text: "This mod runs only on players' computers. The server does not load it; friends add it themselves." }));
  if (v.added.length) {
    box.append(el("h3", { text: "What would be added" }));
    const list = el("ul", { class: "added" });
    for (const a of v.added) {
      list.append(el("li", {}, [
        el("strong", { text: a.name }),
        document.createTextNode(" "),
        el("span", { class: "muted", text: a.versionNumber + (a.channel === "release" ? "" : ` (${a.channel})`) }),
        el("div", { class: "meta" }, [el("span", { text: SOURCE[a.source] || a.source }), el("span", { text: a.reason }), a.replaces ? el("span", { text: `replaces ${a.replaces}` }) : null]),
      ]));
    }
    box.append(list);
  }
  if (v.problems.length) {
    box.append(el("h3", { text: "Why not" }));
    const list = el("ul", { class: "problems" });
    for (const p of v.problems) list.append(el("li", { text: p.message }));
    box.append(list);
  }
  if (v.status === "ok" && requestsEnabled()) box.append(requestForm(project, v));
  return box;
}

const requestsEnabled = () => {
  const meta = document.querySelector('meta[name="requests-enabled"]');
  return !!meta && meta.content === "true";
};

// The name is remembered in this browser only, as a convenience.
const savedName = () => { try { return localStorage.getItem("mc-name") || ""; } catch { return ""; } };
const saveName = (name) => { try { localStorage.setItem("mc-name", name); } catch { /* private window */ } };

function requestForm(project, v) {
  const input = el("input", { id: "mc-name", type: "text", maxlength: "16", autocomplete: "off", spellcheck: "false", placeholder: "Your Minecraft name", value: savedName() });
  const go = el("button", { type: "submit", class: "button solid", text: "Ask for it" });
  const status = el("p", { class: "muted", role: "status", "aria-live": "polite" });
  const form = el("form", { class: "request-form" }, [
    el("div", { class: "field" }, [el("label", { for: "mc-name", text: "Your Minecraft name (it must be on the whitelist)" }), input]),
    go,
  ]);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    go.disabled = true;
    status.className = "muted";
    status.textContent = "Sending…";
    try {
      const response = await fetch("/api/requests", {
        method: "POST",
        headers: { "content-type": "application/json", accept: "application/json" },
        body: JSON.stringify({ name: input.value.trim(), source: project.source, id: project.id, version: v.version.id }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(failure(response.status, data));
      saveName(input.value.trim());
      status.textContent = `Asked. It is ${data.request.state} until the owner decides. See it in the queue.`;
      form.replaceChildren(status, el("a", { href: "/queue", text: "Open the queue" }));
    } catch (error) {
      status.className = "problem";
      status.textContent = error.message;
      go.disabled = false;
    }
  });
  form.append(status);
  return form;
}

$("find").addEventListener("submit", (event) => {
  event.preventDefault();
  const query = $("q").value.trim();
  if (query.length >= 2) search(query);
});
