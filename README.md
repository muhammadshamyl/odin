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
| **Resolve** — waiting approve/reject, quarantine re-inject/ignore | 4/10 | ✅ `odin/resolve.py` |
| **CLI** — `onboard` / `run-extract` / `run-transform` / `ingest` / `runs` / `waiting` / `quarantine` / `web` | 10 | ✅ `odin/cli.py` |
| **Web UI** — home, onboard wizard, table page, run log, waiting + quarantine review | 10 | ✅ `odin/web/` |
| Adversarial test suite (spec §15) | — | ⏳ next |

Feature-by-feature progress for all 11 modules: [`claude/BUILD_STATUS.md`](claude/BUILD_STATUS.md).

## CLI

```bash
uv run odin migrate
uv run odin onboard --name "ERP Sales" --table sales \
    --from-file data/sales_sample.csv \
    --load-type INCREMENTAL --existence-column sale_date
uv run odin ingest erp_sales sales data/sales_sample.csv      # extract + transform
uv run odin runs --source erp_sales
uv run odin waiting list --source erp_sales
uv run odin waiting approve <wbatch_id> --by you@example.com
uv run odin quarantine list --source erp_sales
uv run odin quarantine reinject <qbatch_id>
```

Add `--json` before any subcommand for machine-readable output.

## Web UI

```bash
uv run odin web            # http://127.0.0.1:8000  (--host / --port / --reload)
```

Server-rendered, no auth (local tool). Screens: **home** (registered tables + recent runs),
**Onboard** (upload CSV/TXT → 50-row preview → pick load type + existence column → create,
optionally run now), **table page** (config, upload-and-extract, run-transform, per-table
waiting/quarantine/runs), **Runs** (filterable run log), **Waiting** (pending batches →
held rows vs production side by side → approve/reject), **Quarantine** (open batches → held
rows → re-inject/ignore). Every action calls the same functions as the CLI.

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
- **Collision routing** is DE-configured: `FULL_SNAPSHOT` overwrites its own `load_date`; `INCREMENTAL` diverts rows whose `existence_check_column` value already exists in production → `waiting.<src>_<tbl>`.
- Structurally bad rows → `quarantine.<src>_<tbl>` (re-inject as a new batch); reason + counts in `quarantine_batch_log` / `waiting_batch_log`.
- **No CDC. No automatic retry.** A failure stops and alerts; a DE fixes and re-triggers.
