# Module 1: Extraction Layer
**Owner:** Data Engineering (DE)
**Layer:** Pre-Staging
**Back to:** [claude.md](../claude.md)

---

## Purpose

Connect to a source, pull data in controlled batches, and write CSV/TXT files to the staging file area. Extraction reads **only the source** — never staging, never production — and is never required to be exactly-once; a re-covered range is absorbed downstream at the production write (INCREMENTAL: rows whose existence-check value already exists → the waiting pipeline; FULL_SNAPSHOT: the `load_date` is overwritten).

---

## Key Design Decisions

- One extraction pipeline **per source per table** — isolated from every other (per-source isolation)
- All extractions output **CSV or TXT flat files**, regardless of source type
- **Batch-based** — never a full table pull into memory. Default batch: **~250 MB target, capped at 100,000 rows**; global default, overridable per source (D1.1)
- **No file compression** — files sit in our own storage and are processed in place (D1.2)
- **Python** performs the source → file → staging load; every step after staging is SQL
- Extraction **never reads staging or production**. It scopes rows using only the source and a per-table marker it keeps itself
- Three connectors share one tail (flat file → Check 1 → load to staging) and differ only in how they produce the file:
  - **RDBMS connector** — batch-query MySQL/Postgres → write CSV/TXT. Standalone CSV/TXT sources enter here too, skipping the query step.
  - **NoSQL connector** — extract MongoDB (and similar) documents → flatten to flat file
  - **Nested-file connector** — parse + flatten XML/JSON/Shapefile → flat file (see Module 2)
- **No CDC.** Change Data Capture is not used — it is costly to run and the pipeline does not need per-row operation flags. Rows are pulled as-is.

---

## Extraction Strategies

The DE picks one per table at onboarding (stored in `registry_tables.extraction_strategy`), based on what the source offers.

| Strategy | Use when | How a run scopes its rows | Marker advanced on success |
|----------|----------|---------------------------|-----------------------------|
| **CURSOR** | Source has a monotonic `id` or a reliable `updated_at` | `WHERE cursor > last_watermark ORDER BY cursor LIMIT batch_size`; page until the source returns nothing | `last_watermark` = highest cursor value seen |
| **TIME_WINDOW** | Source has a reliable event/business timestamp column | for each window `[window_start, window_end)` (size = the trigger interval), `WHERE window_column >= window_start AND window_column < window_end`; if runs were missed, cover **every** gap window, oldest first | `last_window_end` = end of the last window fully extracted |
| **FULL** | Neither a cursor nor a usable timestamp (small reference/lookup tables) | pull everything, every run | — |

- **No `OFFSET`** anywhere — CURSOR pages on `cursor > last`, TIME_WINDOW filters on the window bounds.
- **FULL paging without assuming a key:** a **file** source is read in fixed row-count batches (no key needed). A **DB table** with no ordering column registered is read in a single streamed pass (server-side cursor). If a DB table is too large to stream and the DE has registered no ordering column, that is an **onboarding item for the DE** — the system never picks one.
- **Settling lag** (TIME_WINDOW): default `0` — fetch right up to the current boundary. Configurable per source (`settling_lag`) if a lagging source needs a margin. Rows that arrive after their window closed restate → the waiting pipeline (INCREMENTAL).
- A **large** table with no cursor and no timestamp is an **onboarding blocker**, not a silent forever-FULL.
- Crash-safe: on failure the marker is not advanced, so the next run re-covers the same range; re-covered rows whose existence-check value is already in production divert to `waiting.<tbl>` (or, if the failed run wrote nothing, a clean retry inserts them).

---

## Features to Build

### 1.1 Batch Extraction Engine
- Produces a **batch plan** for the run: the ordered list of pages/windows to pull, each ≤ ~250 MB / ≤ 100k rows
- CURSOR → pages on the cursor; TIME_WINDOW → one entry per window in scope; FULL → file read in fixed row-count batches, or DB table streamed in one pass (no key assumed — see Extraction Strategies)

### 1.2 Batch Size Configuration
- Per-table config in `extraction_control` (`batch_size`)
- Default ~250 MB / 100k rows; overridable per source and per run

### 1.3 Extraction Scheduling
- Owned by Module 8 (Orchestration) and the Task Scheduler
- Recurring: interval set at onboarding — for TIME_WINDOW the interval **is** the window size
- One-time: no schedule; runs once and goes idle (definition kept, status INACTIVE)

### 1.3a Failure Model — no automatic retry
- Any failure (source unreachable, extraction error, file → staging load error) → **stop, alert, wait for the DE**. The system does not auto-retry.
- Rationale: a failure that isn't transient will just fail again; a DE looks at it, fixes the cause, and re-triggers. The next scheduled run also naturally re-attempts an un-advanced marker.
- On the next successful run the un-advanced marker means the same range is re-covered; INCREMENTAL rows whose existence-check value is already in production divert to the waiting pipeline.

### 1.4 Flat File Writer
- CSV or TXT, always a header row, no compression
- Name: `{source_system}_{table_name}_{batch_id}_{timestamp}.csv`
- Path: `/{source_system}/{table_name}/{run_date}/` in the staging file area (managed by Module 2)
- Verify each file: rows written = rows read, header present, not empty

### 1.5 RDBMS Connector
- MySQL, PostgreSQL, SQL Server, Oracle, etc., via JDBC / Foundry Data Connection
- The scoping query (CURSOR / TIME_WINDOW / FULL) is generated from the registry by Module 11
- **Standalone CSV/TXT sources are handled here** — already in output format, they skip the query and go straight to the shared tail

### 1.6 NoSQL Connector
- MongoDB and similar document stores; REST / streaming sources
- Extracts in batches and flattens to flat-file shape before landing
- Paginated APIs handled here

### 1.6a Nested-File Connector
- XML, JSON, Shapefile — parsing/flattening done by Module 2 before the file enters staging

### 1.7 Onboarding Sample Extraction
- On first connection, pull the **top 1,000 rows** only, into staging, for the preview (Module 10 wizard)
- The DE reviews the sample + inferred schema, approves, and sets the load config (strategy, window column/grain, recurring vs one-time)
- The **first real run** (full first batch) happens only after that approval — see [Module 10](10-self-service-interface.md) §10.1

---

## Control Table Structure

```sql
CREATE TABLE extraction_control (
    source_system        VARCHAR,
    table_name           VARCHAR,
    extraction_strategy  VARCHAR,   -- CURSOR, TIME_WINDOW, FULL
    load_recurrence      VARCHAR,   -- RECURRING, ONE_TIME
    batch_size           INTEGER,
    cursor_column        VARCHAR,   -- for CURSOR
    last_watermark       VARCHAR,   -- for CURSOR
    window_column        VARCHAR,   -- for TIME_WINDOW
    window_grain         VARCHAR,   -- HOURLY, DAILY, ... (= trigger interval)
    settling_lag_minutes INTEGER,   -- for TIME_WINDOW; default 0
    last_window_end      TIMESTAMP, -- for TIME_WINDOW
    last_run_timestamp   TIMESTAMP,
    status               VARCHAR    -- ACTIVE, PAUSED, FAILED, INACTIVE
)
```

---

## Execution Phases

**Precondition** (set once per source+table): source, tables, columns registered and DE-approved; `extraction_control` row exists with a strategy; scoping query template generated by Module 11.

### Phase 1 — Trigger & claim
- Orchestration / Task Scheduler fires the pipeline (schedule / event / dependency / manual / onboarding first-run)
- Read `extraction_control`; if status ≠ ACTIVE → stop
- Take the per-table **extract lock** (try-lock); if Module 6's transform holds it → stop the cycle, retry next tick
- Open a `run_log` row: stage = EXTRACT, status = RUNNING
- Resolve parameters: `run_date`, strategy, batch size

### Phase 2 — Connect
- Pick the connector by source type; open the connection (file sources: locate the file(s))
- Connectivity fails → mark run FAILED, alert, stop (no auto-retry — §1.3a)

### Phase 3 — Scope the run (build the batch plan)
- **CURSOR** → plan = pages of `cursor > last_watermark`
- **TIME_WINDOW** → plan = every window from `last_window_end` to `now − settling_lag`, aligned to `window_grain`, oldest first
- **FULL** → file read in fixed row-count batches; DB table streamed in one pass. No ordering key assumed.
- Query built from the registry by Module 11; file sources skip this

### Phase 4 — Extract & write, one batch at a time
For each batch in the plan:
1. Pull only that batch from the source (DB connectors run the scoped query; a **CSV/TXT source is already a file** — the loader reads its body in row-batches, no re-write needed)
2. Write the CSV/TXT file to the staging file area (§1.4); verify it (rows written = rows read, header present, not empty)
3. Log the batch: `batch_id`, path, `row_count`, `size_bytes`, and the batch's high cursor / window end
4. Batch fails → stop the run FAILED (keep the batches already written), alert, wait for the DE (§1.3a)

### Phase 5 — Finalize (only if every batch succeeded)
- Advance the marker: `last_watermark` (CURSOR) or `last_window_end` (TIME_WINDOW). FULL has no marker.
- Set `last_run_timestamp`; close `run_log`: status = SUCCESS, `rows_processed` = total
- Register each file in `staging_file_control` as PENDING → hands off to Module 2 / Check 1
- Emit "extraction done" → downstream stages released
- One-time load: set `extraction_control.status = INACTIVE`

### Phase 6 — On failure
- Run marked FAILED; `run_log` records how far it got; alert raised with the `run_id`
- Marker **not** advanced — so once the DE has fixed the cause, the next run (scheduled or manually triggered) re-covers the same range
- No automatic retry. On re-cover, INCREMENTAL rows whose existence-check value is already in production divert to `waiting.<tbl>`; a run that wrote nothing before failing leaves the value absent, so the retry inserts cleanly

---

## Dependencies

- Module 2: File Pre-Processing (owns the staging file area; picks up the files)
- Module 3: Schema Registry (strategy, cursor/window columns, column list)
- Module 8: Orchestration + Task Scheduler (scheduling, the extract lock, missed-run catch-up)
- Module 9: Monitoring (extraction run status)
- Module 11: SQL Generation Engine (builds the scoping query from the registry)

---

## Resolved

- Batch size default — ~250 MB / 100k-row cap, per-source overridable (D1.1)
- File compression — none (D1.2)
- **No automatic retry anywhere** — any failure → stop, alert, DE fixes the cause and re-triggers (supersedes D1.3 / D1.3b)
- **No CDC** — costly to run, not needed; rows pulled as-is (supersedes D6.1)
- Extraction strategy — per-table CURSOR / TIME_WINDOW / FULL, DE picks at onboarding
- FULL paging assumes no key: file → row-count batches; DB table → single streamed pass
- **PK is optional** — registered by the DE only when needed; nothing infers or requires one
- Extraction never reads staging or production; collision handling is at the production write (load-type routing)
- Onboarding: 1,000-row sample → preview → approve → first real run

## Open Questions

- [ ] Large table with no cursor and no timestamp — confirm this is an onboarding blocker (no silent forever-FULL)
