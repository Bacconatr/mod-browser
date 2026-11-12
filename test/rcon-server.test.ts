import { afterEach, describe, it } from "node:test";
import assert from "node:assert/strict";
import { createServer, type Server, type Socket } from "node:net";
import type { AddressInfo } from "node:net";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { RconServer } from "../src/rcon-server.ts";

/** A minimal Source RCON server: password check, then canned command output. */
function fakeRcon(opts: { password: string; replies: Record<string, string> }): Promise<{ srv: Server; port: number; seen: string[] }> {
  const seen: string[] = [];
  const srv = createServer((sock: Socket) => {
    let buf = Buffer.alloc(0);
    sock.on("data", (d) => {
      buf = Buffer.concat([buf, d]);
      while (buf.length >= 4 && buf.length >= buf.readInt32LE(0) + 4) {
        const size = buf.readInt32LE(0);
        const id = buf.readInt32LE(4);
        const type = buf.readInt32LE(8);
        const body = buf.subarray(12, 4 + size - 2).toString("utf8");
        buf = buf.subarray(4 + size);
        const send = (rid: number, rtype: number, text: string) => {
          const b = Buffer.from(text, "utf8");
          const out = Buffer.alloc(14 + b.length);
          out.writeInt32LE(10 + b.length, 0);
          out.writeInt32LE(rid, 4);
          out.writeInt32LE(rtype, 8);
          b.copy(out, 12);
          sock.write(out);
        };
        if (type === 3) {
          // Minecraft answers once: the request's id, or -1 for a bad password.
          send(body === opts.password ? id : -1, 2, "");
        } else if (type === 2) {
          seen.push(body);
          send(id, 0, opts.replies[body] ?? "Unknown command");
        }
      }
    });
  });
  return new Promise((r) => srv.listen(0, "127.0.0.1", () => r({ srv, port: (srv.address() as AddressInfo).port, seen })));
}

let closers: Array<() => void> = [];
afterEach(() => {
  closers.forEach((c) => c());
  closers = [];
});

async function setup(replies: Record<string, string>, password = "s3cret") {
  const f = await fakeRcon({ password, replies });
  closers.push(() => f.srv.close());
  return f;
}

describe("RconServer.probe()", () => {
  it("reads who is online and the overall TPS", async () => {
    const f = await setup({
      list: "There are 2 of a max of 10 players online: Fake_Friend, Test_Player",
      "neoforge tps": "Dim minecraft:overworld (minecraft:overworld): Mean tick time: 4.100 ms. Mean TPS: 20.000\nOverall: Mean tick time: 50.510 ms. Mean TPS: 19.800",
    });
    const rcon = new RconServer({ host: "127.0.0.1", port: f.port, password: "s3cret", timeoutMs: 1000 });
    assert.deepEqual(await rcon.probe(), { up: true, players: ["Fake_Friend", "Test_Player"], maxPlayers: 10, tps: 19.8 });
  });

  it("reads an empty server", async () => {
    const f = await setup({ list: "There are 0 of a max of 10 players online: ", "neoforge tps": "Overall: Mean tick time: 3.0 ms. Mean TPS: 20.000" });
    const rcon = new RconServer({ host: "127.0.0.1", port: f.port, password: "s3cret", timeoutMs: 1000 });
    assert.deepEqual(await rcon.probe(), { up: true, players: [], maxPlayers: 10, tps: 20 });
  });

  it("is up with unknown TPS when the TPS command is not understood", async () => {
    const f = await setup({ list: "There are 1 of a max of 10 players online: Solo" });
    const rcon = new RconServer({ host: "127.0.0.1", port: f.port, password: "s3cret", timeoutMs: 1000 });
    assert.deepEqual(await rcon.probe(), { up: true, players: ["Solo"], maxPlayers: 10, tps: null });
  });

  it("is down when nothing is listening", async () => {
    const f = await setup({});
    const port = f.port;
    f.srv.close();
    const rcon = new RconServer({ host: "127.0.0.1", port, password: "s3cret", timeoutMs: 500 });
    assert.deepEqual(await rcon.probe(), { up: false });
  });

  it("is down, not hung, when the server accepts but never answers", async () => {
    const srv = createServer(() => {});
    await new Promise<void>((r) => srv.listen(0, "127.0.0.1", r));
    closers.push(() => srv.close());
    const rcon = new RconServer({ host: "127.0.0.1", port: (srv.address() as AddressInfo).port, password: "x", timeoutMs: 300 });
    const t = Date.now();
    assert.deepEqual(await rcon.probe(), { up: false });
    assert.ok(Date.now() - t < 2000);
  });

  it("is down, not stuck in a loop, when the peer sends a nonsense packet length", async () => {
    for (const size of [-4, 0, 9, 1_000_000_000]) {
      const srv = createServer((sock) => {
        const bad = Buffer.alloc(12);
        bad.writeInt32LE(size, 0);
        sock.write(bad);
      });
      await new Promise<void>((r) => srv.listen(0, "127.0.0.1", r));
      closers.push(() => srv.close());
      const rcon = new RconServer({ host: "127.0.0.1", port: (srv.address() as AddressInfo).port, password: "x", timeoutMs: 300 });
      assert.deepEqual(await rcon.probe(), { up: false }, `size ${size}`);
    }
  });

  it("is down when the password is refused", async () => {
    const f = await setup({ list: "There are 0 of a max of 10 players online: " });
    const rcon = new RconServer({ host: "127.0.0.1", port: f.port, password: "wrong", timeoutMs: 500 });
    assert.deepEqual(await rcon.probe(), { up: false });
  });
});

describe("RconServer.lastRestart()", () => {
  it("is when the server's latest.log was created", async () => {
    const logs = mkdtempSync(join(tmpdir(), "logs-"));
    try {
      writeFileSync(join(logs, "latest.log"), "x");
      const rcon = new RconServer({ host: "127.0.0.1", port: 1, password: "x", timeoutMs: 100, latestLog: join(logs, "latest.log") });
      const t = await rcon.lastRestart();
      assert.ok(t && Math.abs(t.getTime() - Date.now()) < 60_000);
    } finally {
      rmSync(logs, { recursive: true, force: true });
    }
  });

  it("is null when the log is not there", async () => {
    const rcon = new RconServer({ host: "127.0.0.1", port: 1, password: "x", timeoutMs: 100, latestLog: "/nonexistent/latest.log" });
    assert.equal(await rcon.lastRestart(), null);
  });
});
