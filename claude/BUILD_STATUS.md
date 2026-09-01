# Build Status — feature-by-feature progress

**Back to:** [claude.md](claude.md) · **Design decisions:** [decisions.md](decisions.md)

Tracks every numbered feature in Modules 1–11 against the code in `src/odin/` + `sql/`.
Updated: **2026-08-31** (Slice 1: onboard → extract → transform → resolve, + CLI + web UI + SQL Console; schema-per-layer naming; "Load to Production" one-click flow; self-refreshing dashboard partials).

| Mark | Meaning |
|------|---------|
| ✅ | Built and verified end-to-end |
| 🟡 | Partial — core path works, pieces missing (noted) |
| 📋 | Designed only — documented, not yet coded |
| ⏸️ | Deferred — a later "Configure" step or a later slice |
| 🌐 | Foundry-native — not applicable to the local Python build |

Current scope: **Slice 1 = CSV/TXT load & consume**. Slice 2 = RDBMS, Slice 3 = NoSQL (each reuses the staging→production tail).

---

## Rollup

| Module | Built | Partial | Designed / Deferred | Notes |
|--------|:---:|:---:|:---:|-------|
| 0 · Scaffold / infra | ✅ | — | — | uv project, config, pooled + log DB access, migrations, naming, CLI, web UI |
| 1 · Extraction | 🟡 | file path done | RDBMS/NoSQL, scheduling, `extraction_control` | file *is* the source; land → Check 1 → COPY |
| 2 · File Pre-Processing | 🟡 | file area + control table | XML/JSON/SHP flatten, retention sweep | flatteners are 🌐 |
| 3 · Schema Registry | 🟡 | tables + onboard write | typing, versioning, drift, regen | `load_type` + `existence_check_column` live |
| 4 · Data Quality | ✅ | Check 1/2, both diversions | business rules, dry-run, scorecard | set-based; per-table `quarantine.*` / `waiting.*` |
| 5 · Staging Layer | ✅ | load, VARCHAR, metadata, lock | drift-hold workflow | ephemeral, truncated per run |
| 6 · Production Layer | ✅ | transform + routing + write | business rules, online migration | `FULL_SNAPSHOT` + `INCREMENTAL` verified |
| 7 · Business Layer | 📋 | — | all (AE sessions) | not started |
| 8 · Orchestration | 🟡 | manual triggers, no-retry | scheduler, deps, job groups | Python calls are the triggers for now |
| 9 · Monitoring | 🟡 | `run_log` + batch logs | alerting, drift log, freshness, size | `runlog.py` on autocommit conn |
| 10 · Self-Service UI | 🟡 | web UI: onboard/run/runs/waiting/quarantine | dashboards, scorecard, scheduler, type-change | `odin web` + `odin` CLI both live |
| 11 · SQL Generation | 🟡 | DDL builders | template engine, store, regen | SQL composed in `ddl.py` / `transform.py` |

---

## Module 0 — Scaffold / infrastructure  ✅

| Piece | Status | Where |
|-------|:---:|-------|
| uv project, deps, `odin` entry point stub | ✅ | `pyproject.toml` |
| Runtime config (env / `.env`, `ODIN_` prefix) | ✅ | `src/odin/config.py` |
| Pooled DB access + autocommit log connection | ✅ | `src/odin/db.py` (`connection()`, `log_connection()`) |
| Ordered SQL migrations + tracking | ✅ | `src/odin/migrate.py`, `schema_migrations` |
| Identifier / table-name helpers | ✅ | `src/odin/naming.py` |
| Core schema (11 control tables + 2 schemas) | ✅ | `sql/001_core.sql` |
| CLI — `migrate` / `tables` / `sample` / `onboard` / `run-extract` / `run-transform` / `ingest` / `runs` / `waiting` / `quarantine` / `web` (+ `--json`) | ✅ | `src/odin/cli.py` (`odin` entry point) |
| Web UI (FastAPI + Jinja2 + htmx, server-rendered, no auth) — design system, home, onboard wizard, table page, live run log, waiting/quarantine review | ✅ | `src/odin/web/` (`app.py`, `templates/`, `static/`); `odin web` |
| Background job runner — web `extract`/`transform`/`ingest` run on a single-worker thread; HTTP returns immediately; runs panel polls (`hx-trigger="every 3s"`) while a job is active | ✅ | `src/odin/jobs.py`, `/partials/runs` |

---

## Module 1 — Extraction Layer

| # | Feature | Status | Notes |
|---|---------|:---:|-------|
| 1.1 | Batch Extraction Engine | 🟡 | File read in fixed row-count batches (`connectors/file.py:iter_batches`, `extract.load_to_staging`). CURSOR / TIME_WINDOW batch-planning not built (Slice 2). |
| 1.2 | Batch Size Configuration | 🟡 | Global `settings.batch_rows` / `chunk_rows`. Per-table `extraction_control.batch_size` not built. |
| 1.3 | Extraction Scheduling | 📋 | Owned by Module 8 — nothing scheduled yet; runs are manual calls. |
| 1.3a | Failure model — no automatic retry | ✅ | `extract.py` marks the file FAILED + `run_log` failed, re-raises, never retries. |
| 1.4 | Flat File Writer | 🟡 | File sources: `extract.land_file` copies the file into `staging_file_area/{source}/{table}/{date}/` and registers it. DB → CSV writer not built (Slice 2). |
| 1.5 | RDBMS Connector | 🟡 | Slice 2, **phases 1–2 of 4 built** (`connectors/rdbms.py`, PostgreSQL only): `probe` (connect + `version()` + schema list, cleaned driver errors), `save_connection` / `get_connection` / `connection_meta` — credentials in a `secret` schema, password `pgcrypto`-encrypted (`sql/006_rdbms.sql`, key = `settings.rdbms_secret_key`). Web: `/onboard` reflowed to a 2-column source picker (File | Relational database), `POST /onboard/rdbms/test`, `/onboard/rdbms/{cid}/schemas` (visual schema-tile picker) → `/{schema}/tables` (radial FK **cobweb** — `list_tables` + `fk_edges` from `pg_class`/`pg_constraint`, `_cobweb_layout` places nodes on a ring with FK arcs bowed to centre; click a node → JS lights incident threads + htmx-loads a `peek` panel, `SELECT * … LIMIT 20` + column types; a live substring filter re-lays-out). **Next phases:** bound-the-pull filter + step-2 hand-off (types pre-filled from source) → batched extract-to-CSV + Full/Tenure run prompt. Standalone CSV/TXT path ✅ via `connectors/file.py`. |
| 1.6 | NoSQL Connector | 📋 | Slice 3. |
| 1.6a | Nested-File Connector (XML/JSON/SHP) | 🌐 | Foundry transforms / later. |
| 1.7 | Onboarding Sample Extraction | 🟡 | `connectors/file.sample_rows(path, n)` exists; not wired to a wizard (no UI). |
| — | `extraction_control` table | 📋 | Not built. `registry_tables` currently carries strategy + recurrence. |
| — | Execution Phases 1–6 | 🟡 | Realised for the file path in `extract.run_extract` (checks the table is registered → land → Check 1 → COPY, per-batch lock, FAILED on error). **One run-level `EXTRACT` `run_log` row per call** — set `running` up front, then `failed` (with reason) or `success` — so a file that never gets past landing / Check 1 / an empty body is still visible in the Monitor. Phase 3 scoping is a no-op for files; marker advance (Phase 5) is DB-only for Slice 2. |

---

## Module 2 — File Pre-Processing

| # | Feature | Status | Notes |
|---|---------|:---:|-------|
| 2.1 | XML Flattening | 🌐 | Not in the local build. |
| 2.2 | JSON Flattening | 🌐 | Not in the local build. |
| 2.3 | Shapefile Flattening | 🌐 | Not in the local build. |
| 2.4 | Nested Struct Flattening | 🌐 | Not in the local build. |
| 2.5 | Staging File Area Management | 🟡 | `extract.land_file` writes the dated path + inserts `staging_file_control`; `settings.ensure_dirs()`. Retention sweep / cold archive not built. |
| 2.6 | Malformed File Handling | 🟡 | Check 1 header mismatch → `staging_file_control` FAILED + `error_message` (`extract.check_1`). CSV-reader parse-failure handling is minimal (relies on `csv` errors bubbling up). |
| — | `staging_file_control` table | ✅ | `sql/001_core.sql` — CSV/TXT, statuses PENDING/LOADING/LOADED/FAILED. |

---

## Module 3 — Schema Registry

| # | Feature | Status | Notes |
|---|---------|:---:|-------|
| 3.1 | Source System Registration | 🟡 | `registry.onboard_file_source` upserts `registry_sources`. A **brand-new `(source_id, table_name)`** is pre-checked in SQL (`to_regclass`) — none of its 4 physical targets may already exist, else `RegistryError` (re-registering an already-registered table stays idempotent). Teardown: `registry.deregister_source()` drops the 4 tables + control-plane rows (and the `registry_sources` row if it was the last table) in one locked transaction; `deregister_plan()` is the read-only preview. No role-gating, no wizard. |
| 3.2 | Expected Column Names Store | ✅ | `registry_columns` + `registry.get_columns` (ordered, `effective_to IS NULL`). |
| 3.3 | Expected Data Types Store | ⏸️ | `registry_columns.target_data_type` exists; all `text` in the base build. Per-column typing is the deferred "Configure" step. |
| 3.4 | Nullable Flags Store | ✅ | `registry_columns.is_nullable`; `registry.get_columns_meta` reads it; transform's empty-required check consumes it. |
| 3.4a | Load Type & Collision Routing | ✅ | `registry_tables.load_type` + `existence_check_column` **+ `natural_key`** (ordered comma-list; supersedes the single column for INCREMENTAL; key cols can't be numeric-typed). Validated + written by `onboard_file_source`, read by `transform`. |
| 3.4b | Extraction Strategy & Recurrence | 🟡 | Columns present (`extraction_strategy` default `FULL`, `load_recurrence`, cursor/window). Only `load_recurrence` is meaningfully used in Slice 1. |
| 3.5 | Version Control for Schema Changes | 🟡 | `schema_version` / `effective_from` / `effective_to` columns + `registry_change_log` exist; only a `REGISTER` row is written. No version workflow. |
| 3.6 | Schema Change Alerting | 📋 | — |
| 3.7 | Verified Schema Change → SQL Regeneration | 📋 | Needs Module 11 regen + Module 6 migration. |
| — | `registry_change_log` table | ✅ | Built; `onboard_file_source` writes `REGISTER`. |

---

## Module 4 — Data Quality

| # | Feature | Status | Notes |
|---|---------|:---:|-------|
| 4.1 | Check 1 — Header Validation | ✅ | `extract.check_1` — names + count vs registry; mismatch → file FAILED + reason, nothing loaded. |
| 4.2 | Check 2 — Structural validation (set-based) | 🟡 | `transform._bad_predicate`: over-length `char_length > 256` ✅, empty-required (`NOT NULL` cols) ✅. Would-not-cast is a no-op until types are configured. NUL bytes are rejected at COPY, by design. |
| 4.3 | Quarantine the bad rows | ✅ | `transform._quarantine` — `INSERT … SELECT s.*, qbatch` into `quarantine.<t>`, one `quarantine_batch_log` row per reason, `DELETE` from staging. |
| 4.4 | Collision routing by `load_type` | ✅ | `transform._load_full_snapshot` (delete this `load_date`, reinsert) + `transform._load_incremental` (colliding existence values → `waiting.<t>`; rest → production) + **`_load_incremental_nk`** (composite `natural_key`: one indexed set-based `EXISTS` join on a hashed `nk bigint` + raw-column tie-break; one waiting batch per run; 50k same-file reload ≈ 1 s vs the per-value loop's hours). `ddl.natural_key_sql` / `natural_key_match` / `nk_index_ddl`. Verified. |
| 4.5 | In-load dedup | ✅ | By design: none at row level; a repeat existence value routes to `waiting`. Behaviour verified. |
| 4.6 | Quarantine tables | ✅ | `quarantine` schema + `quarantine.<tbl>` (`ddl.quarantine_ddl`, created at onboard) + `quarantine_batch_log` (`sql/001_core.sql`). |
| 4.7 | Waiting pipeline tables | ✅ | `waiting` schema + `waiting.<tbl>` (`ddl.waiting_ddl`) + `waiting_batch_log`. Approve/reject in `resolve.py`. |
| 4.8 | Business Range / Value Rules (SOFT/HARD) | 📋 | `quality_rules` table not built; Slice 1 applies no business rules. |
| 4.9 | Type-Change Verification (Dry Run) | 📋 | — |
| 4.10 | Reporting | 🟡 | `run_log` carries per-run `rows_to_production` / `rows_to_waiting` / `rows_quarantined`. Scorecard views not built. |
| — | Manual recovery of both diversions | ✅ | `resolve.py` — `approve_waiting` / `reject_waiting` / `reinject_quarantine` / `ignore_quarantine` + `pending_waiting` / `open_quarantine`. Verified incl. error paths. Nothing auto-reprocesses. |

---

## Module 5 — Staging Layer  ✅

| # | Feature | Status | Notes |
|---|---------|:---:|-------|
| 5.1 | Raw Load (Python) | ✅ | `extract.load_to_staging` — bulk `COPY`, per-batch `try_lock`, `staging_file_control` LOADING→LOADED, FAILED on error. Progress / failure is recorded by the single run-level `EXTRACT` `run_log` row `run_extract` owns. |
| 5.2 | VARCHAR Standardization | ✅ | `ddl.staging_ddl` — every source column `text`; values loaded as-is (short rows padded, extras → `__extra__`). |
| — | **Physical naming** | ✅ | A schema per layer: `staging.<table>` / `production.<table>` / `quarantine.<table>` / `waiting.<table>`, where `<table>` is `naming.slug(table_name)` — the DE-given name, no source prefix. Table names are therefore globally unique (the `_assert_targets_absent` SQL pre-check refuses a re-use). `registry_tables.staging_target` / `production_target` store the qualified `schema.table`; `naming.qname` renders it and treats a dotless legacy name as unqualified (public) so pre-`sql/004` pipelines keep working. `sql/004_layer_schemas.sql` creates the `staging` + `production` schemas. |
| 5.3 | Metadata Columns at Staging | ✅ | `ddl.STAGING_META` — `staging_record_id`, `load_date`, `load_timestamp`, `source_file_id`, `batch_id`, `source_system`; stamped in `load_to_staging`. |
| 5.4 | Diversion Hand-off | ✅ | Check 1 → FAILED; Check 2 → `quarantine.<t>`; incremental collision → `waiting.<t>`. |
| 5.5 | New Source Column Handling | 🟡 | Check 1 rejects any header that isn't an exact match. "Drift alert + hold + registry update" workflow not built. |
| 5.6 | Concurrency With the Transform | ✅ | `locks.py` — `try_lock` (loader, per batch) / `lock` (transform + resolve, whole run). Keyed on `hashtext(staging_target)`. |

---

## Module 6 — Production Layer  ✅

| # | Feature | Status | Notes |
|---|---------|:---:|-------|
| 6.1 | Run Setup | ✅ | `transform.run_transform` — blocking `lock`, single transaction, set-based (no chunk streaming). |
| 6.2 | The transform steps | ✅ | structural filter → quarantine → route by `load_type`. |
| 6.3 | Business Rule Application | 📋 | Hook point in the write `SELECT`; no `quality_rules` yet. |
| 6.4 | Writing the Rows | ✅ | `INSERT … SELECT` into production; per-value `INSERT … SELECT` into `waiting.<t>` + `waiting_batch_log`. Daily partitions via `ddl.production_partition_ddl`. |
| 6.5 | End of Run | ✅ | `TRUNCATE staging` + commit; `run_log` row on the autocommit connection; no auto-retry. |
| 6.6 | Schema Enforcement | 🟡 | Production DDL is registry-derived (`ddl.production_ddl`). A live column/type-drift check at run start is not built. |
| 6.7 | Online Schema Migration (type change) | 📋 | Deferred; needs Modules 11 + 8. |
| — | Production table DDL | ✅ | `ddl.production_ddl` — target types, `NOT NULL` where registered, `PARTITION BY RANGE (load_date)`, `restated`; no `period`, no row-level `UNIQUE`. |

---

## Module 7 — Business Layer  📋

| # | Feature | Status |
|---|---------|:---:|
| 7.1 | Aggregation Scripts | 📋 |
| 7.2 | Join Logic Automation | 📋 |
| 7.3 | Business Metric Definitions | 📋 |
| 7.4 | Ontology Output Mapping | 🌐 |
| 7.5 | Virtual Table Management | 📋 |
| 7.6 | Business Layer Table Management | 📋 |

Not started — AE sessions, after the ingestion slices.

---

## Module 8 — Orchestration

| # | Feature | Status | Notes |
|---|---------|:---:|-------|
| 8.1 | Task Scheduler & Pipeline Registry | 📋 | `schedule_control` not built. |
| 8.2 | Dependency Management | 📋 | `pipeline_dependencies` not built. |
| 8.3 | Trigger Types | 🟡 | `manual` via the CLI, or via the web UI which dispatches to a **single-worker background runner** (`odin.jobs`) so the request doesn't block; `onboarding` for run-now. `scheduled` / `event` / `dependency` not built. |
| 8.4 | Pipeline Parameterization | 🟡 | `run_extract` / `run_transform` take `source_id` + `table_name`; `run_id` from `runlog.new_run_id`. `odin.jobs.Job` carries kind/source/table/file/triggered_by + in-memory state. |
| 8.5 | Failure Handling — no automatic retry | ✅ | Uniform across `extract` / `transform` / `resolve`: FAILED + alert-point + re-raise, no retry, no auto-reprocess. |
| 8.6 | Job Group Management | 📋 | — |
| 8.7 | Online Schema Migration Scheduling | 📋 | — |

---

## Module 9 — Monitoring & Alerting

| # | Feature | Status | Notes |
|---|---------|:---:|-------|
| 9.1 | Run Log | ✅ | `run_log` table + `runlog.py` (`new_run_id` / `start` / `finish`) on a separate autocommit connection. Written by extract (one row per run), transform (per run — incl. a `success`/0 row for an empty-staging no-op), resolve (per action). Survives whole-run rollback. |
| 9.2a | Background-job failure surfacing (web) | ✅ | `jobs.recent_failed(120s)` → red banner in the runs panel, so a background `extract` / `transform` failure isn't just an in-memory job state lost on restart. |
| 9.2 | Failed Job Alerting | 🟡 | Failures land as `run_log.status = 'failed'` + `error_message`. `alert_config` + channels not built. |
| 9.3 | Quarantine & Waiting Volume Monitoring | 🟡 | Raw counts in `run_log` + `quarantine_batch_log` / `waiting_batch_log`. Rate views + 5% auto-pause not built. |
| 9.4 | Schema Drift Detection | 🟡 | Check 1 catches header drift at load. `schema_drift_log` + acknowledge/update workflow not built. |
| 9.5 | Data Freshness Monitoring | 📋 | `freshness_config` not built. |
| 9.6 | SLA Breach Alerting | 📋 | — |
| 9.7 | Execution Logs | 🟡 | `run_log` queryable by `run_id` / `source_id` / `table_name` (indexes in `001_core.sql`). Verbose per-run logs not built. |
| 9.8 | Table Size Metadata | 📋 | `table_size_metadata` not built. |

---

## Module 10 — Self-Service Interface

| # | Feature | Status | Notes |
|---|---------|:---:|-------|
| 10.1 | Onboarding & First-Load Wizard | ✅ | Web: `/onboard` → upload → `/onboard/preview` (50-row sample) → pick `load_type` + `existence_check_column` → create (+ optional first run). **Composite `natural_key` pickable** (three key-column dropdowns on the preview screen, type shown faded in each option; CLI `--natural-key`). Also `odin onboard` / `odin sample`. Per-column type "Configure" panel still deferred. |
| 10.2 | Schema Registration / Editing UI | 🟡 | Onboard writes the registry; table page shows config + columns. No edit form / version diff yet. Onboard preview **rejects a target table name whose layer tables already exist** (names are globally unique — Module 3). |
| 10.3 | Pipeline Status Dashboard | 🟡 | Web `/` (tables + recent runs), `/runs` (filterable), per-table page. Three regions self-refresh on the `odin:runs-changed` body event (+ a 4s interval while a job runs), diff-patched with **idiomorph** (`hx-swap="morph:outerHTML"`) so nothing flickers: the runs panel (queued/running strip), the animated lineage, and the table page's **staging/production/waiting/quarantine KPI cards** (`_data_stats.html` + `GET /partials/stats/{s}/{t}` — were static, went stale after a run; counts render compact — `1.23k` / `2.5M` — with the exact value on hover). `pipeline_health_summary` view + colour-coded per-layer grid not built. |
| 10.4 | Data Quality Scorecard | 📋 | — |
| 10.5 | Quarantine Review Interface | ✅ | Web `/quarantine` — **inline re-inject / ignore per row via htmx**, styled confirm modal, toast, panel re-renders in place (`#quarantine-panel`). `/quarantine/{qbatch}` for the full row view. Also `odin quarantine …`. |
| 10.5a | Waiting-Pipeline Review Tool | ✅ | Web `/waiting` — **inline approve (replace) / merge (keep both) / reject per row via htmx** with confirm + toast + in-place panel swap. `/waiting/{wbatch}` shows held rows vs production side-by-side. Also `odin waiting …`. Separate auth model not implemented (no auth yet). |
| 10.6 | Task Scheduler View | 📋 | Needs Module 8. |
| 10.6a | Manual Run | ✅ | Web table page: the primary action is **"Load to production"** — a drag-drop zone that **auto-submits on file pick** and POSTs `/t/{s}/{t}/load`, which runs the `ingest` job (extract → transform → truncate staging) under **one `run_id`, so the Monitor shows one row with both Extract + Transform pills**. If staging still holds un-transformed rows it returns a confirm (`_load_confirm.html`) with **Flush & load** (a `transform` job drains staging, then the `ingest` job loads — single worker ⇒ ordered), **Discard & load** (`TRUNCATE staging` then ingest — for a known-bad batch after a failed transform), or Cancel. Standalone **Extract only** / **Run transform** moved to a collapsed "Manual steps" `<details>` (still needed for quarantine re-inject). All **dispatched to the background worker**; HTTP returns immediately, runs panel polls. Also `odin run-extract` / `run-transform` / `ingest`. |
| 10.7 | Column Type Change Request | 📋 | — |
| 10.8 | SQL Console (web `/sql`) | ✅ | Ad-hoc SQL with guard rails. `odin/sqlconsole.py` classifies each statement (`read` / `write` / `ddl` / `admin`); `read` runs in a rolled-back read-only tx, `write`/`ddl` need a **Write mode** toggle (shifts the whole console to amber) and run in a held-open transaction the user **Commits or Discards** (row counts shown first; `DROP`/`TRUNCATE` need the object name typed), `admin` runs autocommit with a "no undo" note, `BEGIN`/`COMMIT`/`ROLLBACK` refused. Dedicated connections (never the pool), `statement_timeout`, result capped at `sql_row_cap` with "Load all" — the result grid is a **fixed-height (~52 vh) box that scrolls both axes with a sticky header row**. CodeMirror 5 vendored in `static/vendor/codemirror/` (no build step) — SQL highlight + schema-aware autocomplete + block cursor. Every statement (incl. Discarded) → `sql_console_log`. **Danger zone**: "Deregister & drop a source" — previews every table + control-plane row that goes, then `registry.deregister_source()` removes all of it in one transaction (blocked while a job is active for that source). **UI = "Ops terminal"**: `odin://sql $` prompt framing, scanline texture, phosphor accent (page-local, a green-nudged `--accent`), terminal-scrollback history. Schema browser is a **right-side slide-out drawer** (mirrors the platform nav rail; 36px handle → `⌘B` / `Esc`; opening widens its grid track so the console column is pushed left; the panel is a fixed-height internal-scroll box). All scoped under `.sqlc` in `app.css`; `web/static/sqlconsole.js` wires CodeMirror + the drawer + write-mode + run sweep. |

---

## Module 11 — SQL Generation Engine

| # | Feature | Status | Notes |
|---|---------|:---:|-------|
| 11.1 | Template Library | 🟡 | SQL is composed deterministically from the registry with `psycopg.sql` in `ddl.py` (DDL) and `transform.py` (transform). Not a separate reviewed-template engine. |
| 11.2 | Registry Reader | 🟡 | `registry.get_table` / `get_columns` / `get_columns_meta`. No `schema_version` pinning. |
| 11.3 | Transform SQL Builder | 🟡 | Realised imperatively inside `transform.py`; the SQL is executed, not emitted or stored. |
| 11.4 | DDL Builder | ✅ | `ddl.staging_ddl` / `production_ddl` / `production_partition_ddl` / `quarantine_ddl` / `waiting_ddl`. |
| 11.8 | Read-only "Generated SQL" panel (web) | 🟡 | `web/sqlview.pipeline_sql` re-renders **just the production DDL + the production insert** from the shared `transform` / `ddl` builders (so it can't drift from what runs). Table page → "Generated SQL" card. Deliberately narrow: no staging/quarantine/waiting DDL, no COPY, no structural-filter SQL. |
| 11.5 | Regeneration Trigger | 📋 | No registry-commit subscription. |
| 11.6 | Generated SQL Store | 📋 | `generated_sql` not built. |
| 11.7 | Diff & Dry Run | 📋 | — |

---

## Next (Slice 1 finish)

1. ~~**CLI**~~ ✅ `src/odin/cli.py`.
2. ~~**Web UI**~~ ✅ `src/odin/web/` — design system + shell (htmx), home, onboard wizard, table page, live run log, waiting + quarantine review. `odin web`.
3. ~~**Background jobs**~~ ✅ `src/odin/jobs.py` — web extract/transform run off a single-worker thread; runs panel auto-polls.
4. **Web UI polish** (in-progress plan): (a) ✅ design system + shell, (b) ✅ background jobs + live runs, (c) ✅ htmx inline actions + confirm modals for waiting/quarantine resolve, (d) batch row-preview in a modal (still full-page `/waiting/{wbatch}` etc.), (e) drag-drop on table-page extract done — extend to the onboard upload.
5. **Adversarial test suite** (`tests/test_adversarial.py`) — spec §15 cases against `odin_test`: tab/newline in a field, NUL byte, empty required, negative measure, long compressible vs incompressible string in an indexed field, 100 %-bad batch, identical row twice, simulated mid-run crash (production unchanged, staging intact).

## Then

- **Slice 2 — RDBMS**: `extraction_control`, CURSOR / TIME_WINDOW / FULL batch planning, DB → CSV writer, marker advance. Reuses Modules 4/5/6/resolve unchanged.
- **Slice 3 — NoSQL**: document extract → flat file, then the same tail.
- **Orchestration / Monitoring / UI** deepen once the three ingest paths are in.
