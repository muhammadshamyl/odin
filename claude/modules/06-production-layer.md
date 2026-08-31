# Module 6: Production Layer
**Owner:** Analytical Engineering (AE)
**Layer:** Production
**Back to:** [claude.md](../claude.md)

---

## Purpose

Production is the typed, partitioned correctness boundary. The **staging → production transform** is the single point where all casting, structural validation, collision routing, and business rules happen. It reads all of staging with **set-based SQL** (never row-by-row), writes production / `quarantine.<tbl>` / `waiting.<tbl>`, and then truncates staging — as **one transaction per run**.

---

## Key Design Decisions

- All transformation logic is SQL, generated once per pipeline by Module 11 from the registry — never hand-written
- **This is the only place** anything is cast or structurally validated (per the ingestion spec)
- Runs **at production's grain** (hourly if production is one row per entity per hour); running more often just re-touches a still-forming bucket
- Holds the per-table **blocking lock** for the whole run (read → process → write → truncate), so the loader can't insert into staging mid-run
- The production write is **DE-configured** (base build):
  - `FULL_SNAPSHOT` — delete production's rows for this load's `load_date`, then insert every surviving row. No existence column, no waiting pipeline.
  - `INCREMENTAL` — rows whose `existence_check_column` value is already in production (exact match) → `waiting.<tbl>` (one `waiting_batch_log` row per value); the rest → insert.
- A composite `natural_key` (hashed `nk`, waiting divert) is built (§6.2); row-level `ON CONFLICT` upsert is still deferred
- Every row carries system `load_date` / `load_timestamp`; `load_date` is the partition column
- **Production tables are partitioned by `load_date`** — fixed (D5.2 / D6.6)
- **No CDC, no row-level deletes** in the base build — `restated` is set when an approved waiting batch replaces rows for an existence value
- **Business rule approval:** AE signs off standardization rules; derived / computed logic also needs business sign-off (D6.3)
- Whole run is one transaction — a crash rolls back all production / quarantine / waiting writes for that run; the per-chunk run log is written on a separate connection so it survives (Module 9)

---

## Features to Build

### 6.1 Run Setup
- Acquire the per-table blocking lock (auto-releases on commit/rollback)
- The transform is a sequence of **set-based SQL statements** over the whole staging table — no Python row loop, no per-chunk streaming. The lock guarantees nothing writes staging during the run.

### 6.2 The transform steps

1. **Check 2 — structural filter** — one `WHERE` predicate built from the registry (`char_length(col) > :text_cap`; `NOT NULL` columns empty; would-not-cast once types are configured). Matching rows → `quarantine.<tbl>` with a `qbatch_id`, then deleted from staging; one `quarantine_batch_log` row per write.
2. **Business rules** — active `quality_rules` applied to the survivors (base build: none).
3. **Route by `load_type`:**
   - `FULL_SNAPSHOT` — ensure the `load_date` partition exists → `DELETE FROM production.<t> WHERE load_date = :load_date` → `INSERT ... SELECT` all survivors.
   - `INCREMENTAL` + `existence_check_column` — `SELECT DISTINCT E::text, count(*) FROM staging s WHERE EXISTS (SELECT 1 FROM production.<t> p WHERE p.E::text = s.E::text) GROUP BY 1`; for each colliding value, insert its rows into `waiting.<tbl>` + a `waiting_batch_log` row, delete them from staging; insert the survivors into production.
   - `INCREMENTAL` + `natural_key` — **composite key, set-based, no per-value loop** (`transform._load_incremental_nk`). Production and `waiting.<tbl>` carry a hashed `nk bigint` column (btree-indexed):

     ```
     nk = hashtextextended(concat_ws(chr(31),
              coalesce((col1::type1)::text, chr(1)),
              coalesce((col2::type2)::text, chr(1)), …), 0)::bigint
     ```

     `collides = EXISTS (SELECT 1 FROM production.<t> p WHERE p.nk = <staging nk> AND <raw-column tie-break>)`. Two statements: (1) `INSERT INTO waiting.<tbl> SELECT …, <nk> FROM staging WHERE collides` + **one** `waiting_batch_log` row for the whole run (`row_count = N`, `existence_value = "natural key: <cols>"`); (2) `DELETE FROM staging WHERE collides`. Then the survivors → production, `nk` populated by the same expression. The tie-break (`col::type IS NOT DISTINCT FROM col::type` per key column) runs only on rows that already share an `nk`, so a 64-bit hash collision cannot mis-route. Key columns cannot be `numeric`/`unit_interval`-typed.

### 6.3 Business Rule Application
- Standardization (e.g. `UPPER(TRIM(status))`) and universal derived columns, from the `quality_rules` / business-rules config — not hardcoded
- Applied to the surviving rows before the production write, in the same set-based `SELECT`
- Approval: standardization → AE; derived / computed logic → AE + business sign-off (D6.3)

### 6.4 Writing the Rows
- **To production** — one `INSERT INTO production.<t> (...) SELECT ... FROM <staging>` (after the diverted rows have been deleted from staging). No `ON CONFLICT` in the base build.
- **To `waiting.<tbl>`** — `existence_check_column`: one `INSERT ... SELECT` per colliding value + one `waiting_batch_log` row per value. `natural_key`: a single `INSERT ... SELECT` for the whole run + one `waiting_batch_log` row for the run.
- An approved waiting batch re-inserts the held rows with `restated = TRUE`, first deleting the production rows it supersedes — by `existence_value` (legacy) or by `nk` + tie-break (natural key).

### 6.5 End of Run
- `TRUNCATE staging` after the routing statements succeed, then commit — which releases the lock
- A crash partway rolls back all production / waiting / quarantine writes from the run — and does **not** auto-retry; the DE re-triggers after fixing the cause
- The run-log row is written on a separate connection that commits immediately (Module 9) so a failed run still records how far it got

### 6.6 Schema Enforcement
- Column names and types must match the registry; any deviation fails the run and alerts
- `NOT NULL` and primary key on a production column come **only** from what the DE explicitly registered — nothing is inferred
- No ad-hoc columns in production

### 6.7 Online Schema Migration (Column Type Change)
Applies a type change already verified (Module 4 §4.9) and committed to the registry (Module 3 §3.7). Dedicated process, separate from normal runs.

1. **Size check** — target table row count / bytes from Module 9 metadata
2. **Schedule** — Module 8 times the rewrite by size (large → low-activity window; small → immediately)
3. **Rewrite into a temp table** — `production.<t>__migrate` with the new column type; copy **partition by partition**, never the whole table at once
4. Loads are **not** frozen — the delta that lands after a partition is copied is reconciled after the swap (D6.5)
5. **Atomic swap** — all partitions written and verified → swap names in one cutover
6. **Delta reconciliation** — periods that landed post-copy in the old table are re-applied to the new live table
7. **Old table** — kept as `production.<t>__pre_migration_{timestamp}` for a safety window, then auto-dropped (window TBD — see Open Questions)

Module 11 has already re-emitted the transform SQL with the new cast.

---

## Production Table Structure

```sql
CREATE TABLE production.erp_transactions (
    -- source columns at their registered types; NOT NULL / PK only where the DE registered them
    transaction_id          VARCHAR,
    customer_id             VARCHAR,
    amount                  DECIMAL(18,2),
    transaction_date        DATE,
    status                  VARCHAR,
    transaction_direction   VARCHAR,

    -- metadata
    batch_id                VARCHAR,        -- provenance key
    load_date               DATE            NOT NULL,   -- system load date; partition column
    load_timestamp          TIMESTAMP,                  -- exact system load time
    source_system           VARCHAR,
    restated                BOOLEAN         DEFAULT FALSE   -- set TRUE when an approved waiting batch replaces rows
)
-- PARTITIONED BY RANGE (load_date)
-- base build: no row-level UNIQUE; collisions handled by load_type routing (Module 4 §4.4).
-- when a natural_key is set: + nk bigint and a NON-UNIQUE nk btree index (never a UNIQUE constraint).
-- waiting.<tbl> is `LIKE` this table + a wbatch_id column.
```

Naming: `production_{source_system}_{table_name}`.

---

## Production Transform Flow

```
Blocking lock on the table
        ▼
1. structural filter (one SQL): bad rows → quarantine.<tbl> + quarantine_batch_log; DELETE from staging
2. business rules on the survivors (set-based SELECT)
        ▼
3. route by load_type:
   FULL_SNAPSHOT → ensure load_date partition → DELETE production WHERE load_date=:d → INSERT ... SELECT
   INCREMENTAL   → colliding E values → waiting.<tbl> + waiting_batch_log; DELETE from staging
                 → remaining rows → INSERT ... SELECT into production (partitions per load_date)
        ▼
TRUNCATE staging → commit (releases lock)
crash partway → full rollback; run FAILED; no auto-retry; DE re-triggers after fixing the cause
```

---

## Dependencies

- Module 3: Schema Registry (cast schema, `load_type` + `existence_check_column`, `partition_key = load_date`, business rules)
- Module 4: Data Quality (the transform steps + diversions are its logic, run here)
- Module 5: Staging Layer (input; truncated at end of run)
- Module 7: Business Layer (reads from Production)
- Module 8: Orchestration (schedules the transform; the lock; the online migration)
- Module 9: Monitoring (table size metadata; per-chunk run log; migration status)
- Module 11: SQL Generation Engine (emits and regenerates the whole transform)

---

## Resolved

- Single transform point; blocking lock; one transaction per run; truncate staging only on full success; all set-based SQL, never row-by-row
- **Collision routing** (base build) is DE-configured — `FULL_SNAPSHOT` overwrites its `load_date`; `INCREMENTAL` diverts rows already in production → `waiting.<tbl>`, matched by an exact `existence_check_column` value **or** a composite `natural_key` (hashed `nk`, set-based join). Both **divert** to waiting — row-level `ON CONFLICT` upsert is still deferred.
- Diversions are per-table tables in the `quarantine` / `waiting` schemas; reason + counts per batch in `quarantine_batch_log` / `waiting_batch_log`
- Every row carries system `load_date` / `load_timestamp`; production partitioned by `load_date`
- **No CDC, no row-level deletes** in the base build (supersedes D6.1)
- `NOT NULL` / PK only from explicit DE registration — nothing inferred
- **No automatic retry** — a failed run is FAILED; the DE fixes and re-triggers
- Business rule approval — AE for standardization, business sign-off for derived logic (D6.3)
- Migration + in-flight load — no freeze; reconcile the delta after the swap (D6.5)
- Partition key — `load_date`, fixed (D5.2 / D6.6)

## Deferred

- Row-level upsert mechanism (returns with the "Configure" natural key) — design later (D6.2)

## Open Questions

- [ ] Old table after the migration swap — (a) archive + retention policy, (b) drop after row-count verify, (c) archive 7 days then auto-drop. (rec: **c**) — **awaiting decision**
