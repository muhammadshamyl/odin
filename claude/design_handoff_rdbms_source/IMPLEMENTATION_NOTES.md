# Table Web — implementation notes (2026-09-02)

Built the new interactive table web + connect-card restyle from this handoff,
per the 10 agreed answers. **Backend pipeline untouched**; the only backend
change is FK-column enrichment (sanctioned). Things noticed along the way, to
revisit later:

## Deviations from the handoff, on purpose (per the answers)

| # | Handoff says | Built | Why |
|---|---|---|---|
| Motion | permanent rAF loop, animated links, 16 traveling dots | one ~300-iter force pass on load (160 for n>400), static render; bounded rAF only for the layout-switch ease + selection pulse | answer 1 |
| Layouts | WEB / RING / CLUSTERS | all three (CLUSTERS re-added 2026-09-02 after user feedback); RING sorts by (prefix-group, name), CLUSTERS = a grid per prefix group around a ring | answer 4 → reversed on request |
| Node colour | 6 semantic clusters, colour-coded | single `--accent`, size by `reltuples`, glow on hub/selection/filter-match; prefix groups drive only the WEB gravity anchors + a **neutral** legend | answer 3 |
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
