/** What the app needs from the game server. Real: RCON. Tests: a fake. */
export type Probe =
  | { up: false }
  | { up: true; players: string[]; maxPlayers: number; tps: number | null };

export interface ServerPort {
  /** Never throws: a server that does not answer is `{ up: false }`. */
  probe(): Promise<Probe>;
  /** When the server last started, or null when that cannot be told. */
  lastRestart(): Promise<Date | null>;
}

export type Side = "both" | "client" | "server";

export interface ModEntry {
  name: string;
  file: string;
  version: string;
  side: Side;
  /** Where the jar comes from: modrinth, curseforge or another host. */
  source: string;
  /** Who added it: the base pack, or the git author of the commit. */
  addedBy: string;
  /** ISO date, from the pack's git history. */
  addedOn: string;
  /** True for what v2.9 of All of Create: Aeronautics shipped. */
  basePack: boolean;
}

/** The pack, read from its packwiz repository. */
export interface PackSource {
  mods(): Promise<ModEntry[]>;
}
