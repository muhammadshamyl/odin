# Table Web — implementation notes (2026-09-02)

Built the new interactive table web + connect-card restyle from this handoff,
per the 10 agreed answers. **Backend pipeline untouched**; the only backend
change is FK-column enrichment (sanctioned). Things noticed along the way, to
revisit later:

## Deviations from the handoff, on purpose (per the answers)

| # | Handoff says | Built | Why |
|---|---|---|---|
| Motion | permanent rAF loop, animated links, 16 traveling dots | static render; bounded rAF only for the layout-switch ease + selection pulse | answer 1 |
| Layout | force-directed sim | **deterministic** — see "Fix 3" below. The hand-rolled physics degenerated on real schemas | pragmatism |
| Layouts | WEB / RING / CLUSTERS | all three (CLUSTERS re-added 2026-09-02 after user feedback); RING sorts by (prefix-group, name), CLUSTERS = a grid per prefix group around a ring | answer 4 → reversed on request |
| Node colour | 6 semantic clusters, colour-coded | **colour-coded by prefix group** (8-colour palette + grey "other"), size by `reltuples`, group-coloured glow on hub/selection/filter-match, coloured legend (re-added 2026-09-02 on request; answer 3 said single accent). Colour is decoupled from layout — it does NOT pull nodes into wells. | answer 3 → reversed on request |
| Scale | tuned for 186/400 | >200 nodes → only degree≥1 shown + "show all (N)" toggle; >600 → default RING; ≤10 → radial, no sim | answer 5 |
| Connect card | artboard 1d adds a table-picker list, target-name field, DATE PARTITION toggle, EXTRACTION STRATEGY tiles | none of those added — cosmetic restyle only | answer 7 / "backend frozen" |
| Stepper | `Connect · Pick table · Preview & types · Create · First run` | `Connect · Pick table · Configure · Preview & types · Create` | answer 8 |

## Backend

- `connectors/rdbms.fk_edges` now returns `{from, to, from_cols, to_cols}` (one
  row per FK constraint, composite-safe). Still in-schema only, still skips
  self-referential FKs. Cross-schema stubs and self-loops deliberately skipped
  (answer 6) — revisit only if users report missing parents.
- New read route `GET /onboard/rdbms/{cid}/{schema}/{table}/panel` →
  `_rdbms_panel.html`. Uses only existing connector fns
  (`columns` / `sample_rows(limit=6)` / `fk_edges` / `list_tables`).
- Removed: `/peek` route + `onboard_rdbms_peek` + `_rdbms_peek.html`;
  `app._cobweb_layout` (server SVG geometry — layout is client-side now);
  the old `.rdbms-web` SVG + `selectWebNode` handlers in `app.js`.
- The panel route re-queries `fk_edges` + `list_tables` per selection. Cheap for
  normal schemas; fold into one call or cache on the session if it ever matters.

## Fixes after first live test (2026-09-02)

User saw "maybe ten tables" on a ~186-table schema and "it looks off":
- **Root cause 1 — the `>200` FK-only default.** Answer 5's declutter hid every
  table without a foreign key by default, with no visible cue. Changed: every
  table renders up to 1500 nodes (`HUGE`); the toggle is now an opt-in
  *"FK-linked only (N)"* declutter, shown whenever `TOTAL > 220`. A live
  **`X / Y` count chip** in the toolbar shows what's rendered vs. total.
- **Root cause 2 — FIT crammed the graph into a sliver.** The stage was 520px
  min-height, the layout normalised to ~940 units wide, and FIT padded 70px, so
  a tall schema fit at k≈0.4 → a 340px knot of 1px dots. Changed: stage
  min-height 600 (card 720), WEB normalise 470→360, RING R 430→360, FIT pad
  70→46, node min zoom-factor 0.6→0.85. A 186-node schema now fits at k≈0.7 and
  fills ~620px with ~25px node spacing.
- **CLUSTERS re-added** as the third layout (grid per prefix group).
- `resize()` retries on the next frame if the canvas measures < 40px (layout
  not settled).
- `web_json` now escapes `<` as `<` (defensive, inline `<script>` JSON).

## Fix 2 — WEB collapsed to a straight line (2026-09-02)

The handoff's `layoutWeb` seeds nodes at per-group **anchor points** on an
ellipse `{cos(2πi/NG)·400, sin(2πi/NG)·340}` and applies strong gravity toward
them. With `NG === 2` those two anchors are colinear (both at y≈0), so every node
collapsed onto the x-axis — a straight line. `NG === 1` also degenerates.

Rewrote `layoutWeb` as a plain force layout, no per-group wells:
- **golden-angle spiral seed** (even, no axis bias) instead of group anchors;
- repulsion `2400/d²` within ~500px, springs `(d−74)·0.045`;
- gentle `α`-scaled centering (`−p·0.012·α`) so it stays framed but relaxes;
- a *weak* same-colour cohesion (`0.010·α` toward the group centroid, only when
  `NG ≥ 3`) so colours form loose neighbourhoods — never a well.

Probed aspect ratio (min/max bbox side): NG=1 → 0.97, NG=2 → 0.89, NG=9 skewed
→ 0.93 — all well-formed 2-D blobs. Group anchors are still used by CLUSTERS and
the RING sort order.

## Fix 3 — dropped the force sim entirely for a deterministic layout (2026-09-02)

The spiral-seed force layout *still* produced a straight line for the user. Two
compounding reasons: (a) a hand-rolled 300-iteration physics sim is inherently
fragile across schema shapes (sparse edges, star topologies, deep chains all
break it differently), and (b) **the browser was serving the cached v1 of
`rdbms_web.js`** — no cache-busting on `/static`.

Both fixed:
- **Cache-busting.** `app._asset_version()` = max mtime of `web/static/**`,
  exposed as the `asset_v` Jinja global; `base.html` + `rdbms_tables.html` now
  load `app.css` / `app.js` / `rdbms_web.js` with `?v={{ asset_v }}`. Changing a
  static file (or restarting the dev server after an edit) busts the cache.
- **Deterministic layout** (`layoutStructural`, no physics). A FK graph is a
  forest of small hierarchies + a pile of unconnected tables:
  1. union-find the FK graph into connected components;
  2. each multi-node component → a **radial tree** from its highest-degree table
     (BFS ranks on tapering concentric arcs; deep chains coil instead of
     spiking);
  3. every FK-less table → **one grid block**;
  4. shelf-pack the component boxes + the grid, recentre on origin.
  RING and CLUSTERS are likewise pure geometry. Probed across STAR (aspect 1.0),
  2-GROUP-SPARSE (0.92), MOSTLY-ISLANDS (0.68), NO-FKS (tidy grid) — no
  degenerate output. Worst case is a boring grid, never a broken line.
- Node colour by prefix group is unchanged (Fix 1); it's cosmetic only and does
  not influence the layout.

## Colour grouping (2026-09-02, after "the greys" feedback)

- **16-hue curated palette** for the biggest groups; every group past that gets a
  deterministic `hslHex(hash(key))`. **No grey "other" bucket** — every group is
  coloured.
- **Grouping strategy auto-picks:**
  - *prefix* (before the first `_`) when it yields real groups — `≥ 2` prefixes
    with `≥ 2` members and `≥ 35 %` coverage;
  - otherwise *FK connected component* — so a schema whose tables share no
    prefixes (`customers`, `orders`, …) still colours by "what relates to what".
    Each multi-table component is a group labelled by its hub (`orders +4`);
    FK-less tables are one legend entry "unlinked", each tinted (low-sat) by name
    so name-families still read.
- Legend shows the 12 biggest groups + "+N more", titled "prefix groups" or
  "linked groups"; hidden when there's `< 2` groups or the canvas is narrow.
- `groupColor` must stay hex — the canvas `rgba()` helper only parses hex, hence
  `hslHex` rather than raw `hsl()` strings.
- 100 distinct categorical colours isn't useful (indistinguishable past ~12), so
  the cap is "curated + hashed", not a 100-entry table.

## Open items / things to address later

1. **Fonts.** Answer 10's premise ("Chakra Petch not loaded") is wrong — `base.html`
   already pulls Chakra Petch + IBM Plex Sans/Mono from `fonts.googleapis.com`,
   and `--display` is `'Chakra Petch'`. So no font work was done; headings use the
   existing `var(--display)`. The "self-host, no external CDN" preference
   conflicts with the app's existing base.html (all three faces via Google Fonts)
   — that's an app-wide change, out of scope for this task. Decide separately.
2. **`show all` on a huge schema** rebuilds the force layout synchronously — a
   deliberate click can freeze ~200–400 ms on a ~600-table schema. Fine for now;
   move to a worker / incremental layout if it bites.
3. **Sample-cell ellipsis** — `.pnl-sample td` is `nowrap` + `max-width` inside an
   `overflow:auto` box, so it scrolls horizontally (per the design) but true
   per-cell ellipsis would need `table-layout:fixed`, which fights the sticky
   header. Left as scroll-only.
4. **Cluster colour dimension is unused.** `n.g` / `GROUPS` carry the prefix
   grouping already; wiring per-group colour back in (if wanted) is a small change
   in `rdbms_web.js` `draw()` + the legend.
5. **≤10-table schemas** keep the WEB/RING toggle in the toolbar (identical
   chrome, per answer 5) but both map to the same radial layout — the toggle is a
   visual no-op there.
6. **Row estimate** stays `pg_class.reltuples` (`GREATEST(...,0)`); a never-analysed
   table shows `0`, not `—`. The panel shows `—` only if the table vanished from
   `list_tables` between graph load and selection.
