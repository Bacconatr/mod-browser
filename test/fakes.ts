import type { ModEntry, PackSource, Probe, ServerPort } from "../src/ports.ts";

/** A Server port the test controls: up or down, who is on, when it restarted. */
export class FakeServer implements ServerPort {
  state: Probe = { up: true, players: [], maxPlayers: 10, tps: 20 };
  restarted: Date | null = new Date("2026-11-12T09:00:00Z");
  probes = 0;

  async probe(): Promise<Probe> {
    this.probes++;
    return this.state;
  }
  async lastRestart(): Promise<Date | null> {
    return this.restarted;
  }
}

export class FakePack implements PackSource {
  entries: ModEntry[];
  constructor(entries: ModEntry[] = []) {
    this.entries = entries;
  }
  async mods(): Promise<ModEntry[]> {
    return this.entries;
  }
}

export function mod(over: Partial<ModEntry> & { name: string }): ModEntry {
  return {
    file: `${over.name.toLowerCase().replace(/\W+/g, "-")}-1.0.0.jar`,
    version: "1.0.0",
    side: "both",
    source: "modrinth",
    addedBy: "base pack",
    addedOn: "2026-11-12",
    basePack: true,
    ...over,
  };
}
