# Table Web — implementation notes (2026-09-02)

Built the new interactive table web + connect-card restyle from this handoff,
per the 10 agreed answers. **Backend pipeline untouched**; the only backend
change is FK-column enrichment (sanctioned). Things noticed along the way, to
revisit later:

## Deviations from the handoff, on purpose (per the answers)

| # | Handoff says | Built | Why |
|---|---|---|---|
| Motion | permanent rAF loop, animated links, 16 traveling dots | one ~300-iter force pass on load (160 for n>400), static render; bounded rAF only for the layout-switch ease + selection pulse | answer 1 |
| Layouts | WEB / RING / CLUSTERS | WEB + RING only; RING sorts by (prefix-group, name) so groups stay adjacent | answer 4 |
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
