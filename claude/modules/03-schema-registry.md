# Module 3: Schema Registry
**Owner:** DE + AE (Shared)
**Layer:** Cross-cutting — referenced by all modules
**Back to:** [claude.md](../claude.md)

---

## Purpose

The Schema Registry is the single source of truth for all structural expectations across the pipeline. Every data quality check, every extraction query, and every type cast in Production references this registry. It also drives the SQL Generation Engine (Module 11) — the generated transformation SQL is a downstream artifact of the registry, never hand-edited. It eliminates hardcoded schema definitions from individual pipelines.

---

## Key Design Decisions

- Centralized — one registry for all source systems
- Version-controlled — schema changes are tracked, not overwritten
- Referenced by both DE (validation) and AE (transformation)
- The registry is authoritative: verified changes to it trigger regeneration of the staging → production SQL (Module 11)
- Schema changes trigger automated alerts before they break pipelines
- **Write access: DE role only**, assignable per user (D3.1)
- **Registration is auto-inferred from the source, then reviewed and approved by DE** before the schema becomes ACTIVE (D3.3)
- A proposed type change is **verified against current production values** (D3.4)
- The registry defines each table's **`load_type`** (`FULL_SNAPSHOT` | `INCREMENTAL`) and, for incremental, its **`existence_check_column`** — the DE-chosen date column whose values are checked against production. `natural_key` (composite, ordered comma-list) drives `nk`-hash collision routing when set and supersedes `existence_check_column` — see §3.4a.
- The registry also holds each table's **extraction strategy** and **load recurrence** (set at onboarding)
- Every table carries system `load_date` / `load_timestamp` on every row, and is partitioned on `load_date`

---

## Features to Build

### 3.1 Source System Registration
- Each source system is registered with a unique identifier
- Stores connection metadata (type, location, owner)
- Status flag (ACTIVE, DEPRECATED, PAUSED)
- Registration runs through the **onboarding wizard** ([Module 10](10-self-service-interface.md) §10.1): connect → replicate structure → pull a **1,000-row sample** into staging → DE reviews the sample + auto-inferred schema, edits types / nullability / PK / natural key / cursor-or-window column → approves
- The DE then sets the load config (extraction strategy, window column + grain, recurring vs one-time) — the schema goes ACTIVE and the **first real run** follows
- Only DE-role users can write to the registry

**Physical targets & naming (as built, 2026-08-31).** `staging_target` /
`production_target` store the fully-qualified `schema.table` string. One **schema
per layer** — `staging` / `production` / `quarantine` / `waiting` — and the table
name is `naming.slug(table_name)` only, **no source prefix** (was
`staging_<source>_<table>` in `public`). `quarantine`/`waiting` targets are
derived from the same slug. Because there is no prefix, **table names are globally
unique**: `onboard_file_source` runs a `to_regclass` pre-flight
(`registry._assert_targets_absent`) and refuses a brand-new `(source_id,
table_name)` whose four layer tables already exist, naming the clash. Re-onboarding
an existing `(source_id, table_name)` stays idempotent (`ON CONFLICT DO UPDATE`).
`odin/naming.py` (`qname` renders `schema.table` injection-safe; a dot-less legacy
value resolves as a bare `public` identifier); migration `sql/004_layer_schemas.sql`.

### 3.2 Expected Column Names Store
- Per source system, per table
- Stores ordered list of expected column names
- Used by Check 1 (header validation)

### 3.3 Expected Data Types Store
- Per column, per table, per source system
- Maps source column to target Production data type
- Used by Check 2 (type casting validation)

### 3.4 Nullable Flags Store
- Per column — is NULL allowed or not
- Used by Check 2 at Staging → Production transition

### 3.4a Load Type & Collision Routing (per table)
- **`load_type`** — `FULL_SNAPSHOT` or `INCREMENTAL`, chosen by the DE at onboarding.
- **`existence_check_column`** — for `INCREMENTAL` only: a single date column. The transform compares its incoming values against production, exact value.
- **`natural_key`** — for `INCREMENTAL` only: an **ordered list of columns** (stored comma-separated) that together identify one row. **Supersedes `existence_check_column`** when set. Key columns must not be `numeric`/`unit_interval`-typed (their text form is not scale-canonical). See [Module 6](06-production-layer.md) for the hashed-key mechanism.
- **Routing** (base build, all slices), all set-based, never row-by-row:
  - `FULL_SNAPSHOT` — delete production's rows for this load's `load_date`, then insert. No existence column, no waiting pipeline.
  - `INCREMENTAL` + **`existence_check_column`** — existence value absent from production → insert; already present → those rows → `waiting.<table>`, one `waiting_batch_log` row per value.
  - `INCREMENTAL` + **`natural_key`** — a row whose composite key is already in production → `waiting.<table>`; new key → insert. **One `waiting_batch_log` row per run** (not per key). Matched by a hashed `nk bigint` column (indexed) with a raw-column tie-break.
- Every table also carries system `load_date` / `load_timestamp` on every row (stamped at load), and is partitioned on `load_date`.

### 3.4b Extraction Strategy & Recurrence (per table)
- `extraction_strategy` — CURSOR / TIME_WINDOW / FULL (see [Module 1](01-extraction-layer.md))
- `cursor_column` — for CURSOR (the monotonic column; also flagged `is_watermark` in `registry_columns`)
- `window_column` + `window_grain` — for TIME_WINDOW (the extraction window; distinct from `existence_check_column`, which is the collision unit)
- `settling_lag_minutes` — for TIME_WINDOW; default 0
- `load_recurrence` — RECURRING / ONE_TIME (chosen at the onboarding preview step)

### 3.5 Version Control for Schema Changes
- Every change to the registry creates a new version record
- Old versions are never deleted
- Pipelines reference a specific version or always-latest
- _Open:_ behaviour when a schema change is committed while a pipeline for that table is mid-run — see Open Questions (concurrent-run versioning)

### 3.6 Schema Change Alerting
- When a new file header doesn't match registered schema → alert raised
- When a source system adds or removes columns → alert raised
- Ties into Module 9 (Monitoring & Alerting)

### 3.7 Verified Schema Change → SQL Regeneration
- A column type change is proposed through the self-service interface (Module 10)
- The candidate type is **verified against current production values** — a Check-2 dry run (Module 4, §4.8) tests whether every value in the production column can cast to the new type cleanly (D3.4)
- On a clean verification, the new `target_data_type` is committed to `registry_columns` as a new version and written to `registry_change_log` with `change_type = TYPE_CHANGE`
- The commit triggers the SQL Generation Engine (Module 11) to re-emit that pipeline's staging → production SQL with the updated cast
- If the production table already holds data, Module 6 runs the online migration (partition-by-partition rewrite into a temp table, then atomic name swap), scheduled by Module 8 according to table size
- Subsequent loads are still validated staging-side by the normal Check 2 on every run, so future non-conforming values are caught there

---

## Core Table Structures

```sql
-- Source system registry
CREATE TABLE registry_sources (
    source_id           VARCHAR,
    source_name         VARCHAR,
    source_type         VARCHAR,   -- RDBMS, API, FILE, STREAM
    owner               VARCHAR,
    status              VARCHAR,   -- ACTIVE, PAUSED, DEPRECATED
    registered_date     DATE,
    last_updated        TIMESTAMP
)

-- Table registry per source
CREATE TABLE registry_tables (
    source_id                VARCHAR,
    table_name               VARCHAR,
    staging_target           VARCHAR,
    production_target        VARCHAR,
    partition_key            VARCHAR,   -- always 'load_date'
    -- collision routing (base build)
    load_type                VARCHAR,   -- FULL_SNAPSHOT | INCREMENTAL
    existence_check_column   VARCHAR,   -- INCREMENTAL only: date column checked against production
    -- extraction
    extraction_strategy      VARCHAR,   -- CURSOR, TIME_WINDOW, FULL
    cursor_column            VARCHAR,   -- for CURSOR
    window_column            VARCHAR,   -- for TIME_WINDOW
    window_grain             VARCHAR,   -- HOURLY, DAILY, ...  (= trigger interval)
    settling_lag_minutes     INTEGER,   -- for TIME_WINDOW; default 0
    load_recurrence          VARCHAR,   -- RECURRING, ONE_TIME
    -- later ("Configure"): row-level keying
    natural_key              VARCHAR,   -- comma-separated key columns; when set, INCREMENTAL routing is by hashed nk, not existence_check_column
    status                   VARCHAR,
    last_updated             TIMESTAMP
)

-- Column registry
CREATE TABLE registry_columns (
    source_id           VARCHAR,
    table_name          VARCHAR,
    column_name         VARCHAR,
    column_order        INTEGER,
    source_data_type    VARCHAR,
    target_data_type    VARCHAR,
    is_nullable         BOOLEAN,
    is_primary_key      BOOLEAN,
    is_watermark        BOOLEAN,
    schema_version      INTEGER,
    effective_from      DATE,
    effective_to        DATE       -- NULL means current
)

-- Schema change log
CREATE TABLE registry_change_log (
    change_id           VARCHAR,
    source_id           VARCHAR,
    table_name          VARCHAR,
    column_name         VARCHAR,
    change_type         VARCHAR,   -- ADD, REMOVE, TYPE_CHANGE, RENAME
    old_value           VARCHAR,
    new_value           VARCHAR,
    changed_by          VARCHAR,
    changed_timestamp   TIMESTAMP,
    schema_version      INTEGER
)
```

---

## How Other Modules Use the Registry

| Module | How It Uses Registry |
|--------|---------------------|
| Module 1 | Reads `extraction_strategy` + cursor/window columns to scope a run; column list feeds the generated query |
| Module 4 | Check 1 compares file headers against column names; Check 2 uses data types + nullable flags; `load_type` + `existence_check_column` drive collision routing to `waiting.<table>` |
| Module 6 | Production transform casts to target types, routes by `load_type` / `existence_check_column`, partitions on `load_date`; when `natural_key` is set, routing is by hashed `nk` + waiting divert |
| Module 9 | Schema drift detection compares incoming vs registered |
| Module 11 | Generates the scoping query, staging/production/quarantine/waiting DDL, and the full transform SQL from the registry; regenerates on verified change |

---

## Dependencies

- Module 4: Data Quality (Check-2 dry run for schema change verification)
- Module 6: Production Layer (online migration on verified type change)
- Module 9: Monitoring (for schema change alerting)
- Module 10: Self-Service Interface (for non-technical schema registration and type-change requests)
- Module 11: SQL Generation Engine (consumes the registry; regenerates SQL on verified change)

---

## Resolved

- Write access — DE role only, per-user assignable (D3.1)
- Registration — auto-infer from source, 1,000-row sample preview, DE approves (D3.3)
- Type-change verification target — current production values (D3.4)
- Collision routing is DE-configured: `load_type` (`FULL_SNAPSHOT` | `INCREMENTAL`) + `existence_check_column` (incremental only, exact date value). `natural_key` (composite) supersedes `existence_check_column` when set — built; route is nk-hash + waiting divert, still not an upsert
- Extraction strategy + recurrence held per table, set at onboarding
- System `load_date` / `load_timestamp` on every row; production partitioned on `load_date`

## Open Questions

- [ ] Concurrent-run schema versioning — when a change is committed mid-run, does the running pipeline (a) stay on the version it pinned at run start, (b) switch to latest under a write-lock, or (c) read from a copy-on-run snapshot? (rec: **a**, or **c** for the audit trail) — **awaiting decision**
