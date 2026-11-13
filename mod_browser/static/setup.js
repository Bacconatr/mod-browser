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

async function check(since) {
  const list = $("changes");
  list.replaceChildren();
  try {
    const response = await fetch(`/api/pack/changes?since=${encodeURIComponent(since)}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error);
    const notice = $("pack-notice");
    if (data.changed) {
      notice.hidden = false;
      notice.textContent = `The pack changed after ${since}. Just launch the game: it updates itself first.`;
      for (const mod of data.added) list.append(el("li", {}, [el("strong", { text: mod.name }), el("div", { class: "meta" }, [el("span", { text: `added ${mod.on}${mod.by ? ` by ${mod.by}` : ""}` })])]));
    } else {
      notice.hidden = true;
      list.append(el("li", { class: "muted", text: `Nothing was added since ${since}.` }));
    }
  } catch (error) {
    list.append(el("li", { class: "problem", text: error.message || "Could not check." }));
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
const last = recall("last-played");
if (/^\d{4}-\d{2}-\d{2}$/.test(last)) {
  $("since").value = last;
  check(last);
} else {
  $("since").value = today();
}
