# Frontend Feature Inventory
**Back to:** [claude.md](claude.md)

Every screen, view, form, action, table column, and configurable setting discussed so far, grouped into functional areas. Built on Foundry Workshop. Role column: **B**usiness user / **A**E / **D**E / **Adm**in.

Status: areas A–H and K reflect Modules 1–6 + cross-cutting, reconciled with INGESTION_PIPELINE_SPEC.md. Areas I–J (Business layer, parts of Admin) are stubbed pending the AE sessions.

**Slice 1 local web UI** (`src/odin/web/`, `odin web` — server-rendered FastAPI + Jinja2, no auth) currently implements a minimal cut of:
- **A. Onboarding wizard** → `/onboard` (upload → 50-row preview → `load_type` + `existence_check_column` → create, optional first run). Steps 5–7 (extraction strategy, recurring/one-time scheduling, Task Scheduler registration) not yet surfaced.
- **B. Schema Registry** → per-table page `/t/{source}/{table}` shows config + column list (read-only; no edit form / version history).
- **D. Pipeline Status** + **G. Run Log** → `/` and `/runs` (one row per `run_id`, per-stage pills; runs panel + lineage + table-page KPI cards self-refresh on `odin:runs-changed`). No health-summary grid, SLA, or size metadata.
- **E. Quarantine Review** → `/quarantine` + `/quarantine/{qbatch}` (held rows, re-inject / ignore). **Waiting-Pipeline Review** → `/waiting` + `/waiting/{wbatch}` (held rows vs production side by side, approve / reject).
- **F. Manual Run** → **"Load to Production"** on the table page (auto-submits on file pick → `POST /…/load`, one `ingest` run extract→transform→truncate; "Flush & load" confirm if staging is dirty). Standalone extract / transform under "Manual steps". Also a **Delete pipeline** danger zone.
- **SQL Console** → `/sql` — guard-railed ad-hoc SQL (classify → read-only / commit-or-discard / typed-name confirm for DROP·TRUNCATE), `sql_console_log` audit, deregister-source danger zone, CodeMirror editor.
Not yet built: C (type-change wizard — table-page re-typing exists), the D health grid, E scorecard / drift log / quality rules, the F Task Scheduler, most of G alert config, H file-area view, I–J.

---

## A. Onboarding & First-Load Wizard  (Module 1, 3, 8, 10) — DE-driven, 7 steps

**Step 1 — Configure source**
- Fields: `source_name`, `source_type` (RDBMS / NoSQL / nested-file / structured-file), connection details, `owner`, description
- Table picker: which tables to extract (multi-select)

**Step 2 — Connect & replicate** *(system)*
- Connect, read source structure, auto-infer columns + types, create staging + production shells

**Step 3 — Sample load** *(system)*
- Extract the **top 1,000 rows** into staging (preview only — nothing reaches production)

**Step 4 — Preview & pick load type**
- Grid: the 1,000 sampled rows
- Pick **`load_type`** — `FULL_SNAPSHOT` or `INCREMENTAL`
- If `INCREMENTAL`: pick the **`existence_check_column`** (a date column from the header)
- *"Configure" (optional, deferred):* per-column `target_data_type` / `is_nullable`, and a row-level `natural_key`
- Action: **Approve** — D

**Step 5 — Load config**
- `extraction_strategy` (CURSOR / TIME_WINDOW / FULL)
- CURSOR: `cursor_column` · TIME_WINDOW: `window_column` + `window_grain` + `settling_lag_minutes`
- `batch_size` override

**Step 6 — Recurring or one-time**
- Recurring → interval (= TIME_WINDOW size) + start time → ACTIVE `schedule_control` row
- One-time → no schedule; optional bounded date range; runs once then INACTIVE

**Step 7 — First real run** *(system)* — full first batch → staging → transform → production; pipeline registered in the Task Scheduler

---

## B. Schema Registry  (Module 3)

**View — Registered Sources**
- Columns: `source_id`, `source_name`, `source_type`, `owner`, `status` (ACTIVE / PAUSED / DEPRECATED), `registered_date`, `last_updated`
- Action: **Change status** (activate / pause / deprecate) — D, Adm

**View — Tables per Source**
- Columns: `table_name`, `staging_target`, `production_target`, `partition_key` (=`load_date`), `load_type`, `existence_check_column`, `extraction_strategy`, `window_column`/`window_grain`, `load_recurrence`, `natural_key` (blank until "Configure"), `status`
- *As built:* `staging_target` / `production_target` are qualified `schema.table` — one schema per layer, table = slug of the DE-given name, no source prefix, globally unique (Module 3, "Physical targets & naming"). The per-table page also has a **Delete pipeline** danger zone (drops the four layer tables + registry/run_log rows in one transaction).

**View — Columns per Table**
- Columns: `column_name`, `column_order`, `source_data_type`, `target_data_type`, `is_nullable`, `is_primary_key`, `is_watermark` (cursor/window), `schema_version`, `effective_from`, `effective_to`

**Form — Preview & Pick Load Type** (onboarding wizard, step 4)
- Grid of the 1,000-row sample
- Pick `load_type` (`FULL_SNAPSHOT` / `INCREMENTAL`); if incremental, pick `existence_check_column`
- *"Configure" panel (optional, deferred):* per-column `target_data_type` / `is_nullable`; a row-level `natural_key`
- Actions: **Approve** / **Reject** — D

**Form — Manual Column Add / Edit**
- Fields: name, target type, nullable, PK flag, watermark flag, order
- Validation: duplicate / conflict check before save

**View — Schema Version History (per table)**
- Columns: version, change summary, `changed_by`, timestamp
- Action: **Diff two versions**

**View — Change Log**
- Columns: `change_id`, table, column, `change_type` (ADD / REMOVE / TYPE_CHANGE / RENAME), old value → new value, `changed_by`, `changed_timestamp`, `schema_version`

---

## C. Column Type-Change Wizard  (Module 3 §3.7, 4 §4.8, 6 §6.7, 8, 10 §10.7, 11)

**Form — Propose Type Change**
- Fields: production table, column, new data type
- Action: **Run verification (dry run)** — A, D

**View — Verification Result**
- `total_rows`, `would_fail_cast` count, sample of failing values, verdict PASS / FAIL
- Verification runs against current production values (D3.4)
- Actions: **Confirm change** (enabled only on PASS) / **Cancel**

**View — Post-Confirm Status**
- Registry committed (new `schema_version`)
- SQL regenerated → link to **SQL Diff** (old vs new generated transform SQL)
- Migration scheduled → window / immediate

**View — Migration Progress**
- Partitions total / rewritten, current partition
- Swap status (PENDING / COPYING / VERIFYING / SWAPPED / ARCHIVED)
- Started, ETA; old-table disposition (archived until date / dropped)

---

## D. Pipeline Status Dashboard  (Module 9, 10 §10.3)

**View — Health Summary Grid** (`pipeline_health_summary`)
- Per source_system + table: `extract_status`, `staging_status`, `production_status`, `business_status` (color-coded), `last_successful_run`, `total_quarantined`, `pending_restatements`
- Filters: source_system, table, date range, status

**View — Run Log** (`run_log`) — see area G
- Grain `(run_id, batch_id)`; per-chunk rows survive a whole-run rollback
- *As built:* the Monitor renders **one row per `run_id`** with a pill per stage (extract writes one run-level row, not per batch). The runs panel, the animated lineage, and the table-page KPI cards all self-refresh on the `odin:runs-changed` event. `sql_console_log` records every SQL Console statement.

**View — SLA Adherence**
- Pipeline, expected vs actual completion, breach flag, historical adherence %

**View — Table Size Metadata** (`table_size_metadata`)
- `table_name`, `layer`, `row_count`, `size_bytes`, `partition_count`, `measured_timestamp`

---

## E. Data Quality  (Module 4, 9)

**View — Quality Scorecard**
- Per source: rows to production / to waiting_pipeline / quarantined; quarantine-rate trend; pending-restatement backlog + oldest age; top failing fields + reasons
- Action: **Export report** — B, A, D

**View — Quarantine Review** (`quarantine_batch_log` + `quarantine.<tbl>`)
- Batch list columns: `qbatch_id`, `source_id`, `table_name`, `run_id`, `reason` (`over_length` / `empty_required:<col>` / `cast:<col>`), `row_count`, `created_at`, `resolution_status` (open / reinjected / ignored)
- Open a batch → its rows from `quarantine.<tbl>` (verbatim VARCHAR)
- Filters: source, table, date, reason
- Actions (A/D): **Re-inject** (corrected rows → *new batch*), **Ignore**, **Escalate**; bulk actions. No patch-in-place.

**View — Waiting-Pipeline Review** (`waiting_batch_log` + `waiting.<tbl>`) — *separate auth, assigned per source*
- Batch list columns: `wbatch_id`, `source_id`, `table_name`, `existence_value`, `row_count`, `run_id`, `status` (pending / approved / rejected), `created_at`, age
- Open a batch → its rows from `waiting.<tbl>` vs production's current rows for that `existence_value`, side by side
- Actions: **Approve** (delete production's rows for `existence_value`, re-insert the waiting rows with `restated = true`) / **Reject** (drop the batch)
- Filters: source, table, age; backlog + oldest-pending age

**View — File Reload Remediation**
- Failed file record with documented `error_message`
- Action: **Re-submit file for load** — A, D

**View — Schema Drift Log** (`schema_drift_log`)
- Columns: `drift_id`, `source_system`, `table_name`, `detected_timestamp`, `drift_type` (NEW_COLUMN / REMOVED_COLUMN / RENAMED_COLUMN / TYPE_CHANGE), `column_name`, expected vs actual, `resolution_status` (OPEN / ACKNOWLEDGED / REGISTRY_UPDATED), `resolved_by`
- Actions: **Acknowledge drift** / **Update registry from drift** — D

**View — Quality Rules** (`quality_rules`)
- Columns: `rule_id`, source, table, column, `rule_type` (RANGE / ALLOWED_VALUES / REGEX / CUSTOM), `rule_expression`, `severity` (SOFT default / HARD), `is_active`
- Form (D): create / edit rule; set severity; toggle active

---

## F. Task Scheduler & Orchestration  (Module 8, 10 §10.6)

**View — Task Scheduler** (`schedule_control`) — the pipeline registry
- Columns: `pipeline_name`, `source_id`, `table_name`, `stage` (EXTRACT / STAGING / PRODUCTION / BUSINESS / MIGRATION), `schedule_type`, `load_recurrence` (RECURRING / ONE_TIME), `cron_expression`, `is_active`, `status` (ACTIVE / PAUSED / FAILED / INACTIVE), `last_run_timestamp`, `next_run_timestamp`, `owner`
- Actions (D): **Pause / Resume**, **Edit schedule** (override), **Manual trigger**, **Re-run one-time load**, **Convert one-time → recurring**

**Action — Manual Run**
- Select source + table; confirmation dialog; immediate run status — A, D, Adm
- *As built:* the table page's **"Load to Production"** drop-zone (auto-submits on file pick) → `POST /t/{s}/{t}/load` → one `ingest` job (extract → transform → truncate) under one `run_id`. If staging is non-empty, an inline **"Flush & load"** confirm first. Standalone Extract-only / Run-transform kept under "Manual steps". Every manual trigger targets production, not staging. (Module 8 §8.3, Module 10 §10.6a.)

**View — Pipeline Dependencies** (`pipeline_dependencies`)
- `child_pipeline`, `parent_pipeline`, `wait_for_status`, `timeout_minutes`

**View — Failed Runs**
- FAILED `run_log` rows: stage, source/table, `error_message`, how far it got
- Action (D): **Re-trigger** after fixing the cause — no auto-retry exists

**View — Migration Queue**
- Scheduled migrations: table, size, scheduled window, status

---

## G. Monitoring & Alerting  (Module 9, 10 §10-Home)

**View — Home Dashboard**
- Pipeline health overview, recent alerts, quick stats (rows processed today, quarantine rate, active pipelines)

**View / Form — Alert Config** (`alert_config`)
- Columns: `alert_id`, `pipeline_name`, `alert_type` (FAILURE / SLA_BREACH / QUARANTINE_SPIKE / WAITING_BACKLOG / SCHEMA_DRIFT), `threshold_value`, `alert_channel`, `recipients`, `is_active`
- Form (D): create / edit; set channel (email / Slack), recipients, threshold, toggle

**View — Run Log** (`run_log`)
- Grain `(run_id, batch_id)`; columns: `dag_id`, `stage`, `source_system`, `table_name`, `started_at`, `ended_at`, `status`, `rows_processed`, `rows_to_production`, `rows_to_waiting`, `rows_quarantined`, `error_message`, `triggered_by`
- Shows per-chunk progress even after a whole-run rollback

**View — Alert History / Feed**

**View / Form — Data Freshness** (`freshness_config`)
- `table_name`, `layer` (PRODUCTION / BUSINESS), `expected_frequency` (HOURLY / DAILY / WEEKLY), `max_delay_minutes`, `alert_on_breach`

---

## H. Staging File Area  (Module 2)

**View — Staging File Control** (`staging_file_control`)
- Columns: `file_id`, `source_system`, `table_name`, `file_path`, `file_format` (CSV / TXT / XML / JSON / SHP), `landing_timestamp`, `processing_status` (PENDING / PROCESSING / DONE / FAILED), `processed_timestamp`, `row_count`, `file_size_bytes`, `error_message`
- Filters: status, source, format, date
- Action: **Retry failed file load** / **View error** — B, A, D

---

## I. Business Layer  *(stub — pending AE sessions)*  (Module 7)

- View — Metrics Registry (`metrics_registry`): `metric_id`, `metric_name`, `metric_formula`, `grain`, `source_tables`, `owner`, `is_active`
- View — Join Registry (`join_registry`): `join_id`, `join_name`, left / right table, `join_type`, keys, conditions, `owner`, `is_active`
- View — Business Tables list + Ontology mapping opt-in flag
- (metric browsing for business users — Module 10)

---

## J. Admin / RBAC / Generated SQL  *(partial — pending)*

**View — Role-Based Access Matrix** (feature × role: B / A / D / Adm)

**Form — User Role Assignment**
- Owned by IT/security; DE requests _(D10.2 — TBD)_

**View — Generated SQL Store** (`generated_sql`)
- Columns: `pipeline_name`, `source_id`, `table_name`, `artifact_type` (STAGING_DDL / PRODUCTION_DDL / QUARANTINE_DDL / WAITING_DDL / TRANSFORM / SCOPING_QUERY), `schema_version`, `sql_text`, `generated_timestamp`, `generated_by`, `is_current`
- Actions: view SQL, view diff, (re)generate — D

---

## K. Configurable Settings

Every knob that must be editable from the UI. Scope + status noted.

| Setting | Scope | Decision |
|---------|-------|----------|
| Extraction batch size | global default + per source | ~250 MB / 100k-row cap (D1.1) |
| Extraction strategy | per table | CURSOR / TIME_WINDOW / FULL, set at onboarding |
| Cursor column / window column + grain | per table | onboarding |
| TIME_WINDOW settling lag (min) | per source | default 0 |
| Load recurrence | per table | RECURRING / ONE_TIME, set at onboarding |
| `load_type` | per table | `FULL_SNAPSHOT` / `INCREMENTAL`, set at onboarding (#5 = C) |
| `existence_check_column` | per table | INCREMENTAL only; the date column matched (exact value) against production |
| `natural_key` (row-level upsert) | per table | blank in base build; set via "Configure" to switch off load-type routing |
| `load_date` / `load_timestamp` | every row, system-stamped | records when the table was loaded; `load_date` is the partition column |
| Automatic retry | — | none anywhere; failure → alert → DE re-triggers |
| File compression | — | none (D1.2) |
| Staging file area retention (days) | global | 1 week, changeable (D2.1) |
| Processed-file archive vs delete + retention | global | archive to cold ~90 days (D2.2 — awaiting) |
| Quarantine rate pause threshold | global | 5% (D4.1) |
| Quality-rule severity | per rule | SOFT default, HARD opt-in (D4.2′ — awaiting) |
| `text` field length cap | global | 256 chars (D4.text — awaiting) |
| Partition key | fixed | ingestion date (D5.2) |
| Migration old-table disposition + window | global | TBD — rec archive 7 days then drop (D6.4) |
| Migration run-now vs defer threshold | global | TBD (D8.x) |
| Concurrent-run schema versioning mode | global | TBD (D3.2) |
| Extraction concurrency cap | global | TBD (D8.x) |
| Per-pipeline SLA | per pipeline | TBD (D8.x) |
| Alert channels / recipients / thresholds | per alert | TBD |
| Freshness SLA | per table | TBD (Module 9 §9.5) |
| Schema version pinning | per pipeline | TBD (D3.2 / D11.1) |
