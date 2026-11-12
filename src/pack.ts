import { execFile } from "node:child_process";
import { readdir, readFile } from "node:fs/promises";
import { join } from "node:path";
import { promisify } from "node:util";
import type { ModEntry, PackSource, Side } from "./ports.ts";
import { parseFlatToml } from "./toml.ts";

const run = promisify(execFile);

/** The version part of a jar name: from the first `-1.2`/`_v1.2`/`+1.2` on. */
export function versionOf(file: string): string {
  const base = file.replace(/\.jar(\.disabled)?$/i, "");
  const m = /[-_+ ]v?(\d.*)$/.exec(base);
  return m ? m[1]! : base;
}

function sourceOf(meta: Record<string, string>): string {
  if (meta["download.mode"] === "metadata:curseforge") return "curseforge";
  const url = meta["download.url"];
  if (url) {
    const host = new URL(url).hostname;
    if (host.endsWith("modrinth.com")) return "modrinth";
    if (host.endsWith("forgecdn.net")) return "curseforge";
    return host;
  }
  return "unknown";
}

/** `### \`file.jar\`` headings under "Mods added on top of ...": not base-pack. */
function extrasFromNotes(notes: string): Set<string> {
  const out = new Set<string>();
  const start = notes.search(/^## Mods added on top of/m);
  if (start < 0) return out;
  for (const m of notes.slice(start).matchAll(/^### `([^`]+)`/gm)) out.add(m[1]!);
  return out;
}

/**
 * The pack as a packwiz git repository (on guest 120 it is the repo the
 * server's PACKWIZ_URL reads). A mod is `mods/*.pw.toml`; who added it and
 * when is the commit that added the file. Everything in the very first commit
 * shipped with the base pack, except what NOTES.md lists as added on top.
 */
export class GitPack implements PackSource {
  readonly #dir: string;
  #cache: { head: string; mods: ModEntry[] } | null = null;

  constructor(dir: string) {
    this.#dir = dir;
  }

  async mods(): Promise<ModEntry[]> {
    const git = async (...args: string[]) =>
      (await run("git", ["-C", this.#dir, "-c", "safe.directory=*", ...args], { maxBuffer: 64 * 1024 * 1024 })).stdout;
    let log: string;
    let root: string;
    let head: string;
    try {
      // Every page view lands here, on an Internet-facing page: re-read the
      // history only when the pack has a new commit.
      head = (await git("rev-parse", "HEAD")).trim();
      if (this.#cache?.head === head) return this.#cache.mods;
      log = await git("log", "--diff-filter=A", "--no-renames", "--name-only", "--format=@@%H%x09%an%x09%aI", "--", "mods");
      root = (await git("rev-list", "--max-parents=0", "HEAD")).trim().split("\n")[0] ?? "";
    } catch (err) {
      throw new Error(`${this.#dir} is not a pack repository: ${(err as Error).message}`);
    }

    // Newest first, so the first add seen for a path is its latest one.
    const added = new Map<string, { hash: string; author: string; date: string }>();
    let cur: { hash: string; author: string; date: string } | null = null;
    for (const line of log.split("\n")) {
      if (line.startsWith("@@")) {
        const [hash, author, iso] = line.slice(2).split("\t");
        cur = { hash: hash!, author: author!, date: iso!.slice(0, 10) };
      } else if (line && cur && !added.has(line)) added.set(line, cur);
    }

    const notes = await readFile(join(this.#dir, "NOTES.md"), "utf8").catch(() => "");
    const extras = extrasFromNotes(notes);

    const files = (await readdir(join(this.#dir, "mods"))).filter((f) => f.endsWith(".pw.toml")).sort();
    const out: ModEntry[] = [];
    for (const f of files) {
      const meta = parseFlatToml(await readFile(join(this.#dir, "mods", f), "utf8"));
      const file = meta["filename"] ?? f;
      const origin = added.get(`mods/${f}`);
      const basePack = origin?.hash === root && !extras.has(file);
      const side = meta["side"];
      out.push({
        name: meta["name"] ?? f.replace(/\.pw\.toml$/, ""),
        file,
        version: versionOf(file),
        side: side === "client" || side === "server" ? (side as Side) : "both",
        source: sourceOf(meta),
        addedBy: basePack ? "base pack" : (origin?.author ?? "unknown"),
        addedOn: origin?.date ?? "",
        basePack,
      });
    }
    this.#cache = { head, mods: out };
    return out;
  }
}
