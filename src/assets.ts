// Served from /style.css and /app.js, so the page's CSP can forbid inline code.
export const CSS = `
:root{color-scheme:light dark;--bg:#f6f5f1;--fg:#1d1f1e;--mute:#5b605d;--rule:#d9d7cf;--panel:#fff;--ok:#1b7a5a;--bad:#a8321f;--accent:#0f6b63}
@media (prefers-color-scheme:dark){:root{--bg:#141716;--fg:#e8eae7;--mute:#9aa29d;--rule:#2b302d;--panel:#1b1f1d;--ok:#4cc79a;--bad:#ef8068;--accent:#58c4b8}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:46rem;margin:0 auto;padding:1.5rem 1rem 4rem}
h1{font-size:1.75rem;line-height:1.15;margin:0;letter-spacing:-.01em}
h2{font-size:1.125rem;margin:2.5rem 0 .5rem}
.sub{margin:.25rem 0 0;color:var(--mute)}
.quiet{color:var(--mute)}
code{font:.95em ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--panel);border:1px solid var(--rule);border-radius:4px;padding:.1em .4em}
#status{margin-top:1.5rem;padding-bottom:1rem;border-bottom:1px solid var(--rule)}
#status .state{display:flex;align-items:center;gap:.5rem;font-size:1.375rem;margin:0 0 .75rem}
.dot{width:.75rem;height:.75rem;border-radius:50%;background:var(--ok);flex:none}
[data-state=down] .dot{background:var(--bad)}
dl{display:flex;flex-wrap:wrap;gap:.5rem 2rem;margin:0}
dl div{min-width:6rem}
dt{font-size:.8125rem;color:var(--mute)}
dd{margin:0;font-weight:600;font-variant-numeric:tabular-nums}
.names{margin:.75rem 0 0}
.address{margin:1rem 0 0}
form label{display:block;font-size:.875rem;color:var(--mute);margin-bottom:.25rem}
.row{display:flex;gap:.5rem}
input[type=search]{flex:1;min-width:0;font:inherit;padding:.6rem .75rem;border:1px solid var(--rule);border-radius:6px;background:var(--panel);color:var(--fg)}
button{font:inherit;font-weight:600;padding:.6rem 1rem;border:1px solid var(--accent);border-radius:6px;background:var(--accent);color:var(--bg);cursor:pointer}
button:hover{filter:brightness(1.1)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
::selection{background:var(--accent);color:var(--bg)}
.count{margin:1rem 0 .25rem;font-size:.875rem;color:var(--mute)}
.mods{list-style:none;margin:0;padding:0}
.mods li{padding:.75rem 0;border-bottom:1px solid var(--rule)}
.ver{color:var(--mute);font-variant-numeric:tabular-nums;font-size:.875rem;overflow-wrap:anywhere}
.meta{display:flex;flex-wrap:wrap;gap:.25rem 1rem;font-size:.875rem;color:var(--mute);margin-top:.125rem}
.base{color:var(--accent)}
@media (min-width:40rem){main{padding-top:3rem}}
`;

export const JS = `
// Keeps the status block fresh without a reload: the page itself is the
// source of truth, so this re-reads / and swaps in its #status section.
(() => {
  const swap = async () => {
    try {
      const html = await (await fetch("/", { cache: "no-store" })).text();
      const next = new DOMParser().parseFromString(html, "text/html").getElementById("status");
      const cur = document.getElementById("status");
      if (next && cur) cur.replaceWith(next);
    } catch { /* offline: keep what is shown */ }
  };
  setInterval(() => { if (!document.hidden) swap(); }, 15000);
})();
`;
