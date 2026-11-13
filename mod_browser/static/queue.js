// The public request queue. Values are written with textContent, never as HTML.
"use strict";

const $ = (id) => document.getElementById(id);
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
const dayFormat = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });
const day = (iso) => (iso && !Number.isNaN(Date.parse(iso)) ? dayFormat.format(new Date(iso)) : "");
const LABEL = { pending: "Pending", approved: "Approved", queued: "Queued", denied: "Denied" };

async function load() {
  try {
    const response = await fetch("/api/requests", { headers: { accept: "application/json" } });
    if (!response.ok) throw new Error(String(response.status));
    const { requests } = await response.json();
    $("result-line").textContent = requests.length ? `${requests.length} requests` : "Nobody has asked for anything yet.";
    const rows = $("rows");
    rows.replaceChildren();
    for (const r of requests) {
      const mod = el("td", {}, [el("strong", { text: r.mod.name }), document.createTextNode(" "), el("span", { class: "muted", text: r.mod.versionNumber + (r.mod.channel === "release" ? "" : ` (${r.mod.channel})`) })]);
      if (r.dependencies.length) mod.append(el("div", { class: "meta" }, [el("span", { text: `with ${r.dependencies.join(", ")}` })]));
      if (r.reason) mod.append(el("div", { class: "meta" }, [el("span", { text: r.reason })]));
      rows.append(el("tr", {}, [mod, el("td", { text: r.requester }), el("td", { text: LABEL[r.state] || r.state }), el("td", { text: day(r.createdAt) })]));
    }
  } catch {
    $("result-line").textContent = "Could not load the queue. It will try again.";
  }
}
load();
setInterval(() => { if (!document.hidden) load(); }, 30000);
