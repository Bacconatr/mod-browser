import { afterEach, beforeEach, describe, it } from "node:test";
import assert from "node:assert/strict";
import type { AddressInfo } from "node:net";
import type { Server } from "node:http";
import { createApp } from "../src/app.ts";
import { FakePack, FakeServer, mod } from "./fakes.ts";

let http: Server;
let base: string;

beforeEach(async () => {
  http = createApp({
    server: new FakeServer(),
    pack: new FakePack([
      mod({ name: "Create: Connected", file: "create_connected-1.3.3-mc1.21.1.jar", version: "1.3.3-mc1.21.1", source: "curseforge" }),
      mod({ name: "Create Deep Seas [BETA]", version: "3.4.5", addedBy: "Bacconatr", addedOn: "2026-10-19", basePack: false }),
      mod({ name: "Create Collision Crashfix", side: "server", basePack: false, addedBy: "Bacconatr", addedOn: "2026-10-19" }),
      mod({ name: "Sodium", side: "client" }),
    ]),
    now: () => new Date("2026-11-12T12:00:00Z"),
    address: "play.example.com",
  });
  await new Promise<void>((r) => http.listen(0, "127.0.0.1", r));
  base = `http://127.0.0.1:${(http.address() as AddressInfo).port}`;
});
afterEach(() => new Promise<void>((r) => http.close(() => r())));

const mods = async (q?: string) =>
  (await fetch(`${base}/api/mods${q === undefined ? "" : `?q=${encodeURIComponent(q)}`}`)).json() as Promise<any>;

describe("GET /api/mods", () => {
  it("lists the whole pack with source, version, side and who added it when", async () => {
    const r = await mods();
    assert.equal(r.total, 4);
    assert.equal(r.mods.length, 4);
    const deep = r.mods.find((m: any) => m.name.startsWith("Create Deep Seas"));
    assert.deepEqual(
      { v: deep.version, s: deep.side, by: deep.addedBy, on: deep.addedOn, base: deep.basePack },
      { v: "3.4.5", s: "both", by: "Bacconatr", on: "2026-10-19", base: false },
    );
  });

  it("finds Create Connected despite the colon, and says it shipped with the base pack", async () => {
    const r = await mods("create connected");
    assert.equal(r.mods.length, 1);
    assert.equal(r.mods[0].name, "Create: Connected");
    assert.equal(r.mods[0].basePack, true);
    assert.equal(r.mods[0].source, "curseforge");
    assert.equal(r.total, 4, "total stays the pack size");
  });

  it("matches every word, in any order, and ignores case", async () => {
    assert.deepEqual((await mods("SEAS deep")).mods.map((m: any) => m.name), ["Create Deep Seas [BETA]"]);
    assert.equal((await mods("create")).mods.length, 3);
  });

  it("returns an empty list, not an error, for no match", async () => {
    const r = await mods("zzzz");
    assert.deepEqual(r.mods, []);
  });

  it("sorts by name", async () => {
    const names = (await mods()).mods.map((m: any) => m.name);
    assert.deepEqual(names, [...names].sort((a, b) => a.localeCompare(b)));
  });
});
