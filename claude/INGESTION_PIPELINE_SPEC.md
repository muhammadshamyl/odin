# Multi-Source Ingestion Pipeline — Architecture & Replication Spec

A product-agnostic specification for ingesting data from multiple independent
sources into a single analytical warehouse, with first-class handling of
**duplicates / restatements** and **bad data** through two separate diversion
mechanisms:

- a **quarantine pipeline** — for rows that are *structurally* broken, and
- a **waiting pipeline** — for rows that are structurally fine but would change
  already-settled history, and therefore need a human decision.

---

## 0. How to use this document

You are implementing a data-ingestion pipeline. Build it stage by stage in the
order given. Every numbered mechanic below exists because a specific failure was
hit in real testing of a system built to this design — do not "simplify" one away
without understanding what it caught. The **Adversarial test checklist** (§15) is
the acceptance suite; a stage isn't done until its cases pass against a live
database.

**Reference stack:** PostgreSQL (plain — no time-series or proprietary
extension), an orchestrator with dataset/lineage-aware DAG chaining (e.g. Apache
Airflow), Python, a connection-pooled Postgres driver. Substitute equivalents
freely, but keep the *shape*: the staging buffer, the single transform point, the
two diversion tables, the natural-key routing, the advisory lock, and the
per-chunk run log are the load-bearing parts.

**Two ingestion modes are supported** and converge on the same pipeline after the
first hop (§4):

1. **API push** — an external party sends data to an endpoint you host.
2. **Table pull** — you connect to a source database/table and read from it on a
   schedule.

---

## 1. Terminology

| Term | Meaning |
|---|---|
| **Source** | One external system feeding data in. Each source is isolated from every other. |
| **Source identity** (`source_id`) | Your own resolved identifier for a source. Never taken from request/row content. |
| **Entity** (`entity_id`) | The thing a measurement is about (a device, meter, line, account, site…). Carried as the source's own raw identifier unless you add a resolution step (§14). |
| **Window** | The time bucket a measurement covers (`window_start` / `window_end`), pre-aligned to your grain (e.g. one hour). |
| **Measure** | The numeric payload of a row (a count, a total, an amount). |
| **Natural key** | The tuple that uniquely identifies one settled fact: `(source_id, entity_id, window_date, window_bucket)`. |
| **Batch** | A unit of submitted/extracted data carrying a `batch_id` used for provenance and status read-back. |
| **Settled** | A window old enough that changing it requires human review (default: any window before the current UTC calendar day). |
| **Staging** | Loose, untyped, persistent retry buffer between extraction and transform. |
| **Production** | The typed, partitioned, natural-keyed correctness boundary. |
| **Quarantine** | Shared dead-letter table for structurally invalid rows. |
| **Waiting pipeline** | Table holding restatements of settled data, pending an approve/reject decision. |

---

## 2. Principles

These drive every decision below. When in doubt, default to these.

- **Per-source isolation.** One dedicated DAG per source per stage. A failure,
  schema change, or load spike in one source's pipeline must never cascade into
  another's. Enforce it structurally, not by convention. The only deliberately
  shared DAG is the final BI/mart merge, which genuinely needs all sources.
- **Aggregated, not raw.** Ingest pre-aggregated data (e.g. per-hour buckets),
  not raw events. This is what keeps a plain relational database viable at
  volume. If a source can only emit raw events, aggregate them in the connector
  before staging, not downstream.
- **Idempotency everywhere.** Every load is safely re-runnable with zero
  duplication. This is enforced at the **production natural key**, never at
  staging (staging is wiped every cycle, so a constraint there would only catch
  intra-cycle duplicates, not a source resubmitting a key weeks later).
- **Structural validation only, and only at transform time.** Source *content* is
  treated as truth and never second-guessed. Only structural integrity (types,
  ranges, presence, size) is checked, and only during the staging→production
  transform — never at extraction, never at ingestion.
  - **One deliberate, documented exception:** a value-changing resubmission
    against *settled* data is gated behind human review (the waiting pipeline,
    §11). This is a content judgment, and it is justified only for data that
    feeds something high-stakes (billing, financial reporting, regulatory
    submissions) or crosses an external trust boundary. Document it explicitly as
    an exception so it doesn't creep into ordinary validation.
- **Lightweight over infrastructure.** No file-landing server, no message broker,
  no table-swap/rename dance. A "landing table" is just a table. Don't
  reintroduce infrastructure weight without a concrete reason tied to measured
  volume or a real incident.
- **Traceability by design.** Every execution of every stage must be individually
  mappable after the fact — including which batches it touched and how far it
  got — even after staging has been wiped and the orchestrator's own logs have
  rolled off.

---

## 3. Canonical pipeline shape

```
                 ┌── mode A: API push ──►  inbox table (append-only, isolated)
SOURCE ──────────┤                                     │
                 └── mode B: table pull ──────────────► │  (read directly, keyset-paged)
                                                        │
                                                        ▼
                                        extract_to_staging      (per source; frequent)
                                                        │
                                                        ▼
                                        STAGING                 (loose, untyped, persistent retry buffer;
                                                        │        TRUNCATEd only after PRODUCTION load confirmed)
                                                        ▼
                                        staging_to_production   (per source; runs at production's grain)
                                                        │        cast → quarantine → check → route → dedup → write
                                        ┌───────────────┼───────────────┐
                                        ▼               ▼               ▼
                                   quarantine      PRODUCTION      waiting_pipeline
                                  (structural     (typed,          (settled-window
                                   failures)      partitioned,      restatements,
                                                  natural key)      await review)
                                                        │
                                                        ▼
                                        production_to_accumulated   (rollups)
                                                        │
                                                        ▼
                                        accumulated_to_bi           (shared DAG; merges all sources)
```

Key invariants:

- **Everything from the source through staging is loose text.** All
  source-supplied fields are `VARCHAR`/`TEXT` until the transform stage.
- **All casting, typing, and structural validation happen in exactly one place:**
  `staging_to_production`. Nowhere else.
- **Staging is `TRUNCATE`d only after the production load is *confirmed*
  successful** — never on "attempted".
- **A burst of input produces one extraction run, not one per item.** Extraction
  cadence is decoupled from input cadence.

---

## 4. The connector layer (first hop)

Both modes end with rows landing in `staging` in the same loose shape. Only the
first hop differs.

### 4.A — API push

Use when an external party sends data to you.

- **Host an API service in an isolated network segment (DMZ).** Its database
  credentials can reach **only** an isolated intake schema — never the core
  warehouse. There is **no network route from the DMZ into the core warehouse**;
  the only path out is `extract_to_staging` reading the inbox on your schedule,
  connecting *inward* on your initiative.
- **The API accepts, authenticates, validates shape, and acknowledges — it never
  writes to the core warehouse on the request path.** It writes only to the
  isolated `inbox` table.
- **`inbox`** is append-only, never truncated, all source-supplied fields stored
  as loose `VARCHAR`. It is the permanent record of every raw submission and
  satisfies the audit requirement on its own. It carries an `extracted_at`
  timestamp column, initially `NULL`, used as the pull marker by Stage 1.
- **Authentication:**
  - Per-source API key sent in a header (e.g. `X-API-Key`). Identity comes
    **entirely from the key** — the caller never sends its own `source_id` in the
    body. This removes an entire spoofing class by construction.
  - Store only a salted hash of the key, resolved to `source_id`. Never log or
    persist the raw key after issuance.
  - Support **two active keys per source simultaneously** (old + new, old expires
    after a grace window) so rotation needs no flag-day coordination.
  - Reject expired/revoked keys identically to missing keys.
  - Exactly one unauthenticated endpoint: a liveness `GET /health` that touches
    no source data.
  - *(Stack note, if using FastAPI or similar):* the auth dependency must be a
    **plain synchronous function, not `async`** — it makes a blocking DB call,
    and an `async` dependency runs directly on the event loop, so every
    authenticated request would block the entire service for every other
    concurrent request for the duration of that call. A sync dependency is run in
    the threadpool automatically.
- **Validation on the request path is structural only:** required fields present,
  correct JSON types, `window_start < window_end`, windows aligned to your grain,
  mandatory fields (unit, confidence, whatever your domain requires) present. On
  failure, reject the whole request with a `400` and a consistent error shape:
  `{ "error": { "code": "...", "message": "..." } }`.
- **Acknowledge with `202 Accepted`**, returning `accepted_rows` and an itemized
  `rejected_rows` (structural rejections the caller can fix immediately). **`202`
  does not mean "stored".** Natural-key outcomes (landed / pending review /
  restated) are only knowable later.
- **Status read-back:** `GET /batches/{batch_id}` returns **aggregate counts
  only** (`received` / `landed` / `pending_review` / `rejected`) — a plain
  `GROUP BY batch_id` query against `production` + `waiting_pipeline`. Never
  expose per-row internal warehouse state to the source.
- **Write to the inbox with a single bulk `COPY` and one commit**, never
  row-by-row (a row-by-row loop holds a pooled connection for its whole duration
  and adds latency to the caller's response for no benefit).
- **Entity registration (optional):** if you want sources to declare their
  entities up front, expose a separate idempotent `POST /entities` writing to the
  isolated intake schema (safe to re-send; same id updates rather than
  duplicates). Keep it **fully decoupled** from the batch path — the batch path
  does not look entities up (see §14 for why this is a deliberate default).

### 4.B — Table pull

Use when you connect to a source-owned database/table and read from it.

- **Connect with least privilege** — read-only on exactly the source
  object(s) you need.
- **Keyset-paginate** on a monotonic cursor column (an autoincrement id, or an
  `updated_at` if the source mutates rows). Never `OFFSET`; never pull a whole
  table into memory.
- **Track a high-water mark** per source (last cursor value successfully staged),
  persisted in your own metadata table — this is the equivalent of the inbox's
  `extracted_at IS NULL` marker.
- **Prefer a pre-aggregated view.** If the source can expose a view already
  bucketed to your grain, read that. If not, aggregate in the connector before
  writing to staging — do not push raw-event granularity downstream.
- **Do not trust the source's types.** Read every column as text into staging
  exactly as in mode A; all casting still happens once, at transform.
- The trust boundary is weaker than mode A (no external party initiates a
  connection into your network), but the **content trust rules are identical**:
  structural validation at transform, and settled-window restatements still go
  through the waiting pipeline if this source's data is high-stakes.

---

## 5. Stage 1 — `extract_to_staging`

**One DAG per source.** Schedule: frequent and fixed (e.g. every 1–2 minutes for
an inbox; source-appropriate for a pull) — **not** event/dataset-triggered per
submission. This bounds worst-case latency without coupling DAG-run volume to
input volume: 500 submissions still produce one extraction run.

**Chunked** (e.g. 10,000 rows per chunk) — never load everything into memory.
Page on the keyset cursor (§4).

**Per chunk:**

1. Read the next chunk of not-yet-extracted rows:
   `... WHERE <not-extracted> AND cursor > :last ORDER BY cursor LIMIT :n`.
2. **Write to staging first; mark the source rows extracted second — as two
   separate transactions.** Rationale: a crash *between* the two yields a
   harmless duplicate staging row on retry (production's natural key dedups it
   downstream). The reverse order (mark first, write second) would lose rows
   permanently on the same crash. This is not a close call.
3. Write to staging with a **bulk `COPY`**, never row-by-row `INSERT`.
4. Immediately record the chunk's outcome to the run log (§12) — success *or*
   failure — before starting the next chunk.

**Concurrency with Stage 2** (they share the `staging` table, and Stage 2
truncates it): guard the staging write with a **Postgres advisory lock keyed on a
constant** for this source, e.g.
`pg_try_advisory_xact_lock(hashtext('<source>_staging'))`:

- Stage 1 takes it **per chunk, as a try-lock**, around the staging write only.
- If it is **not** acquired, Stage 2 is mid-run → **stop the whole cycle** (not
  just skip the chunk); the still-unmarked backlog is retried on the next tick.
- Do **not** span the lock across the source read and the staging write if the
  source may be a physically separate database — one transaction can't cover
  both.

---

## 6. Staging table

- **Loose and persistent.** Every source-supplied field is `VARCHAR`/`TEXT`. No
  constraints, no type enforcement, no indexes needed.
- **Reused every cycle.** Never renamed, swapped, or recreated. It is the retry
  buffer: if transform fails, the next run retries from staging without
  re-hitting the source.
- Carries `source_id`, `entity_id`, `batch_id`, the raw window bounds, the raw
  measure, and any raw domain fields (unit, confidence, …), plus a `loaded_at`
  default.
- **`TRUNCATE`d only by Stage 2, and only after the entire production load of
  that run has succeeded.**

---

## 7. Stage 2 — `staging_to_production` (the core)

**One DAG per source.** Schedule: **at production's natural grain** (hourly if
production is one row per entity per hour). Running more often just re-touches a
still-forming bucket for no benefit.

**Lock:** the **same** advisory key as Stage 1, but taken **once, blocking, for
the entire run**, inside **one transaction**. `pg_advisory_xact_lock` auto-releases
on commit, so a crashed/reset pooled connection can never leak it. Stage 2 is the
DAG that empties staging, so it must hold the lock across the whole
read → process → write → truncate sequence. If it only locked around the truncate,
Stage 1 could insert new rows between this run's read and its truncate, and those
rows would be silently wiped.

**Read:** a server-side named cursor over all of `staging`, fetched
`CHUNK_SIZE` rows at a time. No `ORDER BY` / keyset column is needed because the
advisory lock guarantees nothing else writes staging during the run, so a single
streamed snapshot can't miss or duplicate rows.

### Per chunk — the five steps

#### Step 1 — Cast & classify → `(good, bad)`

For each row, cast every text field to its real type against a declared cast
schema. Example:

```python
CAST_SCHEMA = {
    "window_start": "timestamptz",
    "window_end":   "timestamptz",
    "entity_id":    "text",           # non-empty, max 256 chars
    "batch_id":     "text",
    "unit":         "text",
    "measure":      "nonneg_int32",   # integer, 0 .. 2_147_483_647
    "confidence":   "unit_interval",  # float, 0.0 .. 1.0   (drop if not in your domain)
}
```

- A row that fails **any** cast, or casts to something out of range, is
  short-circuited and appended to `bad` with an error string — **the whole row**,
  never partially applied.
- Every other row goes to `good`, carrying **both**:
  - the **cast** values (for production), and
  - the **original raw text** of the source-supplied fields (for the waiting
    pipeline — a human reviews what was actually submitted, not a reformatted
    version).
- Derive `window_date` and `window_bucket` (e.g. hour 0–23) from the cast
  `window_start`, normalized to UTC, here.

#### Step 2 — Quarantine the bad rows

Divert all of `bad` into the shared `quarantine` table in **one bulk `COPY`**
(raw row as JSON + the error string + `source` + `destination`). Never blocks the
rest of the chunk. See §10.

#### Step 3 — Ask production one question

For the `good` rows, compute `MIN`/`MAX` `window_date`, then run a **single**
query:

```sql
SELECT source_id, entity_id, window_date, window_bucket
FROM   production
WHERE  window_date BETWEEN :min_date AND :max_date
GROUP  BY source_id, entity_id, window_date, window_bucket;
```

→ a set of already-occupied natural-key slots. **Existence only — no value
comparison.** Routing is decided entirely by whether a slot is taken and how old
its window is, never by whether the incoming number differs from the stored one.

#### Step 4 — Route each good row

| Condition | Destination |
|---|---|
| Slot **not** in the occupied set | `production` — fresh insert |
| Slot occupied, but `window_date == today` (current UTC calendar day) | `production` — upsert; routine same-day refinement, no review |
| Slot occupied **and its window is a previous day** (settled) | `waiting_pipeline` — **regardless of whether the value is identical or different** |

There is deliberately **no lateness cutoff** — a source may restate an
arbitrarily old window, and it will be *considered*. But "considered" is not
"auto-applied": once a window is settled, both a silent overwrite **and** a silent
"confirm unchanged" are treated as integrity risks. A recorded human decision is
required (§11).

#### Step 5 — In-chunk dedup, last-wins

Two rows *in the same chunk* targeting the same natural-key slot cannot both go
through one `INSERT ... ON CONFLICT DO UPDATE` — Postgres refuses a statement that
would touch the same row twice (found by testing, not theory). So:

- Keep the **last** occurrence per key for the production write.
- **The earlier occurrences (losers) are not discarded** — route them into
  `waiting_pipeline` alongside genuine previous-day collisions, so a human still
  sees every submitted value.

### Writing the rows

**To production — via a session-scoped TEMP table, never a hand-built multi-row
`INSERT`:**

```sql
CREATE TEMP TABLE IF NOT EXISTS tmp_production_batch (
    /* same columns as the production insert target */
) ON COMMIT DROP;

-- bulk COPY the chunk's production-bound rows into tmp_production_batch, then:

INSERT INTO production (
    source_id, entity_id, batch_id, window_start, window_end,
    window_date, window_bucket, measure, unit, confidence
)
SELECT
    source_id, entity_id, batch_id, window_start, window_end,
    window_date, window_bucket, measure, unit, confidence
FROM tmp_production_batch
ON CONFLICT (source_id, entity_id, window_date, window_bucket) DO UPDATE SET
    batch_id     = EXCLUDED.batch_id,
    window_start = EXCLUDED.window_start,
    window_end   = EXCLUDED.window_end,
    measure      = EXCLUDED.measure,
    unit         = EXCLUDED.unit,
    confidence   = EXCLUDED.confidence;

TRUNCATE tmp_production_batch;
```

**Why the temp table:** a single `INSERT` with one bound parameter per cell hits
Postgres's hard **65,535-parameters-per-statement limit** as soon as a chunk
sends more than ~6,553 rows — routine at a 10,000-row chunk size, not an edge
case. `COPY` into a temp table followed by one parameter-free
`INSERT ... SELECT ... ON CONFLICT` sidesteps the limit regardless of row count.

**To `waiting_pipeline` — a direct bulk `COPY`.** No `ON CONFLICT`, no uniqueness
constraint on that table, so no temp table or dedup step is needed. Store the
**raw** window/measure/confidence text plus the derived `window_date` /
`window_bucket` plus bookkeeping columns.

### End of run

- `TRUNCATE staging` **only after every chunk has succeeded**, then `commit()` —
  which releases the advisory lock and drops the temp table.
- The whole run is one transaction: a crash partway rolls back all
  production / waiting / quarantine writes from that run, leaving production
  exactly as it was before it started.
- The **per-chunk run-log rows are written on a separate connection that commits
  immediately** (§12), so they survive the rollback and still show precisely
  which chunk reached how far.

---

## 8. Production table

- **Typed.** Every loose staging field becomes its real type here.
- **Partitioned by date** (`PARTITION BY RANGE (window_date)`), with an explicit
  `window_bucket` column (e.g. `SMALLINT CHECK (window_bucket BETWEEN 0 AND 23)`).
- **Generate partitions with a stored procedure that `COMMIT`s every N
  partitions**, not a single `DO` block. A `DO` block holds one lock per created
  partition for the whole run and exhausts `max_locks_per_transaction` at a few
  thousand partitions. Make it re-runnable (`CREATE TABLE IF NOT EXISTS` per
  partition).
- **Natural key:** `UNIQUE (source_id, entity_id, window_date, window_bucket)` —
  one row per source's entity per time bucket.
  - `source_id` **is** part of the key. Two different sources coincidentally
    reusing the same raw `entity_id` for the same window are **separate slots**,
    not a collision — nothing resolves `entity_id` to a shared surrogate.
  - For a partitioned table the primary key must include the partition column:
    `PRIMARY KEY (id, window_date)`.
- `restated BOOLEAN NOT NULL DEFAULT false` — set to `true` when an approved
  waiting-pipeline item overwrites a settled value.
- `batch_id` is carried on every row. It is the **provenance key** — it makes the
  status read-back (§4.A) a plain `GROUP BY batch_id`, and removes the need for a
  separate row-level lineage table for any source that has a natural batch key.

---

## 9. `production_to_accumulated` and the shared BI stage

- `production_to_accumulated` (one DAG per source) rolls production up to whatever
  grain your marts consume.
- `accumulated_to_bi` is the **single deliberately shared DAG** — BI marts
  genuinely need all sources merged. This is the one sanctioned exception to
  per-source isolation; document it as such.
- No source is special-cased past `production` — all modes and all sources look
  identical downstream.

---

## 10. The quarantine pipeline (structural / bad-data diversion)

- **One shared, cross-source table.** Columns: `source`, `destination` (which
  stage rejected it), `raw_data` (`JSONB`, the row verbatim), `error` (`TEXT`),
  `created_at`.
- **What lands here:** *structural* failures only — cast failure, number out of
  range, wrong sign, empty string where a value is required, over-length string.
  **Never** a business-content judgment.
- **How:** a shared `validate_row(raw, schema) -> (ok, error, casted)` helper
  that short-circuits on the first bad field. The caller diverts the **whole**
  row via a bulk `COPY` into `quarantine` — never row-by-row, never a partial
  apply.
- **Raw row stored verbatim as JSON** so a data engineer can review and
  idempotently re-inject once corrected. Re-injection is always a **new batch**
  with a new `batch_id`; there is no "patch a quarantined row in place" path.
- **Cast types to implement** in `validate_row`:
  - `timestamptz` — ISO-8601 parse; attach UTC if naïve.
  - `int32` — integer within signed 32-bit range. (Keep allowing negatives — it
    is shared code and other callers may legitimately need them.)
  - `nonneg_int32` — as `int32` plus `>= 0`. Use this for counts/quantities.
  - `unit_interval` (a.k.a. `confidence`) — float in `[0, 1]`. A dedicated type,
    not plain float, because the range check must happen per-row here — otherwise
    one out-of-range value fails an entire bulk insert downstream.
  - `text` — non-empty after trim, **max 256 characters**. This length cap is not
    cosmetic: a long, high-entropy string in a field that is part of production's
    btree unique index **exceeds Postgres's ~2704-byte btree entry limit**, and
    the failing statement is the bulk insert for the **entire chunk**. One
    poisoned row from any source then jams the shared hourly run for every source
    on every subsequent run until it is manually deleted from staging — a
    standing denial-of-service. Catching over-length strings **per row, here,
    before the bulk insert** is what defuses it. Apply `text` to every
    string field that reaches an indexed production column (`entity_id` at
    minimum; `batch_id`, `unit`, etc. as defense-in-depth).

---

## 11. The waiting pipeline (restatement / human-review diversion)

- **A separate table from quarantine** — different meaning entirely. Quarantine
  says "this row is structurally broken." The waiting pipeline says "this row is
  structurally fine, but applying it would rewrite settled history, which is a
  content decision a human must make."
- **What lands here:**
  - any `good` row whose slot is occupied by a **previous-day** (settled) entry —
    whether the value is identical or different — and
  - in-chunk dedup losers (§7 step 5).
- **Today's collisions never land here** — they upsert straight to production.
- **No stored comparison value.** The review tool joins back to production live
  on `(source_id, entity_id, window_date, window_bucket)` only when an item is
  actually opened for review.
- **Columns:** the natural-key fields, the **raw** submitted window/measure/
  confidence text, `batch_id`, plus bookkeeping:
  `status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected'))`,
  `resolved_by`, `resolved_at`, `created_at`. Partial index on
  `status WHERE status = 'pending'`.
- **Resolution** happens through a **small internal admin tool** (list / approve /
  reject) — never raw SQL against production. Its auth/access model is **separate**
  from the source API keys and from human database/orchestrator admin accounts.
  - **Approve** → apply the new value to production, set `restated = true`; the
    prior value remains visible in provenance/history.
  - **Reject** → discard the waiting row; production keeps its original value.
- **No audit gap** from this being a working table: the permanent record of every
  raw submission is the append-only inbox / source (§4), which this mechanism
  never touches. `waiting_pipeline` only ever holds *unresolved* conflicts;
  clearing a resolved one loses nothing auditable.
- **This whole mechanism is optional per source.** Enable it for sources whose
  data is high-stakes or crosses an external trust boundary. For a low-stakes
  internal source you may choose to let previous-day collisions upsert directly —
  but make that an explicit, recorded decision.

---

## 12. Observability — the run log

- **One shared table**, grain **`(run_id, batch_id)`** — a single run can touch
  many batches, so each batch gets its own row (run-level status/timestamps
  repeat across those rows; this is accepted as simpler than a batch-list
  column). An empty/unknown batch still writes one row with `batch_id = NULL` so
  a quiet cycle or an early failure stays visible.
- Columns: `dag_id`, `run_id`, `batch_id`, `started_at`, `ended_at`, `status`
  (`success` / `failed` / `running`), `error_message`, `triggered_by`
  (`scheduled` / `backfill` / `manual`).
- **Two write paths, both used:**
  1. **`on_failure_callback` at the DAG level** — a safety net for failures
     *outside* the per-chunk loop (initial lock acquisition, the final
     `TRUNCATE`).
  2. **`record_chunk(...)` called directly by the task, once per chunk**, right
     after the chunk finishes (success or its own failure), over a **separate
     connection that commits immediately**. This is what preserves "chunks 1–2
     succeeded, chunk 3 crashed" detail — a callback-only approach can only ever
     write one opaque `batch_id = NULL` "run failed" row, because on a mid-run
     crash the task never returned anything.
  - DAGs using `record_chunk` **drop `on_success_callback`** (redundant with the
    per-chunk writes) but **keep `on_failure_callback`**.
- **Provenance** (which row came from where) is carried by `batch_id` threaded
  through every table — inbox/source → staging → production / waiting_pipeline.
  Only build a separate row-level lineage table for a source that has **no**
  natural batch/provenance key of its own.
- If your orchestrator has dataset/lineage-aware DAG chaining, use it — it gives
  the job-level "what ran, in what order" graph for free. It is **not** a
  substitute for the run-log table's execution diagnostics, and it does not
  survive the orchestrator's own log retention.

---

## 13. Reference — the non-obvious mechanics, collected

| Mechanic | Why it exists |
|---|---|
| Write-to-staging then mark-extracted, **two transactions** | Crash between them = harmless duplicate (natural key dedups). Reverse order = permanent row loss. |
| Advisory lock: try-lock per chunk (Stage 1) vs. blocking whole-run (Stage 2) | Stage 2 truncates staging; it must hold the lock across read+process+write+truncate or Stage 1's writes get wiped. |
| Bulk `COPY` everywhere, never row-by-row `INSERT` | A row-by-row loop pins a pooled connection for its whole duration; adds real latency, especially on the request path. |
| Production write via TEMP table + `INSERT ... SELECT ... ON CONFLICT` | A parameterized multi-row `INSERT` hits Postgres's 65,535-parameter limit above ~6,553 rows. Routine at a 10k chunk size. |
| In-chunk last-wins dedup **before** the upsert | Postgres refuses `INSERT ... ON CONFLICT DO UPDATE` that touches the same row twice in one statement. |
| Dedup losers → waiting_pipeline, not `/dev/null` | Every submitted value must remain visible to a human. |
| `text` cast type: non-empty, max 256 chars | A long high-entropy indexed string exceeds the btree entry limit and jams the shared chunk insert permanently — a one-row DoS. |
| Partition creation via `PROCEDURE` with periodic `COMMIT` | A `DO` block holds one lock per partition and exhausts `max_locks_per_transaction` over a multi-year range. |
| `pg_advisory_xact_lock` (not session-level) | Auto-releases on commit/rollback; a reset pooled connection can't leak it. |
| Per-chunk run-log write on a **separate** connection | Must survive the main transaction's rollback so the crash diagnostic isn't lost. |
| Auth dependency is sync, not async (FastAPI) | An async dependency runs on the event loop; a blocking DB call in it stalls the whole service per request. |

---

## 14. Configuration knobs — set these per product

| Knob | Typical value | Notes |
|---|---|---|
| Natural-key grain | `(source_id, entity_id, window_date, window_bucket)` | Whatever "one settled fact" means in your domain. |
| Time bucket | 1 hour | Sources pre-aggregate to this before submitting. |
| Chunk size | 10,000 | Drives the 65k-parameter math — keep the temp-table write regardless of the value you pick. |
| Stage 1 schedule | every 1–2 min | Bounds worst-case ingestion latency. |
| Stage 2 schedule | hourly | Match the production grain exactly. |
| Partition range / grain | daily, ~10-year span | Stored procedure with periodic `COMMIT`. |
| `text` field cap | 256 chars | Must stay well under Postgres's ~2704-byte btree entry limit for any indexed string field. |
| "Settled" boundary | before the current UTC calendar day | The line past which a touch needs human review. |
| Advisory lock key | `hashtext('<source>_staging')` | One distinct key per source. |
| Waiting pipeline | on for high-stakes sources | Optional per source — record the decision either way. |
| Entity resolution | off by default (`entity_id` = source's raw string) | Turn on only if you need cross-source entity identity; see below. |
| Connection mode | API push and/or table pull | §4; both converge at staging. |

**On entity resolution (default: off).** By default `entity_id` is the source's
own raw string, carried unchanged all the way to production, and the batch path
does **not** verify it against any registration table. This keeps the ingest path
simple and lets you observe real data flowing before adding correctness checks.
Unregistered or malformed entity identifiers are found by a **periodic
reconciliation scan** (count of `entity_id`s in production absent from your
registry), not by synchronous rejection. Turn on synchronous resolution/rejection
only when you have a concrete need for a shared surrogate identity across sources.

---

## 15. Adversarial test checklist (run every case against a live database)

A stage is not done until its cases pass. Build a test that actually executes
each — do not reason about them in the abstract.

1. **Tab / newline embedded in a string field** → survives `COPY` intact, no
   corruption.
2. **NUL byte (`\x00`) inside a string value** → Postgres rejects it. Decide your
   handling: on the API push path this happens *before* quarantine is reachable,
   so it needs an explicit content check in the API layer returning a clean
   `400`, not a generic `500`. (Note: a NUL byte is a character *inside* an
   otherwise-present string — a `NOT NULL` constraint does not address it.)
3. **Empty string (`""`) in a required field** → quarantined, does not flow
   through as a meaningless value.
4. **Negative measure where only non-negative makes sense** → quarantined
   (`nonneg_int32`).
5. **Long *compressible* string (~3000 repeated chars) in an indexed field** →
   lands fine; Postgres compresses it under the btree limit.
6. **Long *incompressible* (~3000 random chars) string in an indexed field** →
   caught per-row by the `text` cap and quarantined; **never reaches the bulk
   insert**. This is the pipeline-wide DoS the cap defends against — verify by
   running Stage 2 twice and confirming it does not crash on either run.
7. **N concurrent submissions racing for the same slot** → exactly 1 winner in
   production, N−1 parked in `waiting_pipeline`, zero data loss.
8. **A chunk that is 100% bad rows** → all quarantined, no crash on the
   empty-`good`-list path.
9. **A revoked / expired API key** → clean `401` (API push mode).
10. **Byte-for-byte identical row twice in one request** → both accepted;
    resolved downstream by the in-chunk last-wins dedup, loser →
    `waiting_pipeline`.
11. **More concurrent requests than the connection-pool size** → queue
    gracefully, all succeed, no timeouts.
12. **Simulated mid-run crash (chunk 2 of 2 fails)** → run log shows chunk 1
    `success` and chunk 2 `failed` with distinct `batch_id`s (not one opaque
    row); production is unchanged; staging is still full, ready for the retry.
13. **A previously-quarantined row, corrected and re-submitted as a new batch** →
    lands normally; the original quarantine entry is untouched (audit trail
    intact).
14. **A settled-window restatement with an *identical* value** → still routed to
    `waiting_pipeline`, not silently confirmed.

---

## 16. Minimal DDL sketch (generic, Postgres)

Illustrative — adapt column names and domain fields to your product.

```sql
-- ============ isolated intake schema (API push mode) ============
CREATE SCHEMA IF NOT EXISTS intake;

CREATE TABLE intake.sources (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE intake.source_api_keys (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id   BIGINT NOT NULL REFERENCES intake.sources(id),
    key_hash    TEXT NOT NULL UNIQUE,
    issued_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ,
    revoked_at  TIMESTAMPTZ
);

-- append-only; never truncated; all source-supplied fields loose text
CREATE TABLE intake.inbox (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id     BIGINT NOT NULL REFERENCES intake.sources(id),
    batch_id      TEXT NOT NULL,
    entity_id     VARCHAR NOT NULL,
    window_start  VARCHAR,
    window_end    VARCHAR,
    measure       VARCHAR,
    unit          TEXT,
    confidence    VARCHAR,
    received_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    extracted_at  TIMESTAMPTZ
);
CREATE INDEX ON intake.inbox (extracted_at) WHERE extracted_at IS NULL;

-- table-pull mode: high-water mark per source
CREATE TABLE intake.pull_cursor (
    source_id     BIGINT PRIMARY KEY REFERENCES intake.sources(id),
    last_cursor   BIGINT NOT NULL DEFAULT 0,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============ core warehouse schema ============
CREATE SCHEMA IF NOT EXISTS core;

-- loose, persistent retry buffer
CREATE TABLE core.staging (
    source_id     BIGINT,
    entity_id     VARCHAR,
    batch_id      TEXT,
    window_start  TEXT,
    window_end    TEXT,
    measure       TEXT,
    unit          TEXT,
    confidence    TEXT,
    loaded_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- typed, partitioned, natural-keyed correctness boundary
CREATE TABLE core.production (
    id            BIGINT GENERATED ALWAYS AS IDENTITY,
    source_id     BIGINT NOT NULL REFERENCES intake.sources(id),
    entity_id     VARCHAR NOT NULL,
    batch_id      TEXT NOT NULL,
    window_start  TIMESTAMPTZ NOT NULL,
    window_end    TIMESTAMPTZ NOT NULL,
    window_date   DATE NOT NULL,
    window_bucket SMALLINT NOT NULL CHECK (window_bucket BETWEEN 0 AND 23),
    measure       INTEGER NOT NULL,
    unit          TEXT NOT NULL,
    confidence    NUMERIC(3,2) CHECK (confidence BETWEEN 0 AND 1),
    restated      BOOLEAN NOT NULL DEFAULT false,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id, window_date),
    UNIQUE (source_id, entity_id, window_date, window_bucket)
) PARTITION BY RANGE (window_date);
-- + a stored procedure that creates daily partitions and COMMITs every ~500

-- settled-window restatements awaiting a human decision
CREATE TABLE core.waiting_pipeline (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id     BIGINT NOT NULL REFERENCES intake.sources(id),
    entity_id     VARCHAR NOT NULL,
    batch_id      TEXT NOT NULL,
    window_start  VARCHAR,
    window_end    VARCHAR,
    window_date   DATE NOT NULL,
    window_bucket SMALLINT NOT NULL CHECK (window_bucket BETWEEN 0 AND 23),
    measure       VARCHAR,
    unit          TEXT,
    confidence    VARCHAR,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','approved','rejected')),
    resolved_by   TEXT,
    resolved_at   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON core.waiting_pipeline (status) WHERE status = 'pending';

-- ============ shared, cross-source ============
CREATE TABLE public.quarantine (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source        TEXT NOT NULL,
    destination   TEXT NOT NULL,
    raw_data      JSONB NOT NULL,
    error         TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE public.run_log (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dag_id        TEXT NOT NULL,
    run_id        TEXT NOT NULL,
    batch_id      TEXT,
    started_at    TIMESTAMPTZ,
    ended_at      TIMESTAMPTZ,
    status        TEXT NOT NULL CHECK (status IN ('success','failed','running')),
    error_message TEXT,
    triggered_by  TEXT
);
```

---

## 17. Build order

1. Schemas + tables (§16), including the partition procedure.
2. Shared helpers: pooled DB access, `copy_chunk`, `validate_row` +
   `quarantine_rows`, `run_log` writer + `record_chunk`.
3. Connector for your first source — API service + inbox (§4.A) **or** table-pull
   connector + cursor (§4.B).
4. `extract_to_staging` for that source (§5), with the advisory try-lock.
5. `staging_to_production` for that source (§7), with the blocking lock, the five
   per-chunk steps, the temp-table upsert, and the waiting-pipeline `COPY`.
6. Run the full adversarial checklist (§15). Fix what it finds before moving on.
7. `production_to_accumulated` + the shared BI stage (§9).
8. The waiting-pipeline admin tool (§11) and its separate auth model.
9. Status read-back endpoint (§4.A), if using API push.
10. Onboard the second source by repeating steps 3–6 only — nothing shared should
    need to change.
```
