import type { ServerPort } from "./ports.ts";

export interface Status {
  state: "online" | "down";
  players: { online: number; max: number | null; names: string[] };
  tps: number | null;
  lastRestart: string | null;
  address: string;
  checkedAt: string;
}

/**
 * Reads the server at most once per `ttlMs`, however many visitors ask. The
 * window is also how late a stop can be noticed, so it stays well under the
 * minute the status page promises.
 */
export class StatusService {
  #cached: { at: number; value: Status } | null = null;
  #inflight: Promise<Status> | null = null;

  readonly #server: ServerPort;
  readonly #now: () => Date;
  readonly #address: string;
  readonly #ttlMs: number;

  constructor(server: ServerPort, now: () => Date, address: string, ttlMs: number) {
    this.#server = server;
    this.#now = now;
    this.#address = address;
    this.#ttlMs = ttlMs;
  }

  async get(): Promise<Status> {
    const t = this.#now().getTime();
    if (this.#cached && t - this.#cached.at < this.#ttlMs) return this.#cached.value;
    this.#inflight ??= this.#read().finally(() => {
      this.#inflight = null;
    });
    return this.#inflight;
  }

  async #read(): Promise<Status> {
    const at = this.#now();
    const probe = await this.#server.probe();
    const lastRestart = probe.up ? await this.#server.lastRestart().catch(() => null) : null;
    const value: Status = probe.up
      ? {
          state: "online",
          players: { online: probe.players.length, max: probe.maxPlayers, names: probe.players },
          tps: probe.tps,
          lastRestart: lastRestart?.toISOString() ?? null,
          address: this.#address,
          checkedAt: at.toISOString(),
        }
      : {
          state: "down",
          players: { online: 0, max: null, names: [] },
          tps: null,
          lastRestart: null,
          address: this.#address,
          checkedAt: at.toISOString(),
        };
    this.#cached = { at: at.getTime(), value };
    return value;
  }
}
