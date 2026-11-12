import { connect, type Socket } from "node:net";
import { stat } from "node:fs/promises";
import type { Probe, ServerPort } from "./ports.ts";

export interface RconOptions {
  host: string;
  port: number;
  password: string;
  /** Per command, and for the connect: a server that stalls reads as down. */
  timeoutMs: number;
  /** Where the server's current log is; its creation time is the last restart. */
  latestLog?: string;
}

const AUTH = 3;
const COMMAND = 2;

/** One short-lived RCON session: connect, log in, run commands, close. */
async function session<T>(o: RconOptions, fn: (cmd: (c: string) => Promise<string>) => Promise<T>): Promise<T> {
  const sock: Socket = await new Promise((resolve, reject) => {
    const s = connect({ host: o.host, port: o.port });
    const timer = setTimeout(() => {
      s.destroy();
      reject(new Error("connect timed out"));
    }, o.timeoutMs);
    s.once("connect", () => {
      clearTimeout(timer);
      resolve(s);
    });
    s.once("error", (e) => {
      clearTimeout(timer);
      reject(e);
    });
  });

  let buf = Buffer.alloc(0);
  const waiters: Array<(p: { id: number; type: number; body: string }) => void> = [];
  const queue: Array<{ id: number; type: number; body: string }> = [];
  sock.on("data", (d) => {
    buf = Buffer.concat([buf, d]);
    // A packet is id + type + body + two NULs: 10 bytes plus the body, which
    // Minecraft caps at 4096. Anything else is not RCON, so drop the peer
    // rather than trust a length (a negative one would loop forever).
    while (buf.length >= 4) {
      const size = buf.readInt32LE(0);
      if (size < 10 || size > 4096 + 10) {
        sock.destroy();
        return;
      }
      if (buf.length < size + 4) break;
      const pkt = { id: buf.readInt32LE(4), type: buf.readInt32LE(8), body: buf.subarray(12, 4 + size - 2).toString("utf8") };
      buf = buf.subarray(4 + size);
      const w = waiters.shift();
      if (w) w(pkt);
      else queue.push(pkt);
    }
  });
  sock.on("error", () => {});

  const next = (): Promise<{ id: number; type: number; body: string }> =>
    new Promise((resolve, reject) => {
      const q = queue.shift();
      if (q) return resolve(q);
      const timer = setTimeout(() => {
        const i = waiters.indexOf(w);
        if (i >= 0) waiters.splice(i, 1);
        reject(new Error("rcon timed out"));
      }, o.timeoutMs);
      const w = (p: { id: number; type: number; body: string }) => {
        clearTimeout(timer);
        resolve(p);
      };
      waiters.push(w);
      sock.once("close", () => reject(new Error("rcon closed")));
    });

  const send = (id: number, type: number, body: string) => {
    const b = Buffer.from(body, "utf8");
    const out = Buffer.alloc(14 + b.length);
    out.writeInt32LE(10 + b.length, 0);
    out.writeInt32LE(id, 4);
    out.writeInt32LE(type, 8);
    b.copy(out, 12);
    sock.write(out);
  };

  try {
    send(1, AUTH, o.password);
    // A server may send an empty response packet before the auth reply.
    let reply = await next();
    while (reply.type !== AUTH - 1) reply = await next();
    if (reply.id === -1) throw new Error("rcon password refused");
    let n = 10;
    return await fn(async (c) => {
      const id = ++n;
      send(id, COMMAND, c);
      for (;;) {
        const r = await next();
        if (r.id === id) return r.body;
      }
    });
  } finally {
    sock.destroy();
  }
}

export function parseList(out: string): { players: string[]; maxPlayers: number } | null {
  const m = /There are (\d+) of a max of (\d+) players online:\s*(.*)/s.exec(out);
  if (!m) return null;
  const players = (m[3] ?? "").split(",").map((s) => s.trim()).filter(Boolean);
  return { players, maxPlayers: Number(m[2]) };
}

export function parseTps(out: string): number | null {
  const m = /Overall:.*?Mean TPS:\s*([\d.]+)/s.exec(out);
  return m ? Math.round(Number(m[1]) * 10) / 10 : null;
}

export class RconServer implements ServerPort {
  readonly #o: RconOptions;

  constructor(o: RconOptions) {
    this.#o = o;
  }

  async probe(): Promise<Probe> {
    try {
      return await session(this.#o, async (cmd) => {
        const list = parseList(await cmd("list"));
        if (!list) throw new Error("unexpected `list` output");
        const tps = parseTps(await cmd("neoforge tps").catch(() => ""));
        return { up: true as const, ...list, tps };
      });
    } catch {
      return { up: false };
    }
  }

  async lastRestart(): Promise<Date | null> {
    if (!this.#o.latestLog) return null;
    try {
      const s = await stat(this.#o.latestLog);
      const t = s.birthtimeMs > 0 ? s.birthtime : s.mtime;
      return t;
    } catch {
      return null;
    }
  }
}
