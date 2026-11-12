/**
 * Just enough TOML for packwiz metafiles: `[a.b]` tables, and `key = "string"`
 * or `key = 123` lines. Returned flat, keyed `a.b.key`. Anything else (arrays,
 * multiline strings) is ignored, because a metafile never holds one.
 */
export function parseFlatToml(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  let table = "";
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const t = /^\[([^\[\]]+)\]$/.exec(line);
    if (t) {
      table = `${t[1]!.trim()}.`;
      continue;
    }
    const kv = /^([A-Za-z0-9_-]+)\s*=\s*(.+)$/.exec(line);
    if (!kv) continue;
    const v = kv[2]!.trim();
    const str = /^"((?:[^"\\]|\\.)*)"/.exec(v);
    if (str) out[table + kv[1]] = str[1]!.replace(/\\(["\\])/g, "$1");
    else if (/^-?\d+(\.\d+)?$/.test(v)) out[table + kv[1]] = v;
  }
  return out;
}
