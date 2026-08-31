"""Stage 1 — land a CSV/TXT file and load it into staging.

For a file source there is no query and no batch-file rewrite: the file *is* the
source. This stage:

  1. lands the file in the staging file area, registers it PENDING
  2. Check 1 — header vs registry; mismatch => whole file to `quarantine`, FAILED
  3. loads the body into the staging table in row batches (bulk COPY), stamping
     load_date / load_timestamp / batch_id / ...

No automatic retry: any failure marks the file FAILED and stops.
"""

from __future__ import annotations

import shutil
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from psycopg import sql

from odin import registry, runlog
from odin.config import settings
from odin.connectors import file as fc
from odin.db import connection
from odin.ddl import STAGING_META
from odin.locks import try_lock
from odin.naming import qname

_DAG = "extract_file"


class ExtractError(RuntimeError):
    pass


def land_file(source_id: str, table_name: str, src_path: Path) -> str:
    src_path = Path(src_path)
    if not src_path.is_file():
        raise ExtractError(f"no such file: {src_path}")
    fmt = fc.detect_format(src_path)
    file_id = uuid.uuid4().hex
    settings.ensure_dirs()
    dest_dir = settings.staging_file_area / source_id / table_name / date.today().isoformat()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{file_id}{src_path.suffix.lower()}"
    shutil.copy2(src_path, dest)

    with connection() as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """INSERT INTO staging_file_control
                 (file_id, source_id, table_name, file_path, file_format, file_size_bytes)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (file_id, source_id, table_name, str(dest), fmt, dest.stat().st_size),
        )
    return file_id


def _fail_file(file_id: str, msg: str) -> None:
    with connection() as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE staging_file_control SET processing_status='FAILED', error_message=%s WHERE file_id=%s",
            (msg, file_id),
        )


def check_1(file_id: str) -> str | None:
    """Header names + count vs registry. Returns ``None`` when the header matches,
    otherwise the mismatch reason (and marks the file FAILED). Mismatch => whole
    file rejected, nothing loaded.

    Nothing is loaded on a Check 1 failure, so there is no per-table quarantine
    row — the record is `staging_file_control` FAILED with the reason (Module 4 §4.1).
    """
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT source_id, table_name, file_path FROM staging_file_control WHERE file_id=%s",
            (file_id,),
        )
        rec = cur.fetchone()
    if rec is None:
        raise ExtractError(f"unknown file_id {file_id}")

    expected = registry.get_columns(rec["source_id"], rec["table_name"])
    actual = fc.read_header(Path(rec["file_path"]))

    if actual == expected:
        return None

    if len(actual) != len(expected):
        reason = f"column count mismatch: file has {len(actual)}, registry expects {len(expected)}"
    else:
        diff = [(a, e) for a, e in zip(actual, expected) if a != e]
        reason = f"column name mismatch: {diff}"

    _fail_file(file_id, f"check_1: {reason}")
    return reason


def load_to_staging(file_id: str, *, triggered_by: str = "manual") -> int:
    """Bulk-load the file body into the staging table. Returns rows loaded.

    Progress and failures are recorded by the single run-level ``EXTRACT``
    ``run_log`` row that :func:`run_extract` owns — this function just moves rows
    and marks ``staging_file_control``.
    """
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT source_id, table_name, file_path FROM staging_file_control WHERE file_id=%s",
            (file_id,),
        )
        rec = cur.fetchone()
    cfg = registry.get_table(rec["source_id"], rec["table_name"])
    source_cols = registry.get_columns(rec["source_id"], rec["table_name"])
    meta_cols = [c for c, _ in STAGING_META]
    all_cols = source_cols + meta_cols
    load_dt = datetime.now(timezone.utc)
    load_d = load_dt.date()

    copy_sql = sql.SQL("COPY {} ({}) FROM STDIN").format(
        qname(cfg.staging_target),
        sql.SQL(", ").join(sql.Identifier(c) for c in all_cols),
    )

    total = 0
    with connection() as conn:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "UPDATE staging_file_control SET processing_status='LOADING' WHERE file_id=%s",
                (file_id,),
            )

        try:
            for batch in fc.iter_batches(Path(rec["file_path"]), settings.batch_rows):
                batch_id = uuid.uuid4().hex
                with conn.transaction(), conn.cursor() as cur:
                    if not try_lock(cur, cfg.staging_target):
                        raise ExtractError("transform holds the table lock — try again next tick")
                    with cur.copy(copy_sql) as cp:
                        for row in batch:
                            cp.write_row(
                                [row.get(c) for c in source_cols]
                                + [uuid.uuid4().hex, load_d, load_dt, file_id, batch_id, rec["source_id"]]
                            )
                total += len(batch)
        except Exception as exc:
            _fail_file(file_id, f"load: {exc}")
            raise

        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                """UPDATE staging_file_control
                   SET processing_status='LOADED', processed_timestamp=now(), row_count=%s
                   WHERE file_id=%s""",
                (total, file_id),
            )
    return total


def run_extract(
    source_id: str, table_name: str, file_path: str | Path, *,
    triggered_by: str = "manual", run_id: str | None = None,
) -> dict:
    """Land -> Check 1 -> load. Returns a small summary. Raises on failure (no retry).

    Every call writes exactly one run-level ``EXTRACT`` row to ``run_log`` — set
    ``running`` up front, then ``failed`` (with the reason) or ``success`` — so a
    file that never gets past landing or the header check is still visible in the
    Pipeline Monitor, not a silent nothing.

    `run_id` is generated per invocation unless the caller threads one in (the
    `ingest` job passes a single id so extract + transform share one run).

    Status: ``loaded`` (rows in staging), ``empty`` (file had only a header),
    ``quarantined`` (Check 1 rejected the whole file).
    """
    run_id = run_id or runlog.new_run_id()
    log_id = runlog.start(
        dag_id=_DAG, run_id=run_id, stage="EXTRACT",
        source_id=source_id, table_name=table_name, triggered_by=triggered_by,
    )
    try:
        registry.get_table(source_id, table_name)  # fail fast + clearly if unknown
        file_id = land_file(source_id, table_name, Path(file_path))
        reason = check_1(file_id)
        if reason is not None:
            runlog.finish(log_id, status="failed", rows_processed=0,
                          error_message=f"check_1: {reason}")
            return {"run_id": run_id, "file_id": file_id, "status": "quarantined",
                    "rows": 0, "reason": reason}
        rows = load_to_staging(file_id, triggered_by=triggered_by)
    except Exception as exc:
        runlog.finish(log_id, status="failed", error_message=str(exc))
        raise
    runlog.finish(log_id, status="success", rows_processed=rows)
    status = "loaded" if rows else "empty"
    return {"run_id": run_id, "file_id": file_id, "status": status, "rows": rows}
