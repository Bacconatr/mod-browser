import { createServer, type Server, type ServerResponse } from "node:http";
import type { PackSource, ServerPort } from "./ports.ts";
import { CSS, JS } from "./assets.ts";
import { renderPage } from "./render.ts";
import { searchMods } from "./search.ts";
import { StatusService } from "./status.ts";

export interface Deps {
  server: ServerPort;
  pack: PackSource;
  now: () => Date;
  /** The address friends type into Minecraft. */
  address: string;
  statusTtlMs?: number;
}

function json(res: ServerResponse, body: unknown, status = 200): void {
  res.writeHead(status, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" });
  res.end(JSON.stringify(body));
}

const CSP = "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'";

function send(res: ServerResponse, type: string, body: string, status = 200): void {
  res.writeHead(status, {
    "content-type": type,
    "content-security-policy": CSP,
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "cache-control": "no-store",
  });
  res.end(body);
}

export function createApp(deps: Deps): Server {
  const status = new StatusService(deps.server, deps.now, deps.address, deps.statusTtlMs ?? 5_000);

  return createServer(async (req, res) => {
    const url = new URL(req.url ?? "/", "http://localhost");
    try {
      if (req.method === "GET" && url.pathname === "/api/health") return json(res, { ok: true });
      if (req.method === "GET" && url.pathname === "/api/status") return json(res, await status.get());
      if (req.method === "GET" && url.pathname === "/api/mods") {
        let all;
        try {
          all = await deps.pack.mods();
        } catch (err) {
          console.error("pack unreadable:", err);
          return json(res, { error: "the pack is unavailable" }, 503);
        }
        return json(res, { total: all.length, mods: searchMods(all, url.searchParams.get("q") ?? "") });
      }
      if (req.method === "GET" && url.pathname === "/style.css") return send(res, "text/css; charset=utf-8", CSS);
      if (req.method === "GET" && url.pathname === "/app.js") return send(res, "text/javascript; charset=utf-8", JS);
      if (req.method === "GET" && url.pathname === "/") {
        const q = (url.searchParams.get("q") ?? "").slice(0, 100);
        const [st, packed] = await Promise.all([
          status.get(),
          deps.pack.mods().then(
            (all) => ({ all, error: false }),
            (err) => {
              console.error("pack unreadable:", err);
              return { all: [], error: true };
            },
          ),
        ]);
        const mods = searchMods(packed.all, q);
        return send(res, "text/html; charset=utf-8", renderPage({ status: st, mods, total: packed.all.length, q, packError: packed.error }));
      }
      json(res, { error: "not found" }, 404);
    } catch (err) {
      console.error(err);
      json(res, { error: "internal error" }, 500);
    }
  });
}
