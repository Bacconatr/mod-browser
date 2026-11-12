import { afterEach, beforeEach, describe, it } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { cpSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { GitPack } from "../src/pack.ts";

const fixtures = join(import.meta.dirname, "fixtures");
let dir: string;

function git(...args: string[]) {
  return execFileSync("git", ["-C", dir, ...args], { encoding: "utf8" });
}
function commit(msg: string, author: string, date: string) {
  git("add", "-A");
  execFileSync("git", ["-C", dir, "commit", "-q", "-m", msg, "--author", `${author} <a@example.com>`, "--date", date], {
    env: { ...process.env, GIT_COMMITTER_NAME: author, GIT_COMMITTER_EMAIL: "a@example.com", GIT_COMMITTER_DATE: date },
  });
}

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "pack-"));
  git("init", "-q", "-b", "main");
  mkdirSync(join(dir, "mods"));
  for (const f of ["create-connected", "create-deep-seas", "kaleidoscope-cookery"]) {
    cpSync(join(fixtures, `${f}.pw.toml`), join(dir, "mods", `${f}.pw.toml`));
  }
  // pack.toml and a resource pack are not mods and must not be listed.
  writeFileSync(join(dir, "pack.toml"), 'name = "t"\n');
  mkdirSync(join(dir, "resourcepacks"));
  writeFileSync(join(dir, "resourcepacks", "pretty.pw.toml"), 'name = "Pretty"\nfilename = "p.zip"\nside = "client"\n');
  writeFileSync(
    join(dir, "NOTES.md"),
    "# notes\n\n## Mods added on top of v2.9\n\n### `create_submarine-3.4.5.jar`\n\nside `both`, `mods/create-deep-seas.pw.toml`\n\nDeep Seas.\n",
  );
  commit("pack v2.9 plus extras", "Bacconatr", "2026-11-12T10:00:00Z");
  cpSync(join(fixtures, "functional-storage.pw.toml"), join(dir, "mods", "functional-storage.pw.toml"));
  commit("Add Functional Storage for Test_Player", "Test_Player", "2026-11-20T18:30:00Z");
});
afterEach(() => rmSync(dir, { recursive: true, force: true }));

describe("GitPack.mods()", () => {
  it("lists only what is under mods/", async () => {
    const names = (await new GitPack(dir).mods()).map((m) => m.name).sort();
    assert.deepEqual(names, ["Create Deep Seas [BETA]", "Create: Connected", "Functional Storage", "Kaleidoscope Cookery"]);
  });

  it("reads name, file, version, side and source from the metafile", async () => {
    const cc = (await new GitPack(dir).mods()).find((m) => m.name === "Create: Connected")!;
    assert.equal(cc.file, "create_connected-1.3.3-mc1.21.1.jar");
    assert.equal(cc.version, "1.3.3-mc1.21.1");
    assert.equal(cc.side, "both");
    assert.equal(cc.source, "curseforge");
    const ds = (await new GitPack(dir).mods()).find((m) => m.name.startsWith("Create Deep"))!;
    assert.equal(ds.source, "modrinth");
    assert.equal(ds.version, "3.4.5");
    const fs = (await new GitPack(dir).mods()).find((m) => m.name === "Functional Storage")!;
    assert.equal(fs.source, "curseforge", "a forgecdn URL is CurseForge");
    assert.equal(fs.version, "1.21.1-1.5.8");
  });

  it("says a mod from the first commit shipped with the base pack, unless NOTES.md lists it as an extra", async () => {
    const mods = await new GitPack(dir).mods();
    const cc = mods.find((m) => m.name === "Create: Connected")!;
    assert.deepEqual([cc.basePack, cc.addedBy, cc.addedOn], [true, "base pack", "2026-11-12"]);
    const ds = mods.find((m) => m.name.startsWith("Create Deep"))!;
    assert.deepEqual([ds.basePack, ds.addedBy, ds.addedOn], [false, "Bacconatr", "2026-11-12"]);
  });

  it("takes added by and added on from the commit that added the file", async () => {
    const fs = (await new GitPack(dir).mods()).find((m) => m.name === "Functional Storage")!;
    assert.deepEqual([fs.basePack, fs.addedBy, fs.addedOn], [false, "Test_Player", "2026-11-20"]);
  });

  it("picks up a new commit, and does not re-read the history while HEAD is unchanged", async () => {
    const pack = new GitPack(dir);
    assert.equal((await pack.mods()).length, 4);
    cpSync(join(fixtures, "create-connected.pw.toml"), join(dir, "mods", "another.pw.toml"));
    // Uncommitted: HEAD has not moved, so the pack as served has not changed.
    assert.equal((await pack.mods()).length, 4);
    commit("Add another", "Test_Player", "2026-11-21T10:00:00Z");
    assert.equal((await pack.mods()).length, 5);
  });

  it("fails loudly, not with an empty list, when the directory is not a pack repository", async () => {
    const empty = mkdtempSync(join(tmpdir(), "nopack-"));
    try {
      await assert.rejects(new GitPack(empty).mods(), /not a pack repository/);
    } finally {
      rmSync(empty, { recursive: true, force: true });
    }
  });
});
