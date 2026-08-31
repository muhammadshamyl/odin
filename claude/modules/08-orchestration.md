# Module 8: Orchestration
**Owner:** Data Engineering (DE)
**Layer:** Cross-cutting
**Back to:** [claude.md](../claude.md)

---

## Purpose

Controls when and how pipelines run. Manages dependencies between modules so that Staging never runs before extraction completes, Production never runs before Staging is ready, and the Business layer never runs before Production is clean. Also handles failure alerting (no automatic retry), manual triggers, and scheduling the online schema migration (Module 6, §6.7).

---

## Key Design Decisions

- **Per-source isolation** — one pipeline per source per stage; a failure, schema change, or load spike in one source's pipeline never cascades into another's. Enforced structurally. The only deliberately shared stage is the Business-layer merge (Module 7 / 9).
- A **Task Scheduler** is the registry of every pipeline (recurring or one-time) — its stage, schedule, status, last run, next run. A pipeline is registered here when its source is onboarded (Module 10).
- Pipeline execution is dependency-driven, not purely time-driven; each stage waits for upstream confirmation
- **Extraction cadence is decoupled from input cadence** — the extract stage runs on a frequent fixed schedule; a burst of input produces one extraction run, not one per item
- The **staging → production transform runs at production's grain** (e.g. hourly); running more often just re-touches a still-forming bucket
- An **extract↔transform lock** per table serialises the loader and the transform (Module 5 §5.6, Module 6 §6.1)
- **No automatic retry** — a failed run stops and alerts; the DE fixes and re-triggers (§8.5). Manual triggers via Module 10.

---

## Features to Build

### 8.1 Task Scheduler & Pipeline Registry
- Every pipeline is registered here at onboarding — one row per source per stage
- Holds schedule, recurrence, status, last/next run; the single place to see what runs when
- Foundry's native scheduling is the execution engine; this table is the control plane

```sql
CREATE TABLE schedule_control (
    schedule_id         VARCHAR,
    pipeline_name       VARCHAR,
    source_id           VARCHAR,
    table_name          VARCHAR,
    stage               VARCHAR,   -- EXTRACT, STAGING, PRODUCTION, BUSINESS, MIGRATION
    schedule_type       VARCHAR,   -- TIME, EVENT, DEPENDENCY, MANUAL, MIGRATION
    load_recurrence     VARCHAR,   -- RECURRING, ONE_TIME
    cron_expression     VARCHAR,   -- for RECURRING; interval also = the TIME_WINDOW size
    is_active           BOOLEAN,
    last_run_timestamp  TIMESTAMP,
    next_run_timestamp  TIMESTAMP,
    status              VARCHAR,   -- ACTIVE, PAUSED, FAILED, INACTIVE (one-time, done)
    owner               VARCHAR
)
```

- **One-time pipelines**: run once, then `status = INACTIVE`. The definition is kept — a DE can re-run it manually or convert it to RECURRING.
- **TIME_WINDOW missed-run catch-up**: if a run was skipped, the next run covers every window from `last_window_end` to `now − settling_lag`, oldest first — no gaps.

### 8.2 Dependency Management
- Defines parent-child relationships between pipelines
- Child pipeline only starts when parent completes successfully
- Dependency chain: Extraction → Pre-Processing → Staging → Production → Business

```sql
CREATE TABLE pipeline_dependencies (
    dependency_id       VARCHAR,
    child_pipeline      VARCHAR,
    parent_pipeline     VARCHAR,
    wait_for_status     VARCHAR,   -- SUCCESS, SUCCESS_OR_PARTIAL
    timeout_minutes     INTEGER
)
```

### 8.3 Trigger Types
- **Time-based**: runs at the configured interval (frequent + fixed for extract; production's grain for the transform)
- **Event-based**: runs when a new file lands in the staging file area
- **Dependency-based**: runs when the upstream stage succeeds
- **Manual**: triggered by a user via the self-service interface
- **Onboarding first-run**: the first real extraction after the DE approves the sample preview (Module 10 §10.1)
- **Migration**: the online schema migration, scheduled by table size (see 8.7)

**As built (Slice 1, 2026-08-31).** The web UI dispatches to an **in-process
single-worker job runner** (`odin/jobs.py`, `ThreadPoolExecutor(max_workers=1)`);
the HTTP request returns immediately and the runs panel polls. Job kinds:
`extract`, `transform`, and **`ingest`** — `ingest` runs extract then (only if a
batch loaded) transform under **one `run_id`**, so a normal load is a single
Monitor row with both stage pills.
- **A manual trigger targets production, not staging.** The table page's primary
  action ("Load to Production", Module 10 §10.6a) always submits `ingest` — pick a
  file → staging → transform → production → `TRUNCATE staging`, one run. Standalone
  `extract` / `transform` are secondary ("Manual steps"), kept for staging
  inspection and for re-running the transform after a quarantine re-inject.
- **Flush-before-load.** If staging still holds un-transformed rows when a new load
  is requested, the UI confirms, then submits a `transform` job followed by an
  `ingest` job; the single worker guarantees they run in order (drain, then load).
- `run_id` is minted per invocation; a threaded-in id is only shared within one
  `ingest` job. `triggered_by` is constrained to
  `scheduled | manual | onboarding | backfill`.

### 8.4 Pipeline Parameterization
- Each pipeline accepts runtime parameters
- Parameters include: batch_id, source_system, table_name, run_date
- Passed through from orchestration layer to individual transforms

### 8.5 Failure Handling — no automatic retry
- Any stage failure → the run is marked FAILED, an alert is raised, and the pipeline stops. The system does **not** retry.
- Rationale: a non-transient failure just fails again; the DE inspects it, fixes the cause, and re-triggers (manual trigger, or the next scheduled run picks up the un-advanced marker).
- Nothing in `quarantine` or `waiting_pipeline` is auto-reprocessed — those are cleared only by a DE action (Module 4, Module 10).

### 8.6 Job Group Management
- Groups related pipelines into a single logical job
- Job group runs in defined sequence
- Single status view for entire job group
- Example job group: `daily_erp_refresh` contains extraction + staging + production + business

### 8.7 Online Schema Migration Scheduling
- Triggered when Module 3 commits a verified column type change
- Reads target table size from Module 9 metadata
- Small tables run immediately; large tables are deferred to a configured low-activity window
- Runs the Module 6 rewrite process (partition-by-partition copy → atomic swap) as a standalone job, outside the normal layer chain

---

## Execution Order

```
Extract stage (frequent fixed schedule, per source)
   Module 1  →  batch files in staging file area
      ↓  [files registered PENDING]
File Pre-Processing (Module 2) + Check 1 header validation (Module 4)
      ↓  [PASS]
Load to staging (Module 5, Python, try-lock)
      ↓
Transform stage (production's grain, per source, blocking lock)
   Module 6 / Module 4:  cast → quarantine → route → dedup → upsert production + waiting_pipeline
      ↓  [all chunks ok → TRUNCATE staging]
Business Layer (Module 7)
      ↓
Monitoring update (Module 9)
```

The extract and transform stages are **separately scheduled**, not one chain — the transform picks up whatever the loader has landed since it last ran.

---

## Dependencies

- Module 1 through 7: All pipeline modules (orchestration governs all)
- Module 9: Monitoring (receives execution status updates; supplies table size for migration scheduling)
- Module 10: Self-Service Interface (manual trigger capability)
- Module 11: SQL Generation Engine (a verified change regenerates SQL, then orchestration schedules the migration)

---

## Resolved

- Per-source isolation — one pipeline per source per stage
- Task Scheduler is the pipeline registry; pipelines registered at onboarding
- One-time pipelines run once then go INACTIVE; definition kept
- Extract and transform are separately scheduled; extract cadence decoupled from input
- TIME_WINDOW missed runs are caught up window-by-window, oldest first

## Open Questions

- [ ] Mid-chain failure — rerun from the failed stage (stages are idempotent), or from the start? (rec: from failed stage)
- [ ] Parallel extraction across sources — unbounded, or a concurrency cap? (rec: cap, configurable)
- [ ] Per-layer SLA — per-pipeline config with defaults, or a global SLA? (rec: per-pipeline)
- [ ] Migration scheduling: table-size threshold for run-now vs defer-to-window

