// The setup page: the live details around the steps. Values are written with textContent.
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

const remember = (key, value) => { try { localStorage.setItem(key, value); } catch { /* private window */ } };
const recall = (key) => { try { return localStorage.getItem(key) || ""; } catch { return ""; } };
const today = () => new Date().toISOString().slice(0, 10);

async function details() {
  try {
    const data = await (await fetch("/api/setup")).json();
    $("memory-note").textContent = `The instance is set up for Minecraft ${data.minecraft} with NeoForge ${data.neoforge} and up to ${Math.round(data.instance.memoryMb / 1024)} GB of memory. Raise it in the instance's settings if your computer has plenty.`;
    if (!data.instance.available) {
      $("download").setAttribute("aria-disabled", "true");
      $("download").removeAttribute("href");
      $("download-note").textContent = "The instance is not published yet. Ask the owner.";
    }
    if (data.manual.length) {
      $("manual").hidden = false;
      const list = $("manual-list");
      for (const mod of data.manual) {
        const label = el("strong", { text: mod.name });
        const line = el("li", {}, [mod.url ? el("a", { href: mod.url, rel: "noopener noreferrer", text: mod.name }) : label,
                                   el("div", { class: "meta" }, [el("span", { text: mod.filename })])]);
        list.append(line);
      }
    }
  } catch {
    $("memory-note").textContent = "Could not load the details; the steps above still apply.";
  }
}

const LABELS = { version: "Pack", neoforge: "NeoForge", minecraft: "Minecraft" };
const day = (iso) => new Date(`${iso}T12:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" });

function tile(kind, count, label) {
  return el("div", { class: `tile tile-${kind}${count ? "" : " tile-zero"}` },
    [el("span", { class: "tile-count", text: String(count) }), el("span", { class: "tile-label", text: label })]);
}

function group(kind, title, mods, row) {
  if (!mods.length) return null;
  const box = el("details", { class: `change-group change-${kind}` });
  if (mods.length <= 60) box.open = true;
  box.append(el("summary", {}, [el("span", { class: "change-mark", "aria-hidden": "true", text: { added: "+", updated: "↑", removed: "−" }[kind] }),
                                el("span", { text: `${title} (${mods.length})` })]));
  box.append(el("ul", { class: "change-list" }, mods.map(row)));
  return box;
}

async function check(since) {
  const box = $("changes");
  box.replaceChildren();
  try {
    const response = await fetch(`/api/pack/changes?since=${encodeURIComponent(since)}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error);
    const notice = $("pack-notice");
    if (!data.changed) {
      notice.hidden = true;
      box.append(el("p", { class: "change-none", text: `Nothing changed since ${day(since)}. You're up to date.` }));
      return;
    }
    notice.hidden = false;
    notice.textContent = `The pack changed since ${day(since)}. Just launch the game: it updates itself first.`;

    box.append(el("div", { class: "tiles" }, [
      tile("added", data.counts.added, "added"),
      tile("updated", data.counts.updated, "updated"),
      tile("removed", data.counts.removed, "removed"),
    ]));

    const bumps = Object.entries(data.pack);
    if (bumps.length) {
      box.append(el("div", { class: "bumps" }, bumps.map(([key, v]) => el("span", { class: "bump" }, [
        el("span", { class: "bump-label", text: LABELS[key] || key }),
        el("span", { class: "ver old", text: v.from || "?" }), el("span", { class: "arrow", "aria-label": "to", text: "→" }),
        el("span", { class: "ver new", text: v.to || "?" }),
      ]))));
    }

    if (data.events.length) {
      box.append(el("ul", { class: "timeline", "aria-label": "Pack updates" }, data.events.map((e) =>
        el("li", {}, [el("time", { datetime: e.on, text: day(e.on) }), el("span", { text: e.title }),
          e.by ? el("span", { class: "meta-inline", text: `by ${e.by}` }) : null]))));
    }

    for (const part of [
      group("added", "Added", data.added, (m) => el("li", {}, [el("strong", { text: m.name }),
        m.by || m.on ? el("span", { class: "meta-inline", text: [m.by && `by ${m.by}`, m.on && day(m.on)].filter(Boolean).join(" · ") }) : null])),
      group("updated", "Updated", data.updated, (m) => el("li", {}, [el("strong", { text: m.name }),
        el("span", { class: "vers" }, [el("span", { class: "ver old", text: m.from }), el("span", { class: "arrow", "aria-label": "to", text: "→" }),
                                       el("span", { class: "ver new", text: m.to })])])),
      group("removed", "Removed", data.removed, (m) => el("li", {}, [el("s", { text: m.name })])),
    ]) if (part) box.append(part);
  } catch (error) {
    box.append(el("p", { class: "problem", text: error.message || "Could not check." }));
  }
}

$("since-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const since = $("since").value;
  if (!since) return;
  remember("last-played", since);
  check(since);
});

details();
// ?since=2026-11-13 opens the check for that day, so "what changed" can be linked in chat.
const asked = new URLSearchParams(location.search).get("since") || "";
const last = /^\d{4}-\d{2}-\d{2}$/.test(asked) ? asked : recall("last-played");
if (/^\d{4}-\d{2}-\d{2}$/.test(last)) {
  $("since").value = last;
  check(last);
} else {
  $("since").value = today();
}
