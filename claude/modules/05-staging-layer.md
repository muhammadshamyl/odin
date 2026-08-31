# Module 5: Staging Layer
**Owner:** Data Engineering (DE)
**Layer:** Staging
**Back to:** [claude.md](../claude.md)

---

## Purpose

Staging is a **loose, untyped retry buffer** between extraction and the transform. Batch files that pass Check 1 are bulk-loaded here as text. The transform (Module 6) reads all of staging, writes production, and then **truncates staging**. It holds at most one cycle's worth of data. It is not a store and it is not queried by consumers.

---

## Key Design Decisions

- Every source-supplied field is **VARCHAR / TEXT** — no constraints, no type enforcement, no indexes
- **Ephemeral** — `TRUNCATE`d by Module 6 **only after that run's production load is confirmed successful**, never on "attempted"
- **Reused every cycle** — never renamed, swapped, or recreated
- **Retry buffer** — if the transform fails, the next run retries straight from staging without re-hitting the source
- **Python performs the load** (batch file → staging table) via bulk COPY, never row-by-row; every step after this is SQL
- A **file → staging load failure raises an alert and does not retry** — the file is left for manual re-submission (D1.3)
- Every row carries ingestion metadata columns
- **Not partitioned** — partitioning applies only to production tables (D5.2)
- Idempotency and dedup are **not** enforced here — collisions are handled downstream by load-type routing at the production write. A constraint here would only ever catch intra-cycle duplicates.

---

## Features to Build

### 5.1 Raw Load (Python)
- Picks up PENDING files from `staging_file_control` (Module 2)
- Bulk-loads every column as VARCHAR into the staging table
- Appends the standard metadata columns
- Guarded by the per-table **extract↔transform lock** (see §5.6): the load takes it as a try-lock around the write; if the transform holds it, the whole load cycle stops and retries next tick

### 5.2 VARCHAR Standardization
- Values loaded exactly as they appear in the file; NULLs preserved as-is
- No transformation of any kind at this layer

### 5.3 Metadata Columns Added at Staging

```sql
load_date             DATE,        -- system date the row was loaded (also the production partition column)
load_timestamp        TIMESTAMP,   -- exact system time the row was loaded
source_file_id        VARCHAR,     -- which file it came from
batch_id              VARCHAR,     -- which extraction batch (provenance key)
source_system         VARCHAR,
staging_record_id     VARCHAR      -- unique id assigned at load
```

`load_date` / `load_timestamp` are stamped by the system on every row at load time — they record *when the table was loaded*, independent of any business date in the data. `load_date` carries through to production as its partition column.

### 5.4 Diversion Hand-off
- Files failing Check 1 never reach staging → `staging_file_control` FAILED — Module 4 §4.1
- Rows failing Check 2 (structural) never reach production → `quarantine.<tbl>` — Module 4/6
- INCREMENTAL rows whose existence-check value is already in production → `waiting.<tbl>` — Module 4/6

### 5.5 New Source Column Handling
- A file header carrying a column not in the registry is a **schema-drift** event
- The load is **held** (not silently ingested); a drift alert is raised (Module 9)
- DE updates the registry (new schema version); once approved, the load proceeds (D5.3)

### 5.6 Concurrency With the Transform
- Staging is shared by the loader (Stage 1) and the transform (Stage 2, which truncates it)
- One **per-table lock key** (Foundry equivalent of a Postgres advisory lock):
  - **Loader** — try-lock per batch, around the staging write only; not acquired → stop the cycle, retry next tick
  - **Transform** — blocking lock, held for the entire read → process → write → truncate sequence, so the loader cannot insert rows between the transform's read and its truncate

---

## Staging Table Structure

```sql
CREATE TABLE staging.erp_transactions (
    -- all source columns as VARCHAR
    transaction_id      VARCHAR,
    customer_id         VARCHAR,
    amount              VARCHAR,
    transaction_date    VARCHAR,
    status              VARCHAR,
    -- ...

    -- metadata
    staging_record_id   VARCHAR,
    load_date           DATE,
    load_timestamp      TIMESTAMP,
    source_file_id      VARCHAR,
    batch_id            VARCHAR,
    source_system       VARCHAR
)
-- no constraints, no indexes
```

Naming: `staging_{source_system}_{table_name}`.

---

## Lifecycle Per Cycle

```
extraction writes batch files
        ▼
Python bulk-loads PENDING files → staging table   (try-lock per batch)
        ▼
Module 6 transform: blocking lock → set-based structural filter → route by load_type → write production
        ▼
production load confirmed  →  TRUNCATE staging  →  release lock
        │
   (transform failed)  →  staging left intact  →  next run retries from staging
```

---

## Dependencies

- Module 1: Extraction Layer (produces the batch files)
- Module 2: File Pre-Processing (owns the staging file area; `staging_file_control`)
- Module 3: Schema Registry (staging table DDL)
- Module 4: Data Quality (Check 1 gate; diversions)
- Module 6: Production Layer (reads staging, then truncates it)
- Module 8: Orchestration (schedules the load; the lock)
- Module 11: SQL Generation Engine (staging DDL generated from the registry)

---

## Resolved

- Staging is **ephemeral** — truncated after each confirmed production load; it is a retry buffer, not a store (supersedes the earlier append-only / snapshot model, and removes D5.1)
- Not partitioned; production only, by ingestion date (D5.2)
- New source column — drift alert + hold load, DE updates registry (D5.3)
- No dedup/uniqueness at staging — collisions handled at the production write by load-type routing

## Open Questions

- [ ] None outstanding for this module.
