// The owner's page. The browser asks for the password once (HTTP Basic) and
// sends it with every call below. Values are written with textContent.
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

async function call(method, path, body) {
  const response = await fetch(path, {
    method,
    headers: { accept: "application/json", ...(body ? { "content-type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await response.json(); } catch { /* not JSON */ }
  if (!response.ok) throw new Error(data.error || `The server answered ${response.status}.`);
  return data;
}

const say = (text) => { $("message").textContent = text; };

async function act(id, action, body) {
  try {
    await call("POST", `/admin/api/requests/${id}/${action}`, body || {});
    say(`${action}: done.`);
  } catch (error) {
    say(error.message);
  }
  load();
}

function button(label, handler) {
  const b = el("button", { type: "button", class: "button", text: label });
  b.addEventListener("click", handler);
  return b;
}

async function versionPicker(r, holder) {
  holder.replaceChildren(el("span", { class: "muted", text: "Reading versions…" }));
  try {
    const data = await (await fetch(`/api/catalog/versions?source=${r.mod.source}&id=${encodeURIComponent(r.mod.projectId)}`)).json();
    const select = el("select", { "aria-label": "Version" });
    for (const v of data.versions) {
      const option = el("option", { value: v.id, text: v.versionNumber + (v.channel === "release" ? "" : ` (${v.channel})`) });
      if (v.id === r.versionId) option.selected = true;
      select.append(option);
    }
    holder.replaceChildren(select, button("Use this version", () => act(r.id, "override", { version: select.value })));
  } catch {
    holder.replaceChildren(el("span", { class: "problem", text: "Could not read the versions." }));
  }
}

function diffList(title, items, describe) {
  if (!items.length) return null;
  const list = el("ul", { class: "added" });
  for (const item of items) list.append(el("li", { text: describe(item) }));
  return el("details", {}, [el("summary", { text: `${title} (${items.length})` }), list]);
}

function upstreamDetails(r) {
  const d = (r.meta && r.meta.diff) || { added: [], removed: [], changed: [], kept: [] };
  return el("div", { class: "meta-block" }, [
    el("div", { class: "meta" }, [el("span", { text: "Upstream release; applied as a whole, through the same test boot and backup." })]),
    diffList("Added", d.added, (m) => `${m.name} ${m.version}`),
    diffList("Changed", d.changed, (m) => `${m.name}: ${m.from} to ${m.to}`),
    diffList("Removed", d.removed, (m) => `${m.name} ${m.version}`),
    diffList("Kept (your pin stays)", d.kept, (m) => `${m.name}: pack has ${m.pack}, upstream has ${m.upstream}`),
  ]);
}

function row(r) {
  const open = r.state !== "denied";
  const upstream = r.kind === "upstream";
  const holder = el("div", { class: "actions" });
  const reason = el("input", { type: "search", placeholder: "Reason for denying", "aria-label": "Reason for denying", maxlength: "300" });
  if (r.state === "pending") holder.append(button("Approve", () => act(r.id, "approve")));
  if (r.state === "approved") holder.append(button("Try queueing again", () => act(r.id, "requeue")));
  if (open) {
    if (!upstream) holder.append(button("Change version", () => versionPicker(r, picker)));
    holder.append(reason, button("Deny", () => act(r.id, "deny", { reason: reason.value })));
  }
  if (r.state === "approved" || r.state === "queued") holder.append(button("Remove from queue", () => act(r.id, "remove")));
  const picker = el("div", { class: "actions" });
  const deps = r.dependencies.length ? el("div", { class: "meta" }, [el("span", { text: `with ${r.dependencies.join(", ")}` })]) : null;
  return el("li", { class: "found" }, [el("div", {}, [
    el("strong", { text: upstream ? r.mod.name : `${r.mod.name} ${r.mod.versionNumber}` }),
    upstream ? upstreamDetails(r) : null,
    el("div", { class: "meta" }, [el("span", { text: r.state }), el("span", { text: `asked by ${r.requester}` }), el("span", { text: r.mod.source })]),
    deps,
    r.reason ? el("div", { class: "meta" }, [el("span", { text: r.reason })]) : null,
    r.note ? el("div", { class: "problem", text: r.note }) : null,
    holder,
    picker,
  ])]);
}

async function load() {
  try {
    const { requests } = await call("GET", "/admin/api/requests");
    const list = $("list");
    list.replaceChildren();
    for (const r of requests.reverse()) list.append(row(r));
    if (!requests.length) say("No requests yet.");
  } catch (error) {
    say(error.message);
  }
}

$("restart-now").addEventListener("click", async () => {
  try {
    const data = await call("POST", "/admin/api/restart-now", {});
    say(data.apply.detail || "Restart requested.");
  } catch (error) {
    say(error.message);
  }
$("upstream-check").addEventListener("click", async () => {
  try {
    const { upstream } = await call("POST", "/admin/api/upstream/check", {});
    say(upstream.error ? `Upstream check failed: ${upstream.error}` : `Upstream check: ${upstream.lastResult}.`);
  } catch (error) {
    say(error.message);
  }
  load();
});

$("add").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await call("POST", "/admin/api/requests", { source: $("add-source").value, id: $("add-id").value.trim() });
    $("add-id").value = "";
    say("Added and queued.");
  } catch (error) {
    say(error.message);
  }
  load();
});
load();
