import { readFileSync } from "node:fs";
import { createApp } from "./app.ts";
import { GitPack } from "./pack.ts";
import { RconServer } from "./rcon-server.ts";

function env(name: string, fallback?: string): string {
  const v = process.env[name] ?? fallback;
  if (v === undefined || v === "") throw new Error(`${name} is required`);
  return v;
}

const password = readFileSync(env("RCON_PASSWORD_FILE"), "utf8").trim();

const app = createApp({
  server: new RconServer({
    host: env("RCON_HOST", "mc"),
    port: Number(env("RCON_PORT", "25575")),
    password,
    timeoutMs: 3_000,
    latestLog: process.env["LATEST_LOG"] || undefined,
  }),
  pack: new GitPack(env("PACK_DIR", "/pack")),
  now: () => new Date(),
  address: env("SERVER_ADDRESS", "play.example.com"),
});

const port = Number(env("PORT", "3000"));
app.listen(port, env("HOST", "0.0.0.0"), () => console.log(`mod-browser listening on ${port}`));
for (const sig of ["SIGTERM", "SIGINT"] as const) process.on(sig, () => app.close(() => process.exit(0)));
