# Foundry Autonomous Pipeline Infrastructure
## Project Overview

This project designs and builds a fully autonomous, self-maintaining data pipeline infrastructure on **Palantir Foundry's Pipeline Builder**. The system is designed to be operated by both technical and non-technical users.

---

## Architecture Philosophy

- **Layered Architecture**: Staging → Production → Business
- **SQL-First**: All transformation logic written in SQL. Python is used *only* to move data from a source into staging.
- **DE + AE Split**: Clear separation between Data Engineering and Analytical Engineering responsibilities
- **Self-Maintaining**: Pipelines are automatically created, validated, and monitored once a source system is connected
- **Non-Technical Friendly**: Every layer has a self-service interface for business users
- **Per-source isolation**: one pipeline per source per stage — a failure, schema change, or load spike in one source never cascades into another. The only deliberately shared stage is the final Business-layer merge.
- **Single transform point**: all casting, typing, and structural validation happen in exactly one place — the staging → production transform. Never at extraction, never at load. Every check there is **set-based SQL over the whole batch — never row-by-row**.
- **DE-configured collision routing**: at onboarding the DE picks a **load type** and (for incremental) an **existence-check column** (a date column):
  - **`FULL_SNAPSHOT`** — the load is one dated snapshot. Production already holds rows for that `load_date` → delete them, insert the new snapshot; otherwise just insert. No existence-check column, no waiting pipeline.
  - **`INCREMENTAL`** — for each incoming value of the existence-check column: absent from production → insert those rows; already present (exact value) → those rows → the **waiting pipeline** for a human approve/reject.
- **Idempotency via one-transaction-per-run**: a failed transform rolls back every production write for that run, so a retried load re-inserts cleanly. Extraction never compares rows against the destination.
- **Two diversions, never a broken pipeline** — both per-table, each in its own schema:
  - `quarantine.<table>` — structurally bad rows (VARCHAR mirror of staging). Failure reason + counts recorded per batch in `quarantine_batch_log`.
  - `waiting.<table>` — colliding rows (exact copy of the production table). One `waiting_batch_log` row per colliding value.
- **No automatic retry or recovery.** Any failure stops and alerts; a DE fixes the cause and re-triggers. Nothing in `quarantine` or `waiting_pipeline` is ever auto-reprocessed — a re-run of a failure just fails again.
- **No CDC.** Change Data Capture is not used (costly, unneeded); rows are pulled as-is.

> The ingestion design (Modules 1, 4, 5, 6) follows [INGESTION_PIPELINE_SPEC.md](INGESTION_PIPELINE_SPEC.md) — a proven blueprint from a prior product. Its Postgres/Airflow mechanics are implementation details; the **shape** (staging buffer, single transform point, two diversion tables, existence-check routing, extract↔transform lock, run log) is what carries over. The base build routes by a DE-chosen existence-check column; the spec's row-level natural key is a later "Configure" option.

---

## Locked Vocabulary

| Term | What it is | Former (medallion) name |
|------|------------|-------------------------|
| **Source** | Origin system — RDBMS, NoSQL, or files | — |
| **Staging file area** | On-disk location where extracted batch files land before load | Landing zone |
| **Staging** | Loose, untyped retry buffer. Batch files loaded (via Python) into raw tables as VARCHAR. **Truncated after each confirmed production load.** | Bronze |
| **Production** | Cleaned, typed, business-rule-applied tables. SQL only. Consumption-grade. Partitioned by `load_date`. | Silver |
| **Business layer** *(syn. Aggregation layer)* | Cross-table joins, aggregations, metrics, Ontology output. SQL only. | Gold / Semantic |
| **Load type** | Per table, DE-set: `FULL_SNAPSHOT` (one dated snapshot; overwrites its own `load_date`) or `INCREMENTAL` (routed by the existence-check column). | — |
| **Existence-check column** | Per table (INCREMENTAL only), DE-chosen date column. An incoming value already in production (exact match) → those rows divert to the waiting pipeline. | — |
| **Quarantine** | Per-table table `quarantine.<table>` (own schema), a VARCHAR mirror of staging, for structurally invalid rows. Failure reason + counts per batch in `quarantine_batch_log`. Re-injected as a new batch once fixed. | — |
| **Waiting pipeline** | Per-table table `waiting.<table>` (own schema), an exact copy of the production table, holding colliding rows. One `waiting_batch_log` row per colliding existence value, awaiting a human approve/reject. | — |
| **Natural key** *(built, INCREMENTAL)* | An optional **ordered list of columns** (`registry_tables.natural_key`) that identify one row. Supersedes `existence_check_column`. A row whose composite key is already in production → `waiting.<table>`; new key → insert. Matched by a hashed `nk bigint` column (indexed) + a raw-column tie-break. Key columns can't be `numeric`-typed. | — |

Medallion terms (Bronze/Silver/Gold) are retired — do not use them going forward.

**Physical table naming (as built, 2026-08-31).** One **schema per layer**, and the
table name is the DE-given target-table name only — **no source prefix**:
`staging.<table>`, `production.<table>`, `quarantine.<table>`, `waiting.<table>`,
where `<table>` = `naming.slug(table_name)` (`"Customer Orders"` → `customer_orders`).
The source is registry metadata, not part of the physical name. Table names are
**globally unique** — onboarding refuses a name whose layer tables already exist.
For RDBMS sources (later) the platform stores source-db + source-table and the
DE-given target-table name becomes the physical name the same way. `<table>` /
`<t>` / `<tbl>` throughout these docs refer to this slugged name.

---

## Two Core Layers of Ownership

### Data Engineering (DE) Layer
Responsible for:
- Source connectivity
- Batch extraction
- File landing and pre-processing
- Staging ingestion
- Data quality enforcement
- Orchestration and monitoring

### Analytical Engineering (AE) Layer
Responsible for:
- Production transformation logic
- Business layer aggregations
- Join automation
- Business rule application
- Ontology output mapping

---

## Data Quality Checks

| Check | Layer | What It Validates |
|-------|-------|-------------------|
| Check 1 | Pre-Staging (file loads only) | Header names and column count match expected schema |
| Check 2 | Staging → Production | Structural integrity only — every text field casts to its declared type, in range, required fields present, strings within length. Failures → `quarantine`. |

Content is treated as truth and never second-guessed. The **one** content judgement is an incremental load whose existence-check value already exists in production → the waiting pipeline for human review.

---

## Modules

| # | Module | Owner | File |
|---|--------|-------|------|
| 1 | Extraction Layer | DE | [modules/01-extraction-layer.md](modules/01-extraction-layer.md) |
| 2 | File Pre-Processing | DE | [modules/02-file-preprocessing.md](modules/02-file-preprocessing.md) |
| 3 | Schema Registry | DE + AE | [modules/03-schema-registry.md](modules/03-schema-registry.md) |
| 4 | Data Quality | DE | [modules/04-data-quality.md](modules/04-data-quality.md) |
| 5 | Staging Layer | DE | [modules/05-staging-layer.md](modules/05-staging-layer.md) |
| 6 | Production Layer | AE | [modules/06-production-layer.md](modules/06-production-layer.md) |
| 7 | Business Layer | AE | [modules/07-business-layer.md](modules/07-business-layer.md) |
| 8 | Orchestration | DE | [modules/08-orchestration.md](modules/08-orchestration.md) |
| 9 | Monitoring & Alerting | DE + AE | [modules/09-monitoring-alerting.md](modules/09-monitoring-alerting.md) |
| 10 | Self-Service Interface | DE + AE | [modules/10-self-service-interface.md](modules/10-self-service-interface.md) |
| 11 | SQL Generation Engine | DE + AE | [modules/11-sql-generation-engine.md](modules/11-sql-generation-engine.md) |

---

## Working Docs

| Doc | Purpose |
|-----|---------|
| [INGESTION_PIPELINE_SPEC.md](INGESTION_PIPELINE_SPEC.md) | Proven ingestion blueprint from a prior product — the shape Modules 1/4/5/6 follow |
| [decisions.md](decisions.md) | Decision log — every locked answer from the planning sessions, plus TBD / deferred items |
| [BUILD_STATUS.md](BUILD_STATUS.md) | Feature-by-feature build progress for Modules 1–11 against the code |
| [frontend-inventory.md](frontend-inventory.md) | Catalogue of every screen, view, form, action, table column, and config setting for the self-service UI |

---

## Connector Taxonomy

Every source converges on the same tail: **flat file → Check 1 → load to staging**. Connectors differ only in how they produce the flat file.

| Connector | Head (produces the flat file) | Handles |
|-----------|-------------------------------|---------|
| **RDBMS connector** | Batch-query relational source → write CSV/TXT. Standalone CSV/TXT sources enter here too, skipping the query step. | MySQL, Postgres, and structured CSV/TXT files |
| **NoSQL connector** | Extract documents → flatten to flat file | MongoDB and similar |
| **Nested-file connector** | Parse + flatten → flat file | XML, JSON, Shapefile |

---

## Extraction Strategies

The DE picks one per table at onboarding, based on what the source offers. Extraction reads **only the source** — never staging or production.

| Strategy | Use when | How a run scopes its rows | Marker |
|----------|----------|---------------------------|--------|
| **CURSOR** | Source has a monotonic `id` / reliable `updated_at` | `WHERE cursor > last_watermark ORDER BY cursor LIMIT batch_size`, page to end | `last_watermark` |
| **TIME_WINDOW** | Source has a reliable event/business timestamp | fetch each window `[window_start, window_end)` aligned to the trigger interval; missed runs cover every gap window, oldest first | `last_window_end` |
| **FULL** | Neither exists (small reference tables) | pull everything every run | — |

Duplication is not prevented at extraction — a re-covered window or re-paged rows are absorbed at the production write (INCREMENTAL: an existence-check value already in production → those rows divert to the waiting pipeline; FULL_SNAPSHOT: the `load_date` is overwritten wholesale).

---

## Key Design Decisions

- All sources are extracted in **batches written to flat files** (~250 MB / 100k rows) in the staging file area — the source is never pulled fully into memory
- **Python** performs only the source → staging load; **all logic from staging onward is SQL**
- Staging holds only VARCHAR, has no constraints, and is **truncated after every confirmed production load** — it is a retry buffer, not a store
- A **Schema Registry** is the single source of truth for all validation and for the generated transformation SQL
- The **SQL Generation Engine** builds each pipeline's staging → production SQL once, deterministically, from the registry; verified schema changes regenerate it automatically — the generated SQL is never hand-edited
- The production write is **DE-configured** per table: `FULL_SNAPSHOT` overwrites its own `load_date`; `INCREMENTAL` inserts rows whose existence-check value is new and routes rows whose value already exists to the waiting pipeline. Every routing decision is one set-based SQL statement — never a row-by-row loop. Row-level upsert is a later per-table "Configure" option.
- Structurally bad rows → the per-table `quarantine.<table>` (re-inject as a new batch once fixed); pipelines never break on bad data
- **Production tables are partitioned by ingestion date.** A verified column type change is applied by rewriting the table partition-by-partition into a temp table, then swapping names atomically
- A **Task Scheduler** registers every pipeline (recurring or one-time) with its schedule and status when its source is onboarded
- Non-SQL pre-processing (XML, JSON, Shapefile flattening) happens before Staging as an isolated module

---

## Status

> **Slice 1 (CSV/TXT) build in progress.** onboard → extract → transform → resolve are coded and verified end-to-end against local Postgres (`odin` / `odin_test`). See [BUILD_STATUS.md](BUILD_STATUS.md) for feature-level progress. Next: CLI, web UI, adversarial test suite. Then Slice 2 (RDBMS), Slice 3 (NoSQL).
