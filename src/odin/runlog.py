"""Run log writer (Module 9).

Every write goes through its own autocommit connection, so a per-chunk row
survives the transform's whole-run rollback.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from odin.db import log_connection


def new_run_id() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


def start(
    *, dag_id: str, run_id: str, stage: str, source_id: str, table_name: str,
    batch_id: str | None = None, triggered_by: str = "manual",
) -> int:
    with log_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO run_log
                 (dag_id, run_id, batch_id, stage, source_id, table_name,
                  started_at, status, triggered_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 'running', %s)
               RETURNING id""",
            (dag_id, run_id, batch_id, stage, source_id, table_name, _now(), triggered_by),
        )
        return cur.fetchone()["id"]


def finish(
    row_id: int, *, status: str, rows_processed: int | None = None,
    rows_to_production: int | None = None, rows_to_waiting: int | None = None,
    rows_quarantined: int | None = None, error_message: str | None = None,
) -> None:
    with log_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE run_log SET
                 ended_at = %s, status = %s, rows_processed = %s,
                 rows_to_production = %s, rows_to_waiting = %s,
                 rows_quarantined = %s, error_message = %s
               WHERE id = %s""",
            (_now(), status, rows_processed, rows_to_production, rows_to_waiting,
             rows_quarantined, error_message, row_id),
        )
