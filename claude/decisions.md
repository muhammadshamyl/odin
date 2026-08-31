# Design Decisions Log
**Back to:** [claude.md](claude.md)

Records locked decisions from the module planning sessions. Newest session on top.

---

## ⏳ Awaiting response

| Ref | Question | Recommendation |
|-----|----------|----------------|
| D1.3b | Retry count + intervals for the source → file extraction step | 3 attempts at 5 / 15 / 45 min, then alert + pause |
| D2.2 | Archive processed files to cold storage before deletion, or delete outright | Archive to cold ~90 days |
| D3.2 | Concurrent-run schema versioning: pin at run start / latest-under-lock / copy-on-run snapshot | **a** – pin at run start (or **c** for audit trail) |
| D4.2′ | Business range/value rules now default **SOFT** (content-is-truth), reversing the earlier HARD default — confirm | Confirm |
| D4.text | `text` cap = 256 chars (spec default) — confirm it clears our DB's btree entry limit | Confirm |
| D6.4 | Old table after migration swap: archive+policy / drop after verify / archive 7 days then auto-drop | **c** |
| D8.x | Mid-chain failure recovery; extraction concurrency cap; per-layer SLA; migration size threshold | from-failed-stage / cap / per-pipeline / (tbd) |
| Onb.1 | Large table with no cursor and no timestamp = onboarding blocker (no silent forever-FULL) — confirm | Confirm |
| Onb.2 | Waiting-pipeline reviewer assignment — per source, by whom? | tbd |

**Superseded:** D5.1 (snapshot retention) — staging is now ephemeral, no snapshots.

---

## Session 6 — 2026-08-31 — Physical naming, one-click load, live dashboard (from testing)

Implementation refinements from hands-on testing of the Slice 1 web UI. Applied to Modules 3, 8, 9, 10, claude.md, frontend-inventory.md, BUILD_STATUS.md.

- **Physical table naming — one schema per layer, no source prefix.** Was `staging_<source>_<table>` in `public`; now `staging.<t>` / `production.<t>` / `quarantine.<t>` / `waiting.<t>` where `<t>` = `naming.slug(table_name)`. The source is registry metadata, not part of the physical name. **Table names are globally unique** — onboarding refuses a name whose layer tables already exist (`to_regclass` pre-flight). Legacy dot-less values still resolve as bare `public` identifiers. Migration `sql/004_layer_schemas.sql`. _(applied: Module 3; `odin/naming.py`, `ddl.py`, `transform.py`, `resolve.py`, `extract.py`, `registry.py`)_
- **A manual trigger targets production, not staging.** User's rule: "the goal is not staging, it's production." The table page's primary action is **"Load to Production"** — pick a file → one `ingest` job runs extract → transform → `TRUNCATE staging` under one `run_id` (so the Monitor shows one row with both stage pills; extract-success + transform-fail = green + red). If staging still holds un-transformed rows, an inline **"Flush & load"** confirm runs a `transform` then the `ingest` (ordered by the single worker). Standalone extract / transform kept but demoted to "Manual steps" (still needed for quarantine re-inject). _(applied: Module 8 §8.3, Module 10 §10.6a; `web/app.py` `/t/{s}/{t}/load`, `_load_confirm.html`, `app.js` `data-autosubmit`)_
- **Dashboard KPI cards must self-refresh.** The table page's staging/production/waiting/quarantine counts were static and went stale after a run while the lineage animation updated. Now a partial (`_data_stats.html` + `GET /partials/stats/{s}/{t}`) that refreshes on the same `odin:runs-changed` event as the runs panel and lineage. _(applied: Module 9, Module 10 §10.3)_
- **Delete pipeline** is available from the table page (not only the SQL Console danger zone) — typed-name confirm, drops the four layer tables + registry/run_log rows in one transaction, blocked while a job runs. _(applied: Module 10 §10.6b)_
- **SQL Console** documented as built (Module 10 §10.8, previously only in BUILD_STATUS): classify → read-only / commit-or-discard / typed-name confirm for DROP·TRUNCATE, `sql_console_log` audit, deregister danger zone, CodeMirror editor, fixed-height dual-scroll result grid.

- **Composite natural key for INCREMENTAL routing.** A 2M-row same-file reload took **3h 37m** — the old routing looped once per distinct existence value (881), each a full scan of unindexed staging. Fix: a DE-chosen **ordered list of key columns** (`registry_tables.natural_key`, comma-separated) that supersedes the single `existence_check_column`. Both production and `waiting.<t>` carry a hashed **`nk bigint`** column (btree-indexed):
  `hashtextextended(concat_ws(chr(31), coalesce((col::type)::text, chr(1)), …), 0)::bigint` — same expression on both sides. Routing becomes one indexed set-based `EXISTS` join (+ a raw-column `IS NOT DISTINCT FROM` tie-break so a 64-bit hash collision can't mis-route), not a per-value loop. 50k same-file reload: **1.0 s**. Key columns cannot be `numeric`/`unit_interval` (text form not scale-stable). Waiting grain for natural-key pipelines is **one batch per run**; the legacy single-column path is unchanged. Onboarding: **three `nk_col` dropdowns** (native `<select>`, replacing a per-column checkbox wall) — a chosen column greys out in the other two, a chip line previews the composite key, each option shows its type synced to the type selects; + CLI `--natural-key "a,b"`. Fewer than two distinct picks is allowed and collapses to a single-column key. _(applied: Module 3 §3.4a, Module 6 §6.2/6.4, Module 4 §4.4, Module 11; `odin/ddl.py` `natural_key_sql`/`natural_key_match`/`nk_index_ddl`, `transform._load_incremental_nk`, `resolve.approve_waiting`/`production_rows_for`/`waiting_compare`, `registry.onboard_file_source`/`retype_table`, `web/sqlview.py`, `templates/preview.html`)_
- **Numeric table columns are centre-aligned** (run log, deck health row, per-stage counts) — was right-aligned and read as misplaced against the mono headers. `table.data td.num` / `th.num` in `app.css`; numeric `<th>`s tagged `class="num"` in `_macros.html`.

**Not bugs, clarified during testing:** reloading a `FULL_SNAPSHOT` pipeline replaces its snapshot for that `load_date` — it does **not** route to `waiting` (that's INCREMENTAL only); `load_date` is the ingestion date, not a CSV column; staging reading 0 on a loaded pipeline is correct (truncated after every confirmed transform).

---

## Session 5 — 2026-08-29 — Set-based checks, per-table diversions, load-type routing

Refines Session 4's period model while building Slice 1. All applied to Modules 3, 4, 6, 11, claude.md.

- **No row-by-row checks, ever.** "First protocol for a smart engine." Every structural check and every routing decision at the transform is **one set-based SQL statement over the whole staging batch** — `... WHERE char_length(col) > :cap OR ...`, `INSERT ... SELECT`, `DELETE ... WHERE EXISTS`. No Python row loop. _(applied: Modules 4, 6, 11)_
- **Diversions are per-table, each in its own schema:**
  - `quarantine.<tbl>` — `LIKE` the staging table (all VARCHAR + staging metadata) + `qbatch_id`. Like a staging table, but for bad data.
  - `waiting.<tbl>` — `LIKE` the production table (exact copy) + `wbatch_id`.
  - The **failure reason + row counts** live in control-plane batch-log tables, not in the diversion table: `quarantine_batch_log` (`qbatch_id`, `run_id`, `reason`, `row_count`, `resolution_status`) and `waiting_batch_log` (`wbatch_id`, `run_id`, `existence_value`, `row_count`, `status`). Both in `001_core.sql`.
  - The old shared `quarantine` table and the `waiting_pipeline_slice` / `waiting_pipeline_rows` JSONB pair are **removed**.
- **Considered but rejected: a separate quarantine *database*.** PostgreSQL can't write/join across databases in one connection or transaction; `postgres_fdw`/`dblink` commit separately and break atomicity. A **schema** in the same `odin` database gives the same isolation and keeps the quarantine write inside the transform's transaction.
- **No computed `period`.** The `period` column and all `date_trunc` slicing are dropped from production and the transform. Only system `load_date` + `load_timestamp` are stamped on insert. `registry_tables.period_column` / `period_grain` **removed**.
- **Routing is DE-configured** — new `registry_tables` columns `load_type` (`FULL_SNAPSHOT` | `INCREMENTAL`) and `existence_check_column` (a source date column; INCREMENTAL only):
  - **`FULL_SNAPSHOT`** — each snapshot is stored with its `load_date`. Same `load_date` already in production → delete those rows, insert the new snapshot; else just insert. Existence column irrelevant; nothing goes to `waiting`.
  - **`INCREMENTAL`** — collision unit is the **exact value** of `existence_check_column`. Value already in production → those rows → `waiting.<tbl>` + one `waiting_batch_log` row per value; value new → insert. Approve = delete production's rows for that value, re-insert the waiting rows with `restated = TRUE`; reject = drop them.
- **run_log** for the transform is **one row per run** (start `running` → finish `success`/`failed` with counts), still on the separate autocommit connection so a rolled-back run still records how far it got. Per-chunk granularity was a streaming-Python concept; the set-based transform has no chunks.

**Superseded:** Session 4's "period-level collision routing" (slice by `period`, `(source, period)` existence check, `waiting_pipeline_slice`/`_rows`) — replaced by `load_type` + `existence_check_column` routing into per-table `quarantine.*` / `waiting.*` schemas.

---

## Session 4 — 2026-08-29 — Slice plan, period routing, no-CDC, no-retry

- **No CDC.** Change Data Capture is not used — costly to run, and nothing in the design needs per-row operation flags. `cdc_operation`, `is_deleted`, and all INSERT/UPDATE/DELETE routing removed from Modules 1, 5, 6, 11. **Supersedes D6.1.** _(applied)_
- **No automatic retry, anywhere.** Any failure (connect, extract, load, transform) → stop, mark FAILED, alert, wait for the DE to fix the cause and re-trigger. The un-advanced marker means the next run naturally re-attempts. `retry_config` removed. **Supersedes D1.3 / D1.3b.** _(applied: Modules 1, 6, 8)_
- **Recovery is manual, always.** Nothing in `quarantine` or `waiting_pipeline` is auto-reprocessed, auto-retried, or auto-expired — a DE re-injects a corrected quarantine batch (new `batch_id`) or approves/rejects a waiting slice. "If it failed once it will fail again" — a re-run without a fix is pointless. _(applied: claude.md, Modules 4, 8, 10)_
- **Primary key is optional.** The DE registers a PK (and `NOT NULL`) only when needed; nothing infers or requires one. FULL extraction assumes no key — file read in row-count batches, DB table streamed in one pass. _(applied: Modules 1, 3, 6)_

- **Build order** — slices: (1) file (CSV/TXT) load & consume, end to end; (2) RDBMS (already designed — build & test); (3) NoSQL. Each slice reuses the same staging→production tail.
- **CSV/TXT ingestion** — header row → staging columns (VARCHAR); body → batched bulk loads. First load also seeds the registry + typed production table via the onboarding preview; later loads validate the header against the registry (Check 1). _(Module 1, 5)_
- **Step 2 (target types + row-level key) is deferred** to an optional "Configure" click — not on the critical path for slices 1–3. _(Module 3, 10)_
- **Period-level collision routing** (all slices, replaces the spec's row-level natural key as the base model):
  - A load is **sliced by `period`** (`registry_tables.period_column` + `period_grain`; none → whole table is one period `'__ALL__'`).
  - `(source, period)` **absent** from production → insert the slice. **Present** → the whole slice → `waiting_pipeline_slice` (pending) + raw rows → `waiting_pipeline_rows`.
  - Existence check only — no per-row lookup, no hashing.
  - Retry safety = one-transaction-per-run: a failed run rolls back its writes, so a retry sees the period absent.
  - `waiting_pipeline` review is **per slice**: Approve → replace production's rows for that `(source, period)`, set `restated = TRUE`; Reject → drop the slice.
  - Row-level upsert on a `natural_key` returns as a later "Configure" option. _(claude.md, Modules 3, 4, 5, 6, 10, 11)_
- **`load_date` + `load_timestamp`** — stamped by the system on **every row** at load time, on staging and production, recording *when the table was loaded* (independent of any business date). `load_date` is the production **partition column** (replaces `ingestion_date`). Older `ingestion_timestamp` / `production_load_timestamp` names retired. _(Modules 5, 6, 11)_

**Superseded:** the Session-3 settled-boundary model (`settled_column` / `settled_after` / `waiting_pipeline_enabled`, spec §7 same-day-upsert vs settled-review) — replaced by period-level routing where **any** existing period diverts. `#5 = C` now means "period config per table", with row-level `natural_key` as the deferred extra.

---

## Session 3 — 2026-08-28 — Reconciliation with INGESTION_PIPELINE_SPEC.md

The spec (a proven blueprint from a prior product) is now authoritative for the ingestion shape. Decisions:

- **Keep files** — extract writes batch files, a Python loader pulls them into staging in batches. The spec's inbox-table model applies only to a future API-push source. _(applied: Modules 1, 2, 5)_
- **Staging is ephemeral** — TRUNCATEd after each confirmed production load; a loose-text retry buffer, not a store. Supersedes the append-only / snapshot model and **D5.1**. _(applied: Module 5)_
- **Keep the `waiting_pipeline`** — a second diversion for structurally-valid rows that would overwrite settled history; human approve/reject; `restated` flag. Opt-in per table. _(applied: Modules 4, 6, 9, 10)_
- **Stack** — stay on Foundry Pipeline Builder + SQL; Python only for source→file→staging. Spec's Postgres mechanics (advisory locks, temp-table upserts, COPY, param/btree limits) are implementation details to translate. _(applied: claude.md, Modules 5, 6, 11)_
- **Extraction strategy** — per-table, DE picks at onboarding: **CURSOR** (keyset on a monotonic column) / **TIME_WINDOW** (fetch the slice matching the trigger interval; missed runs cover every gap window) / **FULL** (small reference tables). Extraction **never reads staging or production**; duplication is absorbed by the production natural-key upsert. _(applied: Modules 1, 3)_
- **TIME_WINDOW is the expected default**; settling lag default 0, configurable per source.
- **Production write** = last-wins upsert on the **natural key** (registry PK, plus window/partition fields for time-windowed tables). Existence-only routing: free → insert; occupied & not settled → upsert; occupied & settled & review-enabled → `waiting_pipeline`. In-chunk dedup losers → `waiting_pipeline`. _(applied: Modules 4, 6, 11)_
- **#5 = C** — the "settled boundary" is per-table registry config (`settled_column`, `settled_after`, `waiting_pipeline_enabled`); default for a new table = no boundary, plain upsert. _(applied: Module 3)_
- **Structural vs business validation** — Check 2 is structural only (cast, range as part of the type, required, length). Business range/value rules are **SOFT by default** now (flag + log, row proceeds), HARD opt-in per rule. Supersedes **D4.2**. _(applied: Module 4)_
- **Two diversions** — `quarantine` (structural, shared, raw JSON, re-inject as a new batch) + `waiting_pipeline`. _(applied: Module 4)_
- **`text` cap 256 chars** — per-row length check before the bulk write, to defuse the btree-entry-limit DoS. _(applied: Module 4)_
- **Per-source isolation** — one pipeline per source per stage; only the Business-layer merge is shared. _(applied: Module 8)_
- **Extract↔transform lock** per table — loader try-locks per batch; transform blocking-locks the whole run (read→process→write→truncate). _(applied: Modules 5, 6, 8)_
- **Run log** at `(run_id, batch_id)` grain, written per chunk on a separate connection so it survives the transform's whole-run rollback. _(applied: Module 9)_

### Onboarding & First-Load Wizard (from the user's flow)
- 7 steps: configure → connect & replicate → **1,000-row sample** into staging → preview & approve schema (+ natural key) → load config (strategy, window col/grain, settled boundary) → **recurring or one-time** → first real run.
- Sample lands in staging only; nothing reaches production until the DE approves _(a = rec)_.
- One-time loads: no schedule, optional bounded range; run once then INACTIVE; definition kept _(b, c)_.
- A **Task Scheduler** registers every pipeline (recurring + one-time) with schedule + status at onboarding. _(applied: Modules 1, 3, 8, 10)_

---

## Session 1 — 2026-08-28 — Modules 1–6 (DE)

### Module 1 — Extraction
- **D1.1 Batch size** — ~250 MB target per batch, 100k-row cap. Global default, overridable per source. _(applied)_
- **D1.2 Compression** — None. _(applied)_
- **D1.3 Retry split** — source → file retries; file → staging load alerts only, no retry. _(applied)_
  - **D1.3b** _(awaiting)_ — retry count + intervals.

### Module 2 — File Pre-Processing
- **D2.1 Staging file area retention** — 1 week, changeable. _(applied)_
- **D2.3 Malformed file** — reject whole, alert, prompt re-upload, record reason. _(applied)_
- **D2.2** _(awaiting)_ — archive vs delete processed files.

### Module 3 — Schema Registry
- **D3.1 Write access** — DE role only, per-user assignable. _(applied)_
- **D3.3 Registration** — auto-infer + 1,000-row sample preview + DE approve. _(applied)_
- **D3.4 Type-change verification target** — current production values. _(applied)_
- **D3.2** _(awaiting)_ — concurrent-run versioning.

### Module 4 — Data Quality
- **D4.1 Quarantine pause threshold** — 5%. _(applied)_
- **D4.3 Quarantine resolution owner** — DE; waiting_pipeline via separate review tool. _(applied)_
- **D4.2** — superseded by Session 3: structural failure → quarantine (binary); business rules SOFT by default. _(awaiting confirmation as D4.2′)_

### Module 5 — Staging
- **D5.2 Partitioning** — production only, by ingestion date; staging not partitioned. Settles **D6.6**. _(applied)_
- **D5.3 New source column** — drift alert + hold, DE updates registry. _(applied)_
- **D5.1** — superseded: staging is ephemeral, no snapshots.

### Module 6 — Production
- **D6.1 CDC DELETE** — per-table, default soft delete. _(applied)_
- **D6.3 Business rule approval** — AE for standardization, business sign-off for derived logic. _(applied)_
- **D6.5 Migration + in-flight load** — no freeze; reconcile delta after swap. _(applied)_
- **D6.6 Partition key** — ingestion date, fixed. _(applied)_
- **D6.2 Late-arriving upsert mechanism** — _deferred_, likely simple MERGE.
- **D6.4** _(awaiting)_ — old-table disposition after migration swap.

---

## Deferred to later sessions

- **Module 7 (Business Layer)** open questions — worked in the AE session.
- **Module 11** open questions — schema_version pinning, SQL dialect target, source-control of generated artifacts, ADD/REMOVE auto-regen, ownership of business-layer SQL.
- The spec's **§15 adversarial checklist** becomes the extraction + transform acceptance suite.
