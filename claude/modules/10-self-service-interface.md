# Module 10: Self-Service Interface
**Owner:** DE + AE (Shared)
**Layer:** Presentation
**Back to:** [claude.md](../claude.md)

---

## Purpose

Makes the entire pipeline infrastructure accessible to non-technical users. Business users can onboard new sources, monitor pipeline health, review quarantined records, and trigger manual runs — all without writing a single line of code or raising a support ticket.

---

## Key Design Decisions

- Built on Foundry Workshop (no-code application layer)
- All actions backed by SQL queries and Pipeline Builder pipelines
- Role-based access — business users see different views than DE/AE teams
- No direct table access for business users — everything via the interface

---

## Features to Build

### 10.1 Onboarding & First-Load Wizard
A DE-driven, 7-step flow. Writes to Schema Registry (Module 3), `extraction_control` (Module 1), and the Task Scheduler (Module 8).

1. **Configure source** — name, type, connection details, owner, table(s) to extract
2. **Connect & replicate** — system connects, reads the source table structure, auto-infers columns + types, creates the staging (and production) table shells
3. **Sample load** — extract the **top 1,000 rows** into staging (preview only — nothing reaches production)
4. **Preview & approve schema** — DE sees the sampled rows, the columns, and picks the **`load_type`** (`FULL_SNAPSHOT` / `INCREMENTAL`). If INCREMENTAL: either an **`existence_check_column`** (single date column) **or** a **composite `natural_key`** — as built, three `nk_col` dropdowns (a chosen column greys out in the others; a live chip line shows the resulting key; each option carries its type, synced to the type selects). Per-column target types are set on the same screen.
5. **Load config** — DE sets `extraction_strategy` (CURSOR / TIME_WINDOW / FULL), the cursor or `window_column` + grain, `settling_lag`, batch-size override
6. **Recurring or one-time?** — Recurring → set interval (= the TIME_WINDOW size) + start time, writes an ACTIVE `schedule_control` row. One-time → no schedule; optional bounded range to pull; runs once then INACTIVE.
7. **First real run** — config set: the pipeline runs for real → staging → transform → production; staging truncated after

### 10.2 Schema Registration / Editing UI
- Per-table settings: `load_type`, `existence_check_column` / `natural_key`, per-column types, extraction config
- Validates duplicates / conflicts before saving; version history per table; diff two versions
- *As built:* onboarding writes all of it; the table page shows config read-only and can **re-type** production columns in place (`retype_table`). No post-onboard edit form for `load_type` / `natural_key` yet — change = delete + re-onboard.

**As built (Slice 1).** Onboarding writes the registry and creates the four layer
tables; the table page shows config + columns read-only (no edit form / version
diff yet). The onboard preview **rejects a target table name whose layer tables
already exist** — names are globally unique (Module 3, "Physical targets &
naming") — showing the clash inline before anything is created.

### 10.3 Pipeline Status Dashboard
- Real-time view of all pipeline executions
- Color-coded status per layer (Extract / Staging / Production / Business)
- Filterable by source system, table, date range
- Drill-down into individual run details and error messages
- Powered by pipeline_health_summary view (Module 9)

**As built (Slice 1, 2026-08-31).** Home (`/`), Pipeline Monitor (`/runs`), and the
per-table page. The Monitor shows **one row per `run_id`** with a pill per stage
(`app._recent_runs`; Module 9). Three UI regions self-refresh on the
`odin:runs-changed` body event (fired by every extract / transform / load) — the
run log panel, the animated lineage diagram, and the table page's
staging / production / waiting / quarantine **KPI cards** (`_data_stats.html` +
`GET /partials/stats/{s}/{t}`) — plus an interval tick while a job is active.
`pipeline_health_summary` and the alerting tables are not built yet.

### 10.4 Data Quality Scorecard
- Per source system quality score (% records passing validation)
- Trend chart showing quarantine rate over time
- Top failing columns and most common failure reasons
- Exportable report for business stakeholders

### 10.5 Quarantine Review Interface  ✅ backend: `odin/resolve.py`
- Lists open `quarantine_batch_log` rows: `qbatch_id`, `source_id`, `table_name`, `reason`, `row_count`, `created_at`; opening one shows its rows from `quarantine.<tbl>` (verbatim VARCHAR)
- Actions (DE): **Re-inject** (`reinject_quarantine` — rows back to staging under a new `batch_id`, then a transform re-processes; still-bad rows re-quarantine), **Ignore** (`ignore_quarantine` — drop the rows, mark ignored)
- Filter by source, table, date, reason; bulk actions
- No patch-in-place — a fixed row always re-enters as a new batch

### 10.5a Waiting-Pipeline Review Tool  ✅ backend: `odin/resolve.py`
- Lists pending `waiting_batch_log` rows — one per `(source, table, existence_value)` with `wbatch_id`, `row_count`, age
- Opening one shows its rows from `waiting.<tbl>` and production's current rows for that `existence_value` side by side
- Actions: **Approve** (`approve_waiting` → delete production's rows for that value, insert the waiting rows with `restated = true`) · **Reject** (`reject_waiting` → drop the waiting rows, production unchanged)
- Its auth is **separate** from the DE/AE roles — assigned per source
- Filter by source, table, age; backlog + oldest-pending age shown

### 10.6 Task Scheduler View
- Every pipeline (recurring + one-time): source, stage, schedule, `load_recurrence`, status, last run, next run
- Actions (DE): pause / resume, edit schedule (schedule override), manual trigger, re-run a one-time load, convert one-time → recurring

### 10.6a Manual Run
- Select source + table; confirmation dialog; immediate run status
- Ties into Module 8 manual trigger

**As built (Slice 1, 2026-08-31).** On the table page (`/t/{source}/{table}`) the
primary action is **"Load to Production"**: a drop-zone that **auto-submits the
moment a file is picked** (`data-autosubmit`, `app.js`) and POSTs
`/t/{s}/{t}/load` → `app.table_load`. A manual run always targets production
(Module 8 §8.3):
- **Staging empty** → one `ingest` job (extract → transform → `TRUNCATE staging`)
  under one `run_id` → one Monitor row, both stage pills.
- **Staging not empty** → returns an inline **"Flush & load"** confirm
  (`_load_confirm.html` into `#load-slot`): confirming submits a `transform` job
  (drains the leftover staging rows to production) then an `ingest` job for the new
  file — ordered by the single worker.
- Guarded while a job is already active for that pipeline; toasts on
  no-file / expired-upload / unknown-pipeline.
- The old standalone **Extract only** (`/extract`) and **Run transform**
  (`/transform`) buttons remain, moved into a collapsed **"Manual steps"**
  `<details>` — needed for staging inspection and for re-running the transform
  after a quarantine re-inject (§10.5).
- For a same-day `FULL_SNAPSHOT`, loading a second file replaces the first (same
  `load_date`) — that is the load type's semantics, not an error.

### 10.6b Delete Pipeline (table-page danger zone)
- Collapsed **"Delete pipeline"** section on the table page. Previews the exact
  `DROP` / `DELETE` statements (`registry.deregister_plan`), requires typing
  `source.table` to confirm, then `registry.deregister_source` drops the four layer
  tables and removes every registry + `run_log` row in one transaction (optional:
  "tables only, keep registry rows" / "keep `run_log` history"). Blocked while a
  job is running. Same operation is also available in the SQL Console danger zone
  (§10.8). The source can be re-onboarded afterwards.

### 10.7 Column Type Change Request
- Form to propose a new data type for a column on a production table
- On submit, runs the verification dry run (Module 4, section 4.8) and shows the result — pass, or the count and sample of values that would fail the cast
- On a clean result, the user confirms; the change is committed to the Schema Registry (Module 3, section 3.7), the staging → production SQL is regenerated (Module 11), and the online migration is scheduled (Module 8)
- Migration progress (partitions rewritten, swap status) visible from this page

**As built (Slice 1).** The table page's **"Production column types"** card: pick a
type per column, submit → `registry.retype_table` runs synchronously, test-casting
every existing value first (a value that will not convert aborts and names it),
then rebuilds `production.<t>` in place and migrates the rows. Blocked while
quarantine / waiting batches are open. The standalone dry-run form and the
partition-by-partition online migration are not built yet.

### 10.8 SQL Console (`/sql`)   — as built (Slice 1, 2026-08-31)
An ad-hoc SQL workbench with guard rails, for DEs. Not in the original module
plan; added as an operational tool.
- `odin/sqlconsole.py` classifies each statement — `read` / `write` / `ddl` /
  `admin`. `read` runs in a rolled-back read-only transaction; `write` / `ddl`
  require a **Write mode** toggle and run in a held-open transaction the user
  **Commits or Discards** (row counts shown first; `DROP` / `TRUNCATE` need the
  object name typed); `admin` runs autocommit with a "no undo" note;
  `BEGIN` / `COMMIT` / `ROLLBACK` are refused.
- Dedicated connections (never the pool), `statement_timeout`, results capped at
  `sql_row_cap` with a "Load all" escape. Every statement (incl. Discarded) is
  audited to `sql_console_log`. Result grid is a fixed-height box (≈52 vh) that
  scrolls both axes with a sticky header.
- CodeMirror 5 vendored (`static/vendor/codemirror/`, no build step): SQL
  highlight, schema-aware autocomplete, block cursor.
- **Danger zone**: "Deregister & drop a source" — previews every table + control
  row that goes, then `registry.deregister_source` in one transaction (blocked
  while a job is active). Same operation as §10.6b.
- UI is an "Ops terminal" theme with a right-side slide-out schema drawer
  (`.sqlc` scope in `app.css`; `static/sqlconsole.js`).

---

## Role-Based Access Matrix

| Feature | Business User | AE Team | DE Team | Admin |
|---------|--------------|---------|---------|-------|
| View Pipeline Status / Task Scheduler | ✓ | ✓ | ✓ | ✓ |
| View Quality Scorecard | ✓ | ✓ | ✓ | ✓ |
| Review Quarantine | ✓ | ✓ | ✓ | ✓ |
| Re-inject / Ignore Quarantine | | ✓ | ✓ | ✓ |
| Waiting-Pipeline Approve / Reject | separate auth model — assigned per source, independent of the roles here |
| Manual Trigger / Schedule Override | | ✓ | ✓ | ✓ |
| Schema Registration / Editing | | ✓ | ✓ | ✓ |
| Column Type Change Request | | ✓ | ✓ | ✓ |
| Source Onboarding | | | ✓ | ✓ |
| Alert Configuration | | | ✓ | ✓ |
| Registry Admin | | | | ✓ |

---

## Interface Pages

```
1. Home Dashboard
   └── Pipeline Health Overview
   └── Recent Alerts (failures, quarantine spikes, waiting-pipeline backlog)
   └── Quick Stats (rows to production today, quarantine rate, pending restatements, active pipelines)

2. Pipeline Monitor
   └── Run Log (per run / per chunk)
   └── Per-Pipeline Drill-Down
   └── SLA Adherence View

3. Task Scheduler
   └── All Pipelines (recurring + one-time): schedule, status, last/next run
   └── Pause / Resume / Edit Schedule / Manual Trigger / Re-run one-time / Convert to recurring

4. Data Quality
   └── Quality Scorecard
   └── Quarantine Review (re-inject as new batch)
   └── Waiting-Pipeline Review (approve / reject — separate auth)
   └── Schema Drift Alerts

5. Source Management
   └── Registered Sources List
   └── Onboarding & First-Load Wizard (7 steps, 1000-row preview)
   └── Schema Registration / Editing (load_type + existence_check_column)
   └── Column Type Change Request
```

**As built (Slice 1)** — server-rendered (FastAPI + Jinja2 + htmx, no auth):
`/` Operations Deck (sources + recent runs) · `/onboard` wizard (upload → preview →
create) · `/runs` Pipeline Monitor · `/t/{source}/{table}` per-pipeline page
(config, KPI cards, **Load to Production**, animated lineage, generated SQL,
column-type re-typing, waiting / quarantine batches, delete-pipeline) ·
`/waiting` + `/quarantine` review · `/sql` SQL Console (§10.8). Not built: Task
Scheduler view, Quality Scorecard, Schema Drift, SLA views, role-based access
(single local operator for now).

---

## Dependencies

- Module 3: Schema Registry (onboarding wizard and type-change requests write here)
- Module 4: Data Quality (quarantine data displayed here; type-change verification dry run)
- Module 8: Orchestration (manual trigger and migration scheduling connect here)
- Module 9: Monitoring (all dashboard data sourced here)
- Module 11: SQL Generation Engine (regenerates SQL after a confirmed type change)

---

## Resolved

- Onboarding is a DE-driven 7-step wizard with a 1,000-row sample preview before anything recurring is committed
- Task Scheduler is the pipeline registry surface; one-time loads kept and re-runnable
- Waiting-pipeline review is a distinct tool with its own auth model
- Quarantine has no patch-in-place — re-inject as a new batch

## Open Questions

- [ ] Foundry Workshop confirmed as the platform (rec: yes)
- [ ] Who owns role assignment — IT/security vs DE (rec: IT/security owns, DE requests)
- [ ] Waiting-pipeline reviewer assignment — per source, by whom?
