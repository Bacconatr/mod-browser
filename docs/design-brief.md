# Surface brief: the public page (`/`)

Development-only. Written while building with the Impeccable design skill
(pbakaus/impeccable, fetched 2026-11-12 at b4bff16, not vendored). Never copy
any of this into `mod_browser/static/`.

**Mode:** Operate. A friend opens it to answer three questions fast: is the
server up, who is on, is a mod already in. Code-led, no comps.

**Audience and scene:** five to ten friends, mostly on a phone, mostly in
the evening right before launching the game, sometimes from a desktop with
the launcher open beside it.

**Constraints:** no login, no third-party requests (CSP `default-src
'none'`), system fonts, works at 360 px wide, every state visible in text
(colour is never the only signal).

## Direction contract

seed: none (unavailable: the engine launcher was not run on this machine;
the mode's rules were read from `reference/mode-operate.md` instead)

- **THESIS:** a status board that answers "can I play" in the first line,
  then gets out of the way of a dense, searchable mod list. It refuses the
  dashboard default of three same-size stat cards over a table.
- **OWN-WORLD:** Create's materials. Andesite casing for the ground (warm
  grey-green), brass for the accent and the lit state, copper for the
  in-between state. One system sans for everything; tabular numerals for
  counts, TPS, versions and the address.
- **FIRST VIEWPORT:** the state band (name, state, address with Copy), then
  one row of vitals: players, TPS, last restart. On a phone the band and
  the player count fit above the fold.
- **SIGNATURE MOVE:** the band's colour carries the state, lit brass when
  online, cold andesite when down, copper while starting; and TPS is drawn
  as twenty tick notches, one per game tick a second, filled to the
  measured rate.
- **STATES:** online, starting, down, status unreachable, empty search,
  pack unreadable. Each has its own words.
- **RISK:** brass on andesite can drift into "steampunk costume". Kept to
  the shell, the state and focus rings; controls stay standard inputs and
  a plain table.
