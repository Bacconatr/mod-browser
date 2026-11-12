import { afterEach, beforeEach, describe, it } from "node:test";
import assert from "node:assert/strict";
import type { AddressInfo } from "node:net";
import type { Server } from "node:http";
import { createApp } from "../src/app.ts";
import { FakePack, FakeServer, mod } from "./fakes.ts";

let http: Server;
let base: string;
let server: FakeServer;
let pack: FakePack;

beforeEach(async () => {
  server = new FakeServer();
  pack = new FakePack([
    mod({ name: "Create: Connected", source: "curseforge", version: "1.3.3" }),
    mod({ name: "Create Deep Seas", basePack: false, addedBy: "Test_Player", addedOn: "2026-11-20", version: "3.4.5" }),
    mod({ name: "<img src=x onerror=alert(1)>" }),
  ]);
  http = createApp({
    server,
    pack,
    now: () => new Date("2026-11-12T12:00:00Z"),
    address: "play.example.com",
    statusTtlMs: 0,
  });
  await new Promise<void>((r) => http.listen(0, "127.0.0.1", r));
  base = `http://127.0.0.1:${(http.address() as AddressInfo).port}`;
});
afterEach(() => new Promise<void>((r) => http.close(() => r())));

const page = async (path = "/") => {
  const res = await fetch(`${base}${path}`);
  return { res, html: await res.text() };
};

describe("GET /", () => {
  it("shows an online server with who is on, TPS and the address to type", async () => {
    server.state = { up: true, players: ["Fake_Friend"], maxPlayers: 10, tps: 19.9 };
    const { res, html } = await page();
    assert.equal(res.status, 200);
    assert.match(res.headers.get("content-type")!, /text\/html/);
    assert.match(html, /Online/);
    assert.match(html, /Fake_Friend/);
    assert.match(html, /19\.9/);
    assert.match(html, /1 of 10/);
    assert.match(html, /play\.example\.com/);
  });

  it("says plainly that the server is down or restarting", async () => {
    server.state = { up: false };
    const { html } = await page();
    assert.match(html, /Down or restarting/);
    assert.doesNotMatch(html, /TPS/);
  });

  it("finds a mod without any script: ?q= filters the server-rendered list", async () => {
    const { html } = await page("/?q=create+connected");
    assert.match(html, /Create: Connected/);
    assert.match(html, /Shipped with the base pack/);
    assert.doesNotMatch(html, /Create Deep Seas/);
  });

  it("shows who added a later mod and when", async () => {
    const { html } = await page("/?q=deep+seas");
    assert.match(html, /Test_Player/);
    assert.match(html, /2026-11-20/);
    assert.doesNotMatch(html, /Shipped with the base pack/);
  });

  it("escapes a mod name, so a metafile cannot inject markup", async () => {
    const { html } = await page();
    assert.doesNotMatch(html, /<img src=x/);
    assert.match(html, /&lt;img src=x/);
  });

  it("says when the pack cannot be read, and still shows the server's state", async () => {
    pack.mods = async () => {
      throw new Error("not a pack repository");
    };
    const { res, html } = await page();
    assert.equal(res.status, 200);
    assert.match(html, /mod list is unavailable/i);
    assert.match(html, /Online/);
  });
});

describe("GET /api/mods when the pack is unreadable", () => {
  it("answers 503, not an empty list", async () => {
    pack.mods = async () => {
      throw new Error("boom");
    };
    const res = await fetch(`${base}/api/mods`);
    assert.equal(res.status, 503);
  });
});
