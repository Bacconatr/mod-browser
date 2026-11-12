import { afterEach, beforeEach, describe, it } from "node:test";
import assert from "node:assert/strict";
import type { AddressInfo } from "node:net";
import type { Server } from "node:http";
import { createApp } from "../src/app.ts";
import { FakePack, FakeServer } from "./fakes.ts";

let http: Server;
let base: string;
let server: FakeServer;
let clock: Date;

beforeEach(async () => {
  server = new FakeServer();
  clock = new Date("2026-11-12T12:00:00Z");
  http = createApp({
    server,
    pack: new FakePack(),
    now: () => clock,
    address: "play.example.com",
    statusTtlMs: 5_000,
  });
  await new Promise<void>((r) => http.listen(0, "127.0.0.1", r));
  base = `http://127.0.0.1:${(http.address() as AddressInfo).port}`;
});
afterEach(() => new Promise<void>((r) => http.close(() => r())));

const status = async () => (await fetch(`${base}/api/status`)).json() as Promise<any>;

describe("GET /api/status", () => {
  it("reports an up server with who is online, TPS and the address", async () => {
    server.state = { up: true, players: ["Fake_Friend", "Test_Player"], maxPlayers: 10, tps: 19.7 };
    const s = await status();
    assert.equal(s.state, "online");
    assert.deepEqual(s.players, { online: 2, max: 10, names: ["Fake_Friend", "Test_Player"] });
    assert.equal(s.tps, 19.7);
    assert.equal(s.address, "play.example.com");
    assert.equal(s.lastRestart, "2026-11-12T09:00:00.000Z");
  });

  it("says the server is down when it does not answer", async () => {
    server.state = { up: false };
    const s = await status();
    assert.equal(s.state, "down");
    assert.deepEqual(s.players, { online: 0, max: null, names: [] });
    assert.equal(s.tps, null);
  });

  it("notices a stop within the cache window, not later", async () => {
    assert.equal((await status()).state, "online");
    server.state = { up: false };
    // Inside the window the last reading is served, so a burst of visitors
    // costs one RCON round trip.
    assert.equal((await status()).state, "online");
    assert.equal(server.probes, 1);
    clock = new Date(clock.getTime() + 6_000);
    assert.equal((await status()).state, "down");
  });
});
