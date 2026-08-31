# Module 11: SQL Generation Engine
**Owner:** DE + AE (Shared)
**Layer:** Cross-cutting
**Back to:** [claude.md](../claude.md)

---

## Purpose

Generates the SQL that moves data from **staging to production**, deterministically, from the Schema Registry. Each pipeline's transformation SQL is built once, then regenerated automatically whenever a verified schema change lands in the registry. No one hand-writes or hand-edits the staging → production SQL — the registry is the source of truth and the SQL is its output.

---

## Key Design Decisions

- **Deterministic** — the same registry state always produces byte-identical SQL
- **Registry-driven** — column list, order, source/target types, nullable flags, primary keys, watermark, and partition key all come from `registry_columns` / `registry_tables`
- **Built once per pipeline**, then regenerated on change — never edited in place
- Generated SQL is **version-stamped** with the `schema_version` it was built from
- The engine itself is stable code; only its inputs (the registry) change
- Templated, not string-concatenated ad hoc — one reviewed template per transform step

---

## Scope

| Generated artifact | Priority | Driven by |
|--------------------|----------|-----------|
| Staging → production transform (structural filter → quarantine → route by `load_type` → insert-or-divert) | Primary | `registry_columns`, `registry_tables`, `quality_rules` |
| Staging table DDL (all VARCHAR + metadata `load_date` / `load_timestamp`, no constraints) | Primary | `registry_columns` |
| Production table DDL (typed, `PARTITION BY RANGE(load_date)`, `restated` column; no row-level UNIQUE in the base build) | Primary | `registry_columns`, `registry_tables` |
| `quarantine.<tbl>` DDL (`LIKE` staging + `qbatch_id`) and `waiting.<tbl>` DDL (`LIKE` production + `wbatch_id`) | Primary | `registry_columns`, `registry_tables` |
| Scoping query — CURSOR / TIME_WINDOW / FULL (Module 1) | Primary | `registry_tables` (strategy, cursor/window columns), column list |
| `quarantine_batch_log`, `waiting_batch_log` — control-plane tables, DDL fixed (in `001_core.sql`) | — | — |
| Row-level upsert transform (`ON CONFLICT (natural_key)`) | Later, when `natural_key` is set via "Configure" | `registry_tables.natural_key` |
| Business layer join / aggregation SQL (Module 7) | Later | `join_registry`, `metrics_registry` |

---

## Features to Build

### 11.1 Template Library
- One template per transform step: structural filter, quarantine split, business-rule apply, existence check, insert / divert write
- Templates are reviewed code artifacts, not inline strings
- Placeholders resolved from registry rows

### 11.2 Registry Reader
- Pulls the current (or a pinned) `schema_version` for a source + table
- Column model: name, order, source/target type, nullable, PK, cursor/window flag
- Table model: staging/production targets, `load_type` + `existence_check_column`, extraction strategy + cursor/window columns, `natural_key` (NULL in base build)

### 11.3 Transform SQL Builder
Emits the transform in the fixed order from Module 6 §6.2, all set-based:

1. **Structural filter** — one `WHERE` predicate: `char_length(col) > :text_cap`, `NOT NULL` columns empty, would-not-cast once types are configured. Matching rows → `INSERT INTO quarantine.<tbl> SELECT s.*, :qbatch_id ...` then `DELETE` from staging; one `quarantine_batch_log` row.
2. **Business rules** — apply active `quality_rules` to the survivors in the write `SELECT`.
3. **Route by `load_type`:**
   - `FULL_SNAPSHOT` — `production_partition_ddl(:load_date)` → `DELETE FROM production.<t> WHERE load_date = :load_date` → `INSERT ... SELECT`.
   - `INCREMENTAL` — `SELECT DISTINCT E::text, count(*) FROM staging s WHERE EXISTS (SELECT 1 FROM production.<t> p WHERE p.E::text = s.E::text) GROUP BY 1`; per value → `INSERT INTO waiting.<tbl> SELECT ..., :wbatch_id ... WHERE E::text = :value` + `waiting_batch_log` row + `DELETE` from staging; then `INSERT ... SELECT` the remaining survivors into production.

```sql
-- Generated example (schema_version = 7) — INCREMENTAL production insert step
INSERT INTO production_erp_transactions (transaction_id, customer_id, amount, transaction_date,
                                        status, batch_id, load_date, load_timestamp,
                                        source_system, restated)
SELECT transaction_id, customer_id, amount, transaction_date,
       UPPER(TRIM(status)), batch_id, load_date, load_timestamp,
       source_system, false
FROM   staging_erp_transactions;   -- diverted rows already removed above
```

### 11.4 DDL Builder
- **Staging** — every source column VARCHAR + metadata (`load_date`, `load_timestamp`, `batch_id`, …); no constraints, no indexes
- **Production** — target types, `NOT NULL` where registered, `PARTITION BY RANGE(load_date)`, `restated` column; **no row-level UNIQUE** in the base build (added by "Configure" when `natural_key` is set)
- **`quarantine.<tbl>`** — `LIKE <staging_target> INCLUDING DEFAULTS` + `qbatch_id text NOT NULL`
- **`waiting.<tbl>`** — `LIKE <production_target> INCLUDING DEFAULTS` + `wbatch_id text NOT NULL`
- **`quarantine_batch_log` / `waiting_batch_log`** — fixed shape (Module 4 §4.6–4.7), in `001_core.sql`

### 11.5 Regeneration Trigger
- Subscribes to registry commits (Module 3, section 3.7)
- On a verified `TYPE_CHANGE` (or ADD / REMOVE / RENAME), rebuilds the affected pipeline's SQL and DDL
- Stamps the new artifacts with the new `schema_version`
- Hands off to Module 6 (online migration) and Module 8 (scheduling) for tables that already hold data

### 11.6 Generated SQL Store

```sql
CREATE TABLE generated_sql (
    pipeline_name       VARCHAR,
    source_id           VARCHAR,
    table_name          VARCHAR,
    artifact_type       VARCHAR,   -- STAGING_DDL, PRODUCTION_DDL, QUARANTINE_DDL, WAITING_DDL, TRANSFORM, SCOPING_QUERY
    schema_version      INTEGER,
    sql_text            VARCHAR,
    generated_timestamp TIMESTAMP,
    generated_by        VARCHAR,   -- always 'SQL_GEN_ENGINE'
    is_current          BOOLEAN
)
```

### 11.7 Diff & Dry Run
- Before publishing regenerated SQL, produce a diff against the current version
- Optional: run the new transform against a sample to confirm it executes
- Diff is surfaced in the self-service interface for the confirming user

---

## Generation Flow

```
Registry commit (new schema_version)
        ↓
Registry Reader builds column + table model
        ↓
Template Library + Builders emit DDL + transform SQL
        ↓
Diff vs current generated_sql
        ↓
Store new artifacts (is_current = TRUE, stamp schema_version)
        ↓
If production table has data → Module 6 migration, scheduled by Module 8
```

---

## Dependencies

- Module 3: Schema Registry (sole input; commits trigger regeneration)
- Module 4: Data Quality (`quality_rules` feed generated range/value checks)
- Module 5: Staging Layer (consumes generated staging DDL)
- Module 6: Production Layer (consumes generated transform SQL and production DDL; runs migration on change)
- Module 8: Orchestration (schedules regeneration follow-up and migration)
- Module 10: Self-Service Interface (shows the diff for confirmation)

---

## Open Questions

- [ ] Do pipelines pin a `schema_version` or always track latest?
- [ ] Which SQL dialect does the engine target — Foundry Pipeline Builder SQL, Spark SQL, or both?
- [ ] Are generated artifacts committed to source control, or only stored in `generated_sql`?
- [ ] For ADD/REMOVE column changes, do we auto-regenerate or require explicit approval like TYPE_CHANGE?
- [ ] Does the engine also own the business layer join/aggregation SQL, or does Module 7 keep that?
