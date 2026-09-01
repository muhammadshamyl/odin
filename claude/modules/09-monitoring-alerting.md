# Module 9: Monitoring & Alerting
**Owner:** DE + AE (Shared)
**Layer:** Cross-cutting
**Back to:** [claude.md](../claude.md)

---

## Purpose

Provides full observability across the pipeline infrastructure. Tracks execution status, data quality trends, schema drift, and SLA adherence. Raises alerts before problems become failures. Feeds the self-service dashboard in Module 10.

---

## Key Design Decisions

- All monitoring data stored in SQL tables — queryable by anyone
- Alerts are threshold-based and configurable
- Schema drift detection runs automatically on every ingestion
- No silent failures — every anomaly is logged and surfaced

---

## Features to Build

### 9.1 Run Log
- **Extract** writes one row per batch (`(run_id, batch_id)`); the **transform** writes one row per run (`batch_id = NULL`) — the set-based transform has no chunks. `resolve` actions (approve/reinject) also write a row.
- **Written on a separate connection that commits immediately** — so a failed transform still records how far it got, surviving the whole-run rollback (Module 6 §6.5).
- A DAG-level `on_failure_callback` is the safety net for failures outside the normal path (lock acquisition, the final truncate).
- Provenance is carried by `batch_id` threaded through every table (source → staging → production / waiting / quarantine).

**As built (Slice 1, 2026-08-31).**
- `run_log` is written by `odin/runlog.py` (`new_run_id` / `start` / `finish`) on a
  **separate autocommit connection**, so a rolled-back transform still records how
  far it got.
- **Extract now writes ONE run-level `EXTRACT` row per `run_extract` call**
  (`running` → `success` / `failed`), not one per COPY batch — a file that never
  passed landing / Check 1 / an empty body is still visible. The "N×100k" hint in
  the Monitor is derived from `ceil(rows_processed / batch_rows)`.
- **Empty-staging transform is a clean no-op**: a `success` / 0-row `run_log` row,
  not a silent fake success.
- **The Monitor and the table page render one row per `run_id`**
  (`app._recent_runs`, `GROUP BY run_id` with `bool_or` / `FILTER` rollups →
  `extract_state` / `transform_state` ∈ `success | running | failed | None`, summed
  row counts). `stage_pill` renders complete / running / failed / —. So an `ingest`
  run shows both stage pills; a standalone `transform` shows `Extract —`. The
  `odin runs` CLI stays at per-row grain (debug view).
- **Self-refreshing UI partials**, all keyed on one body event `odin:runs-changed`
  (fired by the extract / transform / load POSTs) plus an `every Ns` tick while a
  job for that table is active:
  `_runs_panel.html` (the run log), `_lineage.html` + `GET /partials/lineage/…`
  (animated pipeline diagram — every node glows for its state, including the
  **Quarantine / Waiting** boxes, which pulse amber / blue while a transform runs
  and stay lit while their batch table holds rows). An RDBMS-backed table gets
  `_lineage_rdbms.html` instead (same route, chosen when
  `registry.get_rdbms_source` returns a row): the chain gains two nodes —
  **Source DB → extract (batched pull) → CSV batch file → Staging → transform →
  Production** — with the DB / extract / CSV nodes all bound to `state.extract`
  (the CSV is written inside the extract run). And
  `_data_stats.html` + `GET /partials/stats/…` (the table page's
  staging / production / waiting / quarantine KPI cards — previously static, so a
  run left them stale until a full page reload).
- **Background-job failures surface**: `jobs.recent_failed(120s)` → red banner in
  the runs panel (job state is in-memory, lost on `--reload`).
- `pipeline_health_summary` view + `alert_config` / `schema_drift_log` /
  `freshness_config` / `table_size_metadata` are **not built yet** (Slice 1 scope).

```sql
CREATE TABLE run_log (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dag_id         VARCHAR NOT NULL,
    run_id         VARCHAR NOT NULL,
    batch_id       VARCHAR,          -- NULL for the transform / quiet cycle
    stage          VARCHAR,          -- EXTRACT, STAGING, PRODUCTION, BUSINESS, MIGRATION
    source_id      VARCHAR,
    table_name     VARCHAR,
    started_at     TIMESTAMP,
    ended_at       TIMESTAMP,
    status         VARCHAR,          -- running, success, failed
    rows_processed INTEGER,
    rows_to_production INTEGER,
    rows_to_waiting    INTEGER,
    rows_quarantined   INTEGER,
    error_message  VARCHAR,
    triggered_by   VARCHAR           -- scheduled, backfill, manual, onboarding
)
```

### 9.2 Failed Job Alerting
- Alert raised immediately on pipeline failure
- Alert includes: pipeline name, stage, error message, timestamp
- Configurable alert channels (email, Slack, etc.)
- Alert suppression for known/accepted failures

```sql
CREATE TABLE alert_config (
    alert_id            VARCHAR,
    pipeline_name       VARCHAR,
    alert_type          VARCHAR,   -- FAILURE, SLA_BREACH, QUARANTINE_SPIKE, WAITING_BACKLOG, SCHEMA_DRIFT
    threshold_value     VARCHAR,
    alert_channel       VARCHAR,
    recipients          VARCHAR,
    is_active           BOOLEAN
)
```

### 9.3 Quarantine & Waiting-Pipeline Volume Monitoring
- Quarantine rate per source per run; alert when it exceeds the threshold (pause the pipeline at 5% — D4.1)
- **Waiting-pipeline backlog**: count of `waiting_batch_log` rows with `status = 'pending'` per source/table, and oldest-pending age; alert when the backlog or age exceeds a threshold (collisions need human attention)
- Trend analysis on both to catch gradual data-quality drift

```sql
-- Quarantine rate view
SELECT
    r.source_system,
    r.table_name,
    DATE(r.started_at)                                       AS run_date,
    SUM(r.rows_quarantined)                                  AS quarantined_records,
    SUM(r.rows_processed)                                    AS total_records,
    ROUND(SUM(r.rows_quarantined) * 100.0
          / NULLIF(SUM(r.rows_processed), 0), 2)             AS quarantine_rate_pct
FROM run_log r
WHERE r.stage = 'PRODUCTION'
GROUP BY 1, 2, 3
```

### 9.4 Schema Drift Detection
- On every ingestion, incoming file headers compared to Schema Registry
- Any new column, removed column, or renamed column flagged as drift
- Drift logged and alert raised before pipeline proceeds

```sql
CREATE TABLE schema_drift_log (
    drift_id            VARCHAR,
    source_system       VARCHAR,
    table_name          VARCHAR,
    detected_timestamp  TIMESTAMP,
    drift_type          VARCHAR,   -- NEW_COLUMN, REMOVED_COLUMN, RENAMED_COLUMN, TYPE_CHANGE
    column_name         VARCHAR,
    expected_value      VARCHAR,
    actual_value        VARCHAR,
    resolution_status   VARCHAR,   -- OPEN, ACKNOWLEDGED, REGISTRY_UPDATED
    resolved_by         VARCHAR
)
```

### 9.5 Data Freshness Monitoring
- Tracks when each Production and Business layer table was last successfully updated
- Alerts when a table exceeds its expected refresh frequency
- Configurable freshness SLA per table

```sql
CREATE TABLE freshness_config (
    table_name          VARCHAR,
    layer               VARCHAR,   -- PRODUCTION, BUSINESS
    expected_frequency  VARCHAR,   -- HOURLY, DAILY, WEEKLY
    max_delay_minutes   INTEGER,
    alert_on_breach     BOOLEAN
)
```

### 9.6 SLA Breach Alerting
- Tracks expected vs actual completion time per pipeline
- Alerts when pipeline exceeds SLA window
- Historical SLA adherence reportable

### 9.7 Execution Logs
- Full verbose log per pipeline run, linked to the `run_log` row(s)
- Queryable by `run_id`, `dag_id`, source/table, date range
- Retained for a configurable number of days

### 9.8 Table Size Metadata
- Tracks row count and byte size per staging and production table
- Refreshed on every load
- Consumed by Module 8 to schedule the online schema migration (large tables deferred, small tables run immediately)

```sql
CREATE TABLE table_size_metadata (
    table_name          VARCHAR,
    layer               VARCHAR,   -- STAGING, PRODUCTION
    row_count           BIGINT,
    size_bytes          BIGINT,
    partition_count     INTEGER,
    measured_timestamp  TIMESTAMP
)
```

---

## Monitoring Summary View

```sql
CREATE VIEW pipeline_health_summary AS
SELECT
    source_system,
    table_name,
    MAX(CASE WHEN stage = 'EXTRACT'    THEN status END)   AS extract_status,
    MAX(CASE WHEN stage = 'STAGING'    THEN status END)   AS staging_status,
    MAX(CASE WHEN stage = 'PRODUCTION' THEN status END)   AS production_status,
    MAX(CASE WHEN stage = 'BUSINESS'   THEN status END)   AS business_status,
    MAX(ended_at)                                         AS last_successful_run,
    SUM(rows_to_production)                               AS rows_to_production,
    SUM(rows_to_waiting)                                  AS rows_to_waiting,
    SUM(rows_quarantined)                                 AS total_quarantined
FROM run_log
WHERE DATE(started_at) = CURRENT_DATE
GROUP BY 1, 2
```

---

## Dependencies

- All modules feed execution data into this module
- Module 3: Schema Registry (baseline for drift detection)
- Module 4: Data Quality (quarantine data source)
- Module 6: Production Layer (migration run status)
- Module 8: Orchestration (consumes table size metadata to schedule migrations)
- Module 10: Self-Service Interface (displays monitoring data)

---

## Open Questions

- [ ] What are the alert channels available in the target environment?
- [ ] How long do execution logs need to be retained?
- [ ] Who receives alerts — DE team only or business owners as well?
- [ ] What quarantine rate % triggers a pipeline pause vs just an alert?
