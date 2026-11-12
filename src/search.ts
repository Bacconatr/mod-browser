import type { ModEntry } from "./ports.ts";

const squash = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();

/** Every word of the query must appear in the name or file, in any order. */
export function searchMods(mods: ModEntry[], query: string): ModEntry[] {
  const words = squash(query).split(" ").filter(Boolean);
  const sorted = [...mods].sort((a, b) => a.name.localeCompare(b.name));
  if (words.length === 0) return sorted;
  return sorted.filter((m) => {
    const hay = squash(`${m.name} ${m.file}`);
    return words.every((w) => hay.includes(w));
  });
}
