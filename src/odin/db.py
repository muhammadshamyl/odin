"""Database access.

Two ways in:

- `pool` / `connection()` — the shared pooled connection for pipeline work.
- `log_connection()` — a short-lived standalone connection that commits
  immediately. The run log is written through this so per-chunk progress
  survives the transform's whole-run rollback (Module 9).
"""

from __future__ import annotations

import atexit
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from odin.config import settings

pool = ConnectionPool(
    conninfo=settings.database_url,
    min_size=1,
    max_size=8,
    kwargs={"row_factory": dict_row},
    open=False,
)

_opened = False


def open_pool() -> None:
    global _opened
    if not _opened:
        pool.open()
        atexit.register(_close_pool)
        _opened = True


def _close_pool() -> None:
    try:
        pool.close()
    except Exception:
        pass


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """A pooled connection. Caller manages the transaction (`with conn.transaction()`)."""
    open_pool()
    with pool.connection() as conn:
        yield conn


@contextmanager
def log_connection() -> Iterator[psycopg.Connection]:
    """A standalone connection, autocommit on. Never shares a transaction with pipeline work."""
    with psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row) as conn:
        yield conn
