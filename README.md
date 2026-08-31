# Odin — Slice 1: CSV/TXT ingestion

Local build: Python 3.13 + PostgreSQL. Design docs live in [`claude/`](claude/claude.md).

## Setup

```bash
uv sync
createdb odin && createdb odin_test          # if not already present
uv run python -m odin.migrate                # apply sql/*.sql to $ODIN_DATABASE_URL (default: odin)
```

## What works so far

| Piece | Module | Status |
|---|---|---|
| Core schema (registry, staging_file_control, quarantine/waiting batch logs, run_log) | 2/3/4/9 | ✅ `sql/001_core.sql` |
| CSV/TXT connector — header / sample / batched body | 1 | ✅ `odin/connectors/file.py` |
| Onboarding — header → registry + staging/production/quarantine/waiting tables | 1/3 | ✅ `odin/registry.py` |
| **Extract** — land file → Check 1 → bulk-load to staging | 1/2 | ✅ `odin/extract.py` |
| **Transform** — set-based structural filter → quarantine → load-type routing → truncate | 4/6 | ✅ `odin/transform.py` |
| **INCREMENTAL routing** — single `existence_check_column`, or a **composite natural key** (hashed `nk bigint` + index, one set-based join) | 3/4/6 | ✅ `odin/ddl.py`, `transform._load_incremental_nk` |
| **Physical naming** — one schema per layer, table = slugged DE name, globally unique | 3 | ✅ `odin/naming.py`, `sql/004` |
| **Resolve** — waiting approve (replace) / merge (keep both) / reject, quarantine re-inject/ignore | 4/10 | ✅ `odin/resolve.py` |
| **Production re-typing** — test-cast every value, then rebuild the table in place | 3/6 | ✅ `registry.retype_table` |
| **CLI** — `onboard` / `run-extract` / `run-transform` / `ingest` / `runs` / `waiting` / `quarantine` / `web` | 10 | ✅ `odin/cli.py` |
| **Web UI** — deck, onboard wizard, table page (Load to Production, lineage, delete pipeline), run log, waiting + quarantine review | 10 | ✅ `odin/web/` |
| **SQL Console** — guard-railed ad-hoc SQL (classify → read-only / commit-or-discard / typed-name confirm), `sql_console_log` audit | 10 | ✅ `odin/sqlconsole.py`, `/sql` |
| Adversarial test suite (spec §15) | — | ⏳ next |

Feature-by-feature progress for all 11 modules: [`claude/BUILD_STATUS.md`](claude/BUILD_STATUS.md).

## CLI

```bash
uv run odin migrate
uv run odin onboard --name "ERP Sales" --table sales \
    --from-file data/sales_sample.csv \
    --load-type INCREMENTAL --existence-column sale_date
    # or a composite key:  --natural-key "txn_id,sale_date"
uv run odin ingest erp_sales sales data/sales_sample.csv      # extract + transform
uv run odin runs --source erp_sales
uv run odin waiting list --source erp_sales
uv run odin waiting approve <wbatch_id> --by you@example.com   # replace
uv run odin waiting merge   <wbatch_id> --by you@example.com   # keep both
uv run odin quarantine list --source erp_sales
uv run odin quarantine reinject <qbatch_id>
```

Add `--json` before any subcommand for machine-readable output.

## Web UI

```bash
uv run odin web            # http://127.0.0.1:8000  (--host / --port / --reload)
```

Server-rendered, no auth (local tool). Screens: **Operations Deck** (registered tables +
recent runs, one row per `run_id` with a pill per stage), **Onboard** (upload CSV/TXT →
50-row preview → pick load type + existence column *or* a composite natural key → set
column types → create, optionally run now), **table page** (config, **Load to Production**
one-click extract→transform, animated lineage, self-refreshing KPI cards, re-typing,
per-table waiting/quarantine/runs, delete-pipeline danger zone; standalone extract /
transform under "Manual steps"), **Runs** (filterable run log), **Waiting** / **Quarantine**
review (held rows vs production side by side → approve (replace) / merge (keep both) / reject / re-inject/ignore),
**SQL Console** (`/sql` — guard-railed ad-hoc SQL). Every action calls the same functions
as the CLI.

## Same path from Python

```python
from odin import registry, extract, transform
cfg = registry.onboard_file_source(
    source_name="ERP Sales", file_format="CSV", table_name="sales",
    columns=["txn_id","customer_id","amount","sale_date","status"],
    load_type="INCREMENTAL", existence_check_column="sale_date",   # or load_type="FULL_SNAPSHOT"
)
extract.run_extract("erp_sales", "sales", "data/sales_sample.csv")
transform.run_transform("erp_sales", "sales")
```

## Principles (from the design docs)

- One transform point; staging is an ephemeral retry buffer (truncated after each confirmed load). Every check is set-based SQL over the whole batch — never row-by-row.
- **Physical tables**: one schema per layer — `staging.<t>` / `production.<t>` / `quarantine.<t>` / `waiting.<t>`, where `<t>` is the slugged DE-given table name (no source prefix), globally unique.
- **Collision routing** is DE-configured: `FULL_SNAPSHOT` overwrites its own `load_date`; `INCREMENTAL` diverts rows already in production → `waiting.<t>`, matched by a single `existence_check_column` value **or** a composite `natural_key` (hashed `nk bigint`, indexed, matched with a set-based join + raw-column tie-break).
- Structurally bad rows → `quarantine.<t>` (re-inject as a new batch); reason + counts in `quarantine_batch_log` / `waiting_batch_log`.
- **No CDC. No automatic retry.** A failure stops and alerts; a DE fixes and re-triggers.

## Housekeeping (disk)

Nothing prunes these yet (Module 2 §2.5 retention sweep is unbuilt), so they grow with every load:

- **`web_uploads/`** — a copy of every file dropped on the web UI.
- **`staging_files/<source>/<table>/<date>/`** — a second dated copy landed by the extract step.

Both are just landing-zone copies; the pipeline never re-reads them once a run has consumed the file. Safe to clear:

```bash
rm -rf web_uploads/* staging_files/*
```

In Postgres, a resolved `waiting.<t>` / `quarantine.<t>` can hold reclaimable dead space after a large batch. To reclaim, once the pipeline is idle:

```sql
TRUNCATE waiting.<table>;        -- only if no batches are still pending
TRUNCATE production.<table>;     -- drops all its load_date partitions' rows; keeps the pipeline registered
```

Or drop a pipeline whole from its table page (**Delete pipeline**) / the SQL Console danger zone.
