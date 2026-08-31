# Module 4: Data Quality
**Owner:** Data Engineering (DE)
**Layer:** Pre-Staging (file loads) + Staging → Production
**Back to:** [claude.md](../claude.md)

---

## Purpose

Keep bad data out of production without ever breaking a pipeline, via **two separate diversions**, each a **per-table table in its own schema**:

- **`quarantine.<table>`** — rows that are *structurally* broken (over-length string, empty required field, would not cast). A VARCHAR mirror of staging. The failure reason + row counts for each write are recorded in the control-plane table **`quarantine_batch_log`**. Fixed and re-injected as a **new batch**.
- **`waiting.<table>`** — rows that are structurally fine but whose existence-check value already exists in production. An **exact copy of the production table**. One **`waiting_batch_log`** row per colliding value. Held for a human approve/reject.

Source **content is treated as truth and never second-guessed.** Only structural integrity is checked, and only at the one transform point (staging → production), always as **set-based SQL over the whole batch — never row-by-row**. The single content judgement is the existence-check collision above.

---

## Key Design Decisions

- **Check 1** (file loads only) — header names + column count match the registry, or the whole file is rejected. No partial ingestion.
- **Check 2** — structural validation, at exactly one place: the staging → production transform. Run as **one set-based SQL pass** over the whole staging batch (`... WHERE char_length(col) > :cap OR ...`), never a row-by-row loop.
- Structural failure is **binary** — the whole row moves to `quarantine.<table>`; it is never partially applied. There is no "SOFT structural failure".
- **Business range/value rules** (`amount > 0`, `status IN (...)`) are **SOFT by default** — the row still lands in production, flagged and logged. A DE may mark a specific rule HARD (quarantine) when a business constraint must not enter production. _(This supersedes the pre-spec D4.2 default of HARD.)_
- **Waiting-pipeline routing** is by existence-check value only — an incoming value already in production (exact match) diverts those rows. Never a comparison of incoming vs stored *content*.
- Re-injection of a fixed quarantined row is always a **new batch**; there is no patch-in-place path.
- **Pause the pipeline when the quarantine rate exceeds 5%** (D4.1).
- **Recovery is manual, always.** A DE resolves `quarantine` rows (fix the cause → re-inject as a new batch) and `waiting_pipeline` slices (approve / reject) through the review tools ([Module 10](10-self-service-interface.md)). The system never auto-retries, auto-reprocesses, or auto-expires either — a failed load re-run will just fail the same way.

---

## Features to Build

### 4.1 Check 1 — Header Validation (file loads)
- Incoming file header vs `registry_columns` (names + count)
- Mismatch → whole file rejected: `staging_file_control` set FAILED with the reason, alert, file left for re-submission. Nothing is loaded, so there is no per-table quarantine row.

### 4.2 Check 2 — Structural validation (one set-based SQL pass)
At the transform, one SQL statement flags every structurally bad row in the staging batch against a per-table predicate built from the registry:

- **over-length** — `char_length(col) > :text_cap` (256) for every text column that can reach an indexed production column
- **empty-required** — `col IS NULL OR btrim(col) = ''` for every column the registry marks `NOT NULL`
- **would-not-cast** — once "Configure" assigns real types, a `col IS NOT NULL AND <col>::<type>` that raises → flagged (base build: all columns text, so this contributes nothing)

> **The `text` cap is not cosmetic.** A long, high-entropy string in a column that is part of production's unique index exceeds the DB's btree entry limit, and the failing statement is the **bulk insert for the whole batch** — one poisoned row jams every subsequent run. Catching it in the pre-insert filter defuses a standing DoS.
>
> **NUL bytes** (`\x00`) never reach here: PostgreSQL `text` cannot hold one, so `COPY` into staging fails at extract time. No transform check needed for the file connector.

### 4.3 Quarantine the bad rows
- `INSERT INTO quarantine.<table> SELECT s.*, :qbatch_id FROM <staging> s WHERE <bad predicate>` — one bulk write, then `DELETE` those rows from staging.
- One `quarantine_batch_log` row per write: `qbatch_id`, `run_id`, `reason` (e.g. `over_length`), `row_count`.
- The empty-bad and empty-good paths must both be no-ops, never a crash.

### 4.4 Collision routing (the surviving rows) — by `load_type`
No period is computed. Rows keep the system `load_date` / `load_timestamp` stamped at load.

**`FULL_SNAPSHOT`**
1. Ensure the daily partition for this load's `load_date` exists.
2. `DELETE FROM production.<table> WHERE load_date = :load_date`.
3. `INSERT INTO production.<table> SELECT ... FROM <staging>`.
- The existence-check column is irrelevant; nothing goes to the waiting pipeline. Re-running a snapshot for the same `load_date` is idempotent.

**`INCREMENTAL` with a single `existence_check_column`** — `E` = the column
1. Colliding values: `SELECT DISTINCT s.E::text, count(*) FROM <staging> s WHERE EXISTS (SELECT 1 FROM production.<table> p WHERE p.E::text = s.E::text) GROUP BY 1`.
2. For each colliding value: `INSERT INTO waiting.<table> SELECT ..., :wbatch_id FROM <staging> s WHERE s.E::text = :value`, plus one `waiting_batch_log` row (`wbatch_id`, `run_id`, `existence_value`, `row_count`, `status='pending'`). Then delete those rows from staging.
3. Remaining rows → `production.<table>` (create partitions for each distinct `load_date` present).

**`INCREMENTAL` with a composite `natural_key`** (supersedes the single column; `transform._load_incremental_nk`)
- Production and `waiting.<table>` carry a hashed **`nk bigint`** column (btree-indexed): `hashtextextended(concat_ws(chr(31), coalesce((col::type)::text, chr(1)), …), 0)` — identical expression on staging and production.
- `collides = EXISTS (SELECT 1 FROM production.<table> p WHERE p.nk = <staging nk> AND <raw-column tie-break>)`. **Two set-based statements, no per-value loop:** all colliding rows → `waiting.<table>` in one INSERT + **one** `waiting_batch_log` row for the run; then `DELETE FROM staging WHERE collides`; survivors → production with `nk` populated.
- The tie-break (`col::type IS NOT DISTINCT FROM col::type` per key column) runs only on rows already sharing an `nk`, so a 64-bit hash collision cannot mis-route. Key columns cannot be `numeric`/`unit_interval`-typed.

Both paths: existence only — no content comparison. Retry safety comes from one-transaction-per-run (§6.5): a failed run rolls back its production writes, so a retry sees the value / key as absent and inserts cleanly.

### 4.5 In-load dedup
- Not handled at the row level in the base build. If two files in the same run carry the same existence value, both sets of rows collide with production (or the first inserts and the second then collides) and route to `waiting`.

### 4.6 Quarantine tables (per source table, `quarantine` schema)

```sql
CREATE SCHEMA quarantine;

-- created at onboarding, one per source table:
CREATE TABLE quarantine.<table> (
    LIKE <staging_target> INCLUDING DEFAULTS,   -- every staging column, all VARCHAR, + staging metadata
    qbatch_id  TEXT NOT NULL                    -- links to quarantine_batch_log
);

-- control plane (in 001_core.sql):
CREATE TABLE quarantine_batch_log (
    qbatch_id         TEXT PRIMARY KEY,
    run_id            TEXT,
    source_id         TEXT NOT NULL,
    table_name        TEXT NOT NULL,
    reason            TEXT NOT NULL,            -- 'over_length', 'empty_required:<col>', 'cast:<col>', ...
    row_count         BIGINT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolution_status TEXT NOT NULL DEFAULT 'open'  -- open, reinjected, ignored
                      CHECK (resolution_status IN ('open','reinjected','ignored')),
    resolved_by       TEXT,
    resolved_at       TIMESTAMPTZ
);
```

- Rows stored verbatim (loose VARCHAR) so a DE can review and idempotently re-inject once corrected — always as a **new `batch_id`**.

### 4.7 Waiting pipeline tables (per source table, `waiting` schema)

```sql
CREATE SCHEMA waiting;

-- created at onboarding, one per source table:
CREATE TABLE waiting.<table> (
    LIKE <production_target> INCLUDING DEFAULTS, -- exact copy of the production columns + metadata
    wbatch_id  TEXT NOT NULL                     -- links to waiting_batch_log
);

-- control plane (in 001_core.sql):
CREATE TABLE waiting_batch_log (
    wbatch_id        TEXT PRIMARY KEY,
    run_id           TEXT,
    source_id        TEXT NOT NULL,
    table_name       TEXT NOT NULL,
    existence_value  TEXT NOT NULL,              -- the colliding value of the existence-check column
    row_count        BIGINT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending','approved','rejected')),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_by      TEXT,
    resolved_at      TIMESTAMPTZ
);
```

- The review tool shows the pending batch's label and row count, and previews the waiting rows against production's current rows for the same value / key.
- **Approve** → delete the production rows the batch supersedes — by `E = :existence_value` (single column) or by `p.nk = w.nk AND <tie-break>` (natural key) — then insert the waiting rows for that `wbatch_id` with `restated = true`; clear them from `waiting.<…>`; `waiting_batch_log.status = 'approved'`.
- **Reject** → delete the rows for that `wbatch_id` from `waiting.<…>`; `status = 'rejected'`. Production keeps what it had.
- Holds only *unresolved* batches — the permanent record is the append-only source file, which this never touches.

### 4.8 Business Range / Value Rules (SOFT by default)

```sql
CREATE TABLE quality_rules (
    rule_id         VARCHAR,
    source_id       VARCHAR,
    table_name      VARCHAR,
    column_name     VARCHAR,
    rule_type       VARCHAR,   -- RANGE, ALLOWED_VALUES, REGEX, CUSTOM
    rule_expression VARCHAR,
    severity        VARCHAR,   -- SOFT (flag + log, row proceeds) | HARD (quarantine); default SOFT
    is_active       BOOLEAN
)
```

- SOFT: row lands in production with a flag; counted in the quality report
- HARD: row diverts to `quarantine` (`destination = 'rule:<rule_id>'`)

### 4.9 Type-Change Verification (Dry Run)
- Invoked by Module 3 §3.7 when a column type change is proposed
- Runs the cast test against the **current production column** with the **candidate** type (D3.4)
- Returns pass/fail + count and sample of values that would fail; read-only, nothing moved

```sql
SELECT
    COUNT(*)                                                   AS total_rows,
    COUNT(CASE WHEN TRY_CAST(amount AS DECIMAL(18,2)) IS NULL
               AND amount IS NOT NULL THEN 1 END)              AS would_fail_cast
FROM production.erp_transactions
```

### 4.10 Reporting
- Per source per run: rows processed / to production / to waiting_pipeline / quarantined; quarantine-rate trend; top failing fields and reasons
- Feeds Module 9 and the Module 10 scorecard

---

## Flow

```
(file loads)  File lands → Check 1 (header) → FAIL → staging_file_control FAILED, alert
                                            → PASS → load to staging
                                                       │
staging → production transform (one transaction, blocking lock):
        ▼
   Check 2: one set-based filter ──► bad rows ──► quarantine.<tbl> + quarantine_batch_log
        │ (surviving rows)
        ▼
   load_type = FULL_SNAPSHOT → delete production rows for this load_date → insert all
   load_type = INCREMENTAL   → E value already in production → those rows → waiting.<tbl>
                                                                          + waiting_batch_log
                             → E value new → insert into production
        ▼
   TRUNCATE staging → commit (releases lock)
```

---

## Dependencies

- Module 3: Schema Registry (cast schema, `load_type` + `existence_check_column`; calls §4.9 for type-change verification)
- Module 5: Staging Layer (input)
- Module 6: Production Layer (runs Check 2 + collision routing as part of its transform)
- Module 9: Monitoring (quarantine + waiting-pipeline backlog alerts)
- Module 10: Self-Service Interface (quarantine review; waiting-pipeline batch approve/reject tool)
- Module 11: SQL Generation Engine (emits the filter → quarantine → route → write SQL)

---

## Resolved

- Two diversions, both **per-table in their own schema**: `quarantine.<tbl>` (structural, VARCHAR mirror of staging) + `waiting.<tbl>` (colliding rows, exact copy of production). Reason + counts per batch in `quarantine_batch_log` / `waiting_batch_log`.
- **All checks and routing are set-based SQL over the whole batch — never row-by-row.**
- **Collision routing** for slices 1–3 is DE-configured: `FULL_SNAPSHOT` overwrites its own `load_date`; `INCREMENTAL` diverts rows whose `existence_check_column` value (exact) is already in production. No row-level natural key in the base build; that's a later "Configure" option.
- Structural failure is binary → quarantine; business rules SOFT by default (supersedes D4.2)
- Quarantine rate pause threshold — 5% (D4.1)
- Quarantine resolution owner — DE; waiting-pipeline batches via a separate review tool (D4.3)
- Re-injection = new batch, no patch-in-place

## Open Questions

- [ ] Confirm business range/value rules default to **SOFT** now (content-is-truth), reversing the earlier HARD default — **awaiting confirmation**
- [ ] `text` cap value — 256 chars (spec default); confirm it clears our DB's btree entry limit
