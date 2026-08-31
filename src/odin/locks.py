"""Per-table extract<->transform lock (Module 5 §5.6 / Module 6 §6.1).

Postgres advisory locks keyed on a constant per table:
  - the loader takes it as a *transaction* try-lock around the staging write;
    not acquired => the transform is mid-run, so the loader stops and retries
    on the next tick.
  - the transform takes it as a *transaction* blocking lock for the whole run.
"""

from __future__ import annotations

import psycopg


def try_lock(cur: psycopg.Cursor, staging_target: str) -> bool:
    cur.execute(
        "SELECT pg_try_advisory_xact_lock(hashtext(%s)) AS got", (staging_target,)
    )
    return bool(cur.fetchone()["got"])


def lock(cur: psycopg.Cursor, staging_target: str) -> None:
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (staging_target,))
