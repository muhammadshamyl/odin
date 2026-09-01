# Handoff: RDBMS source onboarding (Odin)

## Overview
Adds a second source type to Odin's onboarding wizard (`/onboard`): **connect to a relational database** alongside the existing CSV/TXT file upload. Step 1 reflows into two columns (File | Relational database). The DB path adds a connection form, a schema/table exploration screen rendered as an interactive **table web** (force-directed graph of tables + foreign keys), a target-table + extraction-strategy config, and an optional **date partition** option — then hands off to the *existing* step-2 "Preview & configure" screen unchanged.

## About the design files
The two `.dc.html` files in this bundle are **design references written in HTML/Canvas** — prototypes that show intended look and behavior. They are not production code to copy. Recreate them in the target codebase's existing environment (the app appears to be Django-served at `127.0.0.1:8000`; use whatever the front end already is — templates + vanilla JS, React, etc.) using its established patterns. `support.js` is only the preview runtime for the prototypes; do not port it.

Open either file directly in a browser to interact with it.

- `Onboard RDBMS Source.dc.html` — static artboards for every state of the connect card (pan/zoom canvas, artboards `1a`–`1f`).
- `Table Web.dc.html` — a **working, interactive** prototype of the table-web screen (zoom/pan, layouts, selection, preview panel). This is the reference implementation for the graph behavior.

## Fidelity
**High-fidelity.** Final colors, typography, spacing and interactions. Recreate pixel-close using the app's existing CSS variables (the token list below matches what the prototypes hard-code).

---

## Screens / views

### 1. Step 1 — Select source (artboard `1a`)
**Purpose:** user chooses whether the source is a file or a database.

**Layout:** app shell (246px left sidebar, 1px `--border` right edge; top breadcrumb bar 14px/26px padding). Content area max ~1200px, padding `34px 26px 40px`.
- Section header: display numeral "02" (Chakra Petch 700, 44px, color `--panel-2`-ish `#12202c`), eyebrow `— MODULE 1 · 3 · 8 · 10.1` (IBM Plex Mono 500 10px, letter-spacing .14em, `--accent`), H1 "Onboard Source" (Chakra Petch 700 34px), sub text (Plex Sans 13.5px/1.6, `--text-soft`, max 640px).
- Stepper: single bordered row, `border 1px --border`, radius 6px, cells separated by 1px rules, active cell `background #12202c`; step badge is an 18px circle (active = solid `--accent` on `#062230` ink; inactive = 1px `--border`, `--text-faint`). Labels Plex Mono 500 10-11px, .12em tracking.
  - File path: `Upload · Preview & load type · Create · First run`
  - DB path (after connect): `Connect · Pick table · Preview & types · Create · First run`
- Body: `display:grid; grid-template-columns:1fr 1fr; gap:22px; align-items:start`.

**Cards (both):** `background --panel; border 1px --border; radius 6px; padding 22px 24px 24px;` no shadow. Corner tick = two 1px `--accent` marks (14×1 and 1×14) at the top-left corner.
- Left card: title "File (CSV / TXT)" + pill `ONE-SHOT` (info tint). Helper copy, label `FILE`, dashed drop zone (`1px dashed --border`, `--panel-2` fill, radius 6px, 30px/18px padding, centered mono copy `Drop a file here, or browse` + `.csv · .txt — up to 2 GB`), primary button `UPLOAD & PREVIEW`.
- Right card: title "Relational database" + pill `RECURRING` (ok tint). The connection form (below).

### 2. RDBMS card — Idle / form (artboard `1b`)
Width 560px standalone. Fields in a `1fr 1fr` grid, `gap 16px 18px`:

| Field | Control | Constraint |
|---|---|---|
| Engine | segmented (PostgreSQL / MySQL), full-width row | drives port default + introspection query; switching resets PORT if untouched (5432 / 3306) |
| Host | text | required, hostname or IPv4, trimmed |
| Port | number | integer 1–65535, default per engine |
| Database | text | required |
| Schema | text | default `public` (PG) / database name (MySQL); lowercase identifier, unquoted |
| Username | text | required |
| Password | password | write-only — posted to secret store on test, never echoed. Helper: "Stored in the secret store, never shown again." |
| SSL | toggle (disable / require) | default require |

- Field label: Plex Mono 500 10px, `.12em`, `--text-faint`, uppercase, 7-8px below-margin.
- Input: `background --panel-2; border 1px --border; radius 4px; padding 10px 12px; font 13px Plex Mono; color --text`. Focus: `border-color --accent; box-shadow 0 0 0 3px rgba(70,199,240,.14)`.
- Toggle: 40×21 pill, on = `rgba(70,199,240,.2)` fill + `--accent` border + 15px accent knob.
- Buttons: primary `TEST CONNECTION` (solid `--accent`, ink `#062230`, radius 4px, padding 11px 18px, Plex Mono 600 11px/.12em); secondary `CANCEL` (transparent, 1px `--border`, `--text-soft`).
- `TEST CONNECTION` disabled until host, database, username, password non-empty.

### 3. Testing (artboard `1c`)
Form wrapper `opacity .45; pointer-events:none`. Header pill switches to `TESTING` (warn tint). Primary button becomes outline-accent with a 12px spinner (2px ring, `--accent` top border, 0.8s linear spin) and label `TESTING…`. Meta text: "handshake · 10 s timeout".

### 4. Connected (artboard `1d`)
Card width 620px, sections separated by 1px `#141d29` rules.
1. **Stepper** switched to the 5-step DB path, step 1 done (green ✓ badge), step 2 active.
2. **Success strip:** `background rgba(63,208,139,.09); border 1px rgba(63,208,139,.35); border-left 2px --ok; radius 4px; padding 10px 14px`. Left: `✔ Connected — PostgreSQL 16.2`. Right meta: `warehouse · public · 41 tables · 128 ms`.
3. **Table picker:** label `SOURCE TABLE` + count meta; filter input (`filter schema.table`); list box `1px --border`, `--panel-2`, max-height 214px, scrollable. Row grid `1fr 110px 92px`: name, ≈ rows (right-aligned, space-grouped digits), cols. Selected row: `background rgba(70,199,240,.1); box-shadow inset 2px 0 0 --accent`. Footnote: row counts are planner estimates (`pg_class.reltuples`).
4. **Target table name:** text input, helper "The physical name Odin creates — staging.&lt;name&gt; / production.&lt;name&gt;". Constraint: lowercase `[a-z0-9_]`, ≤63 chars, defaults to source table name, unique within staging.
5. **Date partition (optional, default OFF):** toggle row with label `DATE PARTITION` + `OPTIONAL` pill (info tint) and sub-copy "Partition the target tables by date for faster traversal." When on, a sub-panel (`border-left 2px --accent`) reveals:
   - `PARTITION COLUMN` select — list filtered to `date` / `timestamp` / `timestamptz` columns; pre-selects the window column when strategy is TIME_WINDOW.
   - `PARTITION GRAIN` select — Daily · Monthly · Yearly; must be coarser than or equal to the window grain.
   - Note: Odin creates `production.<name>` RANGE-partitioned on the column, one partition per grain unit, provisioned ahead of the current window. NULL partition keys are quarantined. Off = single unpartitioned table.
6. **Extraction strategy:** three option tiles in a `1fr 1fr 1fr` grid; selected tile = `1px --accent` + `rgba(70,199,240,.1)` fill.
   - `CURSOR` → reveals `CURSOR COLUMN` select. Candidates are monotonic columns only (e.g. `id (bigint)`, `updated_at`). NULL cursor rows → quarantine.
   - `TIME_WINDOW` → reveals `WINDOW COLUMN` (select), `WINDOW GRAIN` (Hourly · Daily · Weekly · Monthly), `SETTLING LAG (MIN)` (integer 0–1440). Windows pull only once `now − lag` passes the upper bound; late arrivals go to the waiting pipeline.
   - `FULL` → no extra fields; note "small reference tables — pulled whole each run"; warn above ≈5M estimated rows.
   Sub-form container: `--panel-2` fill, `1px --border`, `border-left 2px --accent`, radius 4px, padding 16px.
7. **Actions:** primary `CONTINUE → PREVIEW & CONFIGURE`, secondary `CHANGE CONNECTION`.

**Hand-off:** `CONTINUE` runs a `LIMIT 200` sample against the selected table and enters the **existing step-2 screen unchanged** (sample rows, per-column production types, load type, existence column / composite natural key, recurrence, owner). Step 2 receives `{connection_id, schema, table, target_name, strategy, strategy_params, partition: {enabled, column, grain} | null}` in place of the file path's `{upload_id}`. Nothing recurring is committed until step 3.

### 5. Error (artboard `1f`)
Header pill `FAILED` (danger tint), corner tick turns `--danger`. Inline strip: `rgba(255,111,107,.08)` fill, `1px rgba(255,111,107,.4)`, `border-left 2px --danger`; title `✕ Connection failed — 4.2 s`; body = verbatim driver text, truncated at 400 chars (e.g. `FATAL: password authentication failed for user "odin_reader" (SQLSTATE 28P01)`); note that the password field was cleared and nothing was written to the secret store. Offending fields get `--danger` borders (password also gets the danger glow ring). Form stays editable.

### 6. Table web — Pick table (`Table Web.dc.html`)
**Purpose:** explore all tables in the chosen schema as a graph of FK relationships, pick one, preview it, continue.

**Layout:** app shell + header block, then a wrapping flex row, `gap 18px`, padding `0 26px 26px`:
- Graph card: `flex:5 1 560px; min-width:0; min-height:660px; overflow:hidden`.
- Preview card: `flex:1 1 320px; max-width:560px`. Below ~1100px total width the Preview card wraps under the graph (never let the canvas go below ~560px).

**Graph card toolbar** (`padding 14px 16px`, bottom rule `#141d29`): "186 tables" (Chakra Petch 600 14px) · "400 FOREIGN-KEY LINKS" (mono meta) · filter input (flex) · segmented layout switch `WEB | RING | CLUSTERS`.

**Canvas** fills the rest; devicePixelRatio-aware, `ResizeObserver` re-fits on resize.
- Background: `--panel-2` fill + radial accent glow (`rgba(70,199,240,.07)` → transparent) + a pan/zoom-locked 44px grid at `rgba(31,46,62,.42)`.
- Nodes: radius `clamp(2.1, 1.6 + log10(rows+10)*0.72 + degree*0.14, 9)` scaled by zoom; fill = cluster color; 1px `#090d14` outline; hub/selected/filter-matched nodes get a radial glow halo.
- Links: quadratic béziers, control point pulled toward the canvas center by a **bundling factor** — 0.28 in WEB, 0.86 in RING (this is what makes the ring read as a bundled cobweb). Idle stroke `rgba(126,168,199,.26±.09)` animated slowly; highlighted stroke `rgba(70,199,240,.85)` with an 8px accent shadow; dimmed `rgba(88,109,130,.10)`.
- Idle flourish: 16 small accent dots travel along sampled links (`t` cycles ~0.24/s, alpha `sin(πt)`); suppressed while a node is focused.
- Labels: Plex Mono 500 11px on a `rgba(9,13,20,.72)` plate. Shown when: node is selected, is a neighbor of the focused node (and zoom > 0.5), matches the filter, or (unfocused) zoom > 1.5 / degree > 9. In RING, labels on the left half are drawn flush-right of the node (outward). Labels whose box intersects the legend or zoom-control boxes are skipped (except the selected node's).
- Clusters (6, color-coded): xref partitions `--accent`, rnc core `--ok`, rfam/models `--info`, pipeline/precomputed `--warn`, literature/ontology `--text-soft`, ops/audit `--danger`.

**Overlays:** legend bottom-left (hidden when canvas < 520px wide); bottom-right zoom cluster = zoom % chip, `−`, `+`, `FIT`; top-right hint chip (`SCROLL = ZOOM · DRAG = PAN`, becomes `CLICK EMPTY SPACE TO CLEAR` when a node is selected). All overlay chrome: `rgba(11,18,27,.82)` + `1px --border` + radius 4px + `backdrop-filter: blur(6px)`.

**Preview panel** (right):
- Empty state: 46px circle outline + "Select a table in the web."
- Selected state: `SCHEMA.TABLE` heading (mono 14px); 2×2 stat tiles `≈ ROWS`, `COLUMNS`, `FK OUT` (accent), `FK IN` (ok); **SAMPLE ROWS** block — sticky header row with column name (mono 500 10px, `--text-soft`) over its type (mono 9px, `--text-faint`), 6 data rows (mono 11px, `#c8dae9`), cells `min-width 104px; max-width 190px`, ellipsis, horizontal + vertical scroll (`max-height 190px`), provenance meta `SELECT * FROM public.<table> LIMIT 6`, footnote "Read-only sample · types are confirmed on the next screen."; **LINKED TABLES** list (max-height 150px, scroll) with `OUT`/`IN` direction tags — clicking a row selects that table in the graph; footer primary `CONTINUE → PREVIEW & CONFIGURE` + note "Creates staging.&lt;name&gt; / production.&lt;name&gt; on the next screen."

---

## Interactions & behavior

**Connect card**
- Test connection → `testing` (form disabled, spinner) → `connected` (success strip + picker) or `error` (inline strip, form editable, password cleared).
- Engine change updates the port default (only if the user has not edited it) and the schema default.
- Strategy tiles are radio-like; changing the selection swaps the sub-form. Date partition toggle reveals/hides its sub-panel.

**Table web**
- Wheel = zoom anchored at the cursor: `k' = clamp(k * exp(-deltaY * 0.0016), 0.15, 9)`, then `view.x = mouseX - worldX*k'`.
- Drag = pan (cursor `grab`/`grabbing`); a drag of >3px suppresses the click-select.
- Click a node = select (focus mode: neighbors + their links highlighted, everything else dimmed, pulses off). Click empty space = clear.
- Hover = same focus treatment, transient.
- Layout switch = positions ease to the new target (`p += (target - p) * 0.12` per frame) then `FIT` re-frames.
- `FIT` frames the current layout's bounding box with 70px padding.
- Filter = substring match on table name; matches stay lit and labelled, non-matches dim.
- Layouts: **WEB** = clustered force layout (repulsion `1600/d²` cut off at d²>90000, spring length 64 stiffness .035, per-cluster anchor gravity .014 on a 400×340 anchor ellipse, 420 iterations, then normalize: recenter on centroid, clamp outliers to 1.25× the 92nd-percentile radius, scale so that percentile = 470). **RING** = alphabetical within cluster around a 430px circle with heavy edge bundling. **CLUSTERS** = one small grid per cluster placed around a 470px ring.
- Rendering is a single `requestAnimationFrame` loop drawing links → pulses → nodes → labels; ~186 nodes / 400 links holds 60fps.

## State
```
sourceType: 'file' | 'rdbms'
connection: { engine, host, port, database, schema, username, password, ssl }
connState:  'idle' | 'testing' | 'connected' | 'error'
serverInfo: { version, tableCount, latencyMs } | null
error:      string | null
tables:     [{ schema, name, approxRows, cols }]
edges:      [{ from, to }]                 // FK graph
filter:     string
selected:   tableId | null
targetName: string
strategy:   'CURSOR' | 'TIME_WINDOW' | 'FULL'
strategyParams: { cursorColumn } | { windowColumn, grain, settlingLagMin } | {}
partition:  { enabled: boolean, column: string|null, grain: 'DAILY'|'MONTHLY'|'YEARLY' }
graph:      { layout: 'web'|'ring'|'grid', view: {k,x,y}, hover, positions }
```

**Endpoints to wire (suggested):**
- `POST /api/sources/rdbms/test` → `{connection_id, engine, version, latency_ms}` or `{error}` (never returns the password).
- `GET /api/sources/rdbms/{connection_id}/schemas`
- `GET /api/sources/rdbms/{connection_id}/{schema}/tables` → `[{name, approx_rows, columns}]` + `[{from, to}]` FK edges (the graph needs both in one payload).
- `GET /api/sources/rdbms/{connection_id}/{schema}/{table}/sample?limit=6` → `{columns:[{name,type}], rows:[[...]]}`.
- `GET .../{table}/columns` → used to populate cursor / window / partition column selects (filter by type client-side).
- `POST /api/onboard/step2` (existing) with the payload in §4 above.

## Design tokens
```
--void      #090d14   page background
--panel     #0f1722   cards
--panel-2   #0b121b   inputs, list boxes, canvas ground
--border    #1f2e3e   1px borders; #141d29 for internal hairlines
--text      #e8eff7
--text-soft #93a6ba
--text-faint#586d82
--accent    #46c7f0   primary; ink on accent = #062230
--ok        #3fd08b
--warn      #f2b445
--danger    #ff6f6b
--info      #6aa8ff
radius      6px (cards) / 4px (inputs, buttons, tiles) / 999px (pills)
tint fills  color at 8–12% alpha; strips add a 2px left border in the full color
```
**Type:** IBM Plex Sans (body 12.5–13.5px/1.6) · IBM Plex Mono (field labels, table cells, code, meta; eyebrow = 10px/.12–.14em uppercase `--text-faint`) · Chakra Petch (display headings; H1 30–34px 700, card titles 17–18px 600).

**Spacing:** card padding 22–26px; field grid gap 16px/18px; section gaps 20–24px; label→input 7–8px; input padding 10px 12px; button padding 11px 18px.

**Elevation:** none on cards (borders + corner ticks only). The only shadows are focus rings and canvas glows.

## Assets
None. All chrome is CSS/Canvas; the corner ticks, spinner, chevrons and toggles are drawn with borders/pseudo-elements. Icons in the prototype are text glyphs — substitute the app's existing icon set.

## Files
- `Onboard RDBMS Source.dc.html` — artboards `1a` (two-column step 1), `1b` (idle + annotations), `1c` (testing), `1d` (connected, picker, date partition, CURSOR), `1e` (TIME_WINDOW / FULL sub-forms), `1f` (error).
- `Table Web.dc.html` — interactive table-web screen (graph + preview panel). The graph logic lives in the `Component` class at the bottom of the file: `build()` (synthetic 186-table / 400-FK dataset — replace with the introspection payload), `layoutWeb/layoutRing/layoutGrid()`, `draw()`, `sample()`.
- `support.js` — preview runtime for the two prototypes only. Not part of the design; do not port.
