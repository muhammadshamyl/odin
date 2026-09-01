"""RDBMS source connector (Slice 2) — PostgreSQL only for now.

Phase 1: dial a remote Postgres read-only, list its schemas, and persist the
connection. Credentials live in Odin's own DB under the ``secret`` schema; the
password is symmetric-encrypted with pgcrypto (key: ``settings.rdbms_secret_key``).

Later phases add: FK-graph introspection, table peek, typed sample, and the
batched extract-to-CSV that feeds the existing staging → production tail.
"""

from __future__ import annotations

import csv
import uuid
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import conninfo
from psycopg import sql as _sql

from odin.config import settings
from odin.db import connection

_SSL_MODES = ("disable", "require")


class RdbmsError(RuntimeError):
    """A source-connection problem, message safe to show the DE (driver text,
    trimmed)."""


@dataclass(frozen=True)
class ConnParams:
    host: str
    port: int
    database: str
    username: str
    password: str
    ssl_mode: str = "require"
    default_schema: str | None = None
    engine: str = "postgres"

    def _dsn(self) -> str:
        return conninfo.make_conninfo(
            host=self.host, port=self.port, dbname=self.database,
            user=self.username, password=self.password,
            sslmode=self.ssl_mode,
            connect_timeout=settings.rdbms_connect_timeout,
        )


def _clean_err(exc: Exception) -> str:
    msg = str(exc).strip() or exc.__class__.__name__
    return msg[:400]


def _validate(p: ConnParams) -> None:
    if p.engine != "postgres":
        raise RdbmsError(f"engine {p.engine!r} not supported yet — PostgreSQL only")
    if not p.host.strip():
        raise RdbmsError("host is required")
    if not (1 <= p.port <= 65535):
        raise RdbmsError("port must be 1–65535")
    if not p.database.strip() or not p.username.strip():
        raise RdbmsError("database and username are required")
    if p.ssl_mode not in _SSL_MODES:
        raise RdbmsError(f"ssl_mode must be one of {_SSL_MODES}")


def probe(p: ConnParams) -> dict:
    """Open the source connection, read its version and schema list. Raises
    :class:`RdbmsError` (cleaned driver text) on any failure. Returns
    ``{server_version, schemas: [{schema, tables}]}``."""
    _validate(p)
    try:
        with psycopg.connect(p._dsn(), autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SET statement_timeout = 15000")
            cur.execute("SELECT version()")
            version = cur.fetchone()[0]
            cur.execute(
                r"""
                SELECT n.nspname AS schema,
                       count(*) FILTER (WHERE c.relkind IN ('r', 'p')) AS tables
                FROM pg_namespace n
                LEFT JOIN pg_class c ON c.relnamespace = n.oid
                WHERE n.nspname NOT LIKE 'pg\_%%' AND n.nspname <> 'information_schema'
                GROUP BY n.nspname
                HAVING count(*) FILTER (WHERE c.relkind IN ('r', 'p')) > 0
                ORDER BY n.nspname
                """
            )
            schemas = [{"schema": r[0], "tables": int(r[1])} for r in cur.fetchall()]
    except psycopg.Error as exc:
        raise RdbmsError(_clean_err(exc)) from exc
    except OSError as exc:  # DNS / refused / timeout
        raise RdbmsError(_clean_err(exc)) from exc
    if not schemas:
        raise RdbmsError("connected, but no schema holds a table the reader can see")
    return {"server_version": version, "schemas": schemas}


def save_connection(p: ConnParams, *, server_version: str, label: str | None = None) -> str:
    """Persist a probed connection to ``secret.rdbms_connection`` (password
    encrypted). Returns the new ``connection_id``."""
    cid = uuid.uuid4().hex
    key = settings.rdbms_secret_key
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO secret.rdbms_connection
                 (connection_id, label, engine, host, port, database,
                  default_schema, username, password_enc, ssl_mode,
                  server_version, last_ok_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                       pgp_sym_encrypt(%s, %s), %s, %s, now())""",
            (cid, label, p.engine, p.host.strip(), p.port, p.database.strip(),
             p.default_schema, p.username.strip(), p.password, key, p.ssl_mode,
             server_version),
        )
    return cid


def get_connection(connection_id: str) -> ConnParams:
    """Read a stored connection back, password decrypted. Raises
    :class:`RdbmsError` if unknown."""
    key = settings.rdbms_secret_key
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT host, port, database, username,
                      pgp_sym_decrypt(password_enc, %s) AS password,
                      ssl_mode, default_schema, engine
               FROM secret.rdbms_connection WHERE connection_id = %s""",
            (key, connection_id),
        )
        row = cur.fetchone()
    if row is None:
        raise RdbmsError(f"unknown connection {connection_id}")
    return ConnParams(
        host=row["host"], port=row["port"], database=row["database"],
        username=row["username"], password=row["password"], ssl_mode=row["ssl_mode"],
        default_schema=row["default_schema"], engine=row["engine"],
    )


def connection_meta(connection_id: str) -> dict | None:
    """Non-secret fields for display."""
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT connection_id, label, engine, host, port, database,
                      default_schema, username, ssl_mode, server_version,
                      created_at, last_ok_at
               FROM secret.rdbms_connection WHERE connection_id = %s""",
            (connection_id,),
        )
        return cur.fetchone()


# --------------------------------------------------------------------------- #
# introspection — phase 2 (schema explorer + FK cobweb + peek)
# --------------------------------------------------------------------------- #

def _source_conn(connection_id: str):
    """Open the *source* database from a stored connection id (autocommit,
    short statement_timeout). Caller closes."""
    p = get_connection(connection_id)
    conn = psycopg.connect(p._dsn(), autocommit=True)
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = 8000")
    return conn


def list_tables(connection_id: str, schema: str, *, q: str = "") -> list[dict]:
    """Base + partitioned tables in `schema`: name, planner row estimate, column
    count. Optional substring filter `q` (case-insensitive)."""
    try:
        with _source_conn(connection_id) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.relname AS name,
                       GREATEST(c.reltuples, 0)::bigint AS rows,
                       (SELECT count(*) FROM pg_attribute a
                         WHERE a.attrelid = c.oid AND a.attnum > 0
                           AND NOT a.attisdropped) AS cols
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND c.relkind IN ('r', 'p')
                  AND (%s = '' OR c.relname ILIKE '%%' || %s || '%%')
                ORDER BY c.relname
                """,
                (schema, q, q),
            )
            return [{"name": r[0], "rows": int(r[1]), "cols": int(r[2])}
                    for r in cur.fetchall()]
    except (psycopg.Error, OSError) as exc:
        raise RdbmsError(_clean_err(exc)) from exc


def fk_edges(connection_id: str, schema: str) -> list[dict]:
    """Foreign-key pairs whose *both* ends live in `schema` — the cobweb threads.
    ``[{from, to}]`` (child table -> parent table), de-duplicated."""
    try:
        with _source_conn(connection_id) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT cl.relname AS child, pr.relname AS parent
                FROM pg_constraint k
                JOIN pg_class cl ON cl.oid = k.conrelid
                JOIN pg_class pr ON pr.oid = k.confrelid
                JOIN pg_namespace ncl ON ncl.oid = cl.relnamespace
                JOIN pg_namespace npr ON npr.oid = pr.relnamespace
                WHERE k.contype = 'f'
                  AND ncl.nspname = %s AND npr.nspname = %s
                  AND cl.relname <> pr.relname
                """,
                (schema, schema),
            )
            return [{"from": r[0], "to": r[1]} for r in cur.fetchall()]
    except (psycopg.Error, OSError) as exc:
        raise RdbmsError(_clean_err(exc)) from exc


def columns(connection_id: str, schema: str, table: str) -> list[dict]:
    """``[{name, data_type}]`` in ordinal order (works on an empty table)."""
    try:
        with _source_conn(connection_id) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT column_name, data_type
                   FROM information_schema.columns
                   WHERE table_schema = %s AND table_name = %s
                   ORDER BY ordinal_position""",
                (schema, table),
            )
            return [{"name": r[0], "data_type": r[1]} for r in cur.fetchall()]
    except (psycopg.Error, OSError) as exc:
        raise RdbmsError(_clean_err(exc)) from exc


# --------------------------------------------------------------------------- #
# type mapping + typed sample + static filter (phase 3)
# --------------------------------------------------------------------------- #

# information_schema.columns.data_type  ->  odin cast token
_PG_TOKEN = {
    "smallint": "int", "integer": "int",
    "bigint": "bigint",
    "numeric": "numeric", "decimal": "numeric",
    "real": "numeric", "double precision": "numeric",
    "boolean": "boolean",
    "date": "date",
    "timestamp without time zone": "timestamptz",
    "timestamp with time zone": "timestamptz",
    "time without time zone": "timestamptz",
    "time with time zone": "timestamptz",
}


def token_for(pg_data_type: str) -> str:
    """Odin production cast token for a Postgres ``information_schema`` type.
    Anything not explicitly numeric / temporal / boolean lands as ``text``."""
    return _PG_TOKEN.get((pg_data_type or "").lower(), "text")


def date_columns(connection_id: str, schema: str, table: str) -> list[str]:
    return [c["name"] for c in columns(connection_id, schema, table)
            if token_for(c["data_type"]) in ("date", "timestamptz")]


def _static_where(flt: dict | None):
    """Build ``(sql, params)`` for an optional ``{column, pg_type, from, to}``
    range filter, or ``(None, [])``. Bounds are inclusive; either side may be
    blank. The column is cast to ``pg_type`` so text bounds compare typed."""
    if not flt:
        return None, []
    col = flt.get("column")
    if not col:
        return None, []
    pg = flt.get("pg_type") or "text"
    lo, hi = (flt.get("from") or "").strip(), (flt.get("to") or "").strip()
    if not lo and not hi:
        return None, []
    ref = _sql.SQL("{}::{}").format(_sql.Identifier(col), _sql.SQL(pg))
    parts, params = [], []
    if lo:
        parts.append(_sql.SQL("{} >= %s::{}").format(ref, _sql.SQL(pg)))
        params.append(lo)
    if hi:
        parts.append(_sql.SQL("{} <= %s::{}").format(ref, _sql.SQL(pg)))
        params.append(hi)
    return _sql.SQL(" AND ").join(parts), params


def sample_rows(connection_id: str, schema: str, table: str, *,
                limit: int = 200, static_filter: dict | None = None):
    """``(header, rows)`` where ``header`` is the column-name list and ``rows`` is
    a list of dicts keyed by name — the shape ``preview.html`` expects. The
    static range filter, if any, is applied."""
    cols = [c["name"] for c in columns(connection_id, schema, table)]
    where, params = _static_where(static_filter)
    stmt = _sql.SQL("SELECT * FROM {}.{}").format(
        _sql.Identifier(schema), _sql.Identifier(table))
    if where is not None:
        stmt = stmt + _sql.SQL(" WHERE ") + where
    stmt = stmt + _sql.SQL(" LIMIT {}").format(_sql.Literal(int(limit)))
    try:
        with _source_conn(connection_id) as conn, conn.cursor() as cur:
            cur.execute(stmt, params)
            names = [d.name for d in cur.description]
            rows = [
                {n: ("" if v is None else str(v)) for n, v in zip(names, row)}
                for row in cur.fetchall()
            ]
    except (psycopg.Error, OSError) as exc:
        raise RdbmsError(_clean_err(exc)) from exc
    return (cols, rows)


def column_tokens(connection_id: str, schema: str, table: str) -> dict[str, str]:
    """``{column: cast_token}`` pre-filled from the source schema."""
    return {c["name"]: token_for(c["data_type"])
            for c in columns(connection_id, schema, table)}


# --------------------------------------------------------------------------- #
# batched extract-to-CSV — phase 4 (the "smarter CSV producer")
# --------------------------------------------------------------------------- #

def _csv_cell(v):
    if v is None:
        return ""
    if v is True:
        return "true"
    if v is False:
        return "false"
    return v


def extract_to_csv(src: dict, cols: list[str], dest_path, *,
                   tenure_from: str | None = None, tenure_to: str | None = None) -> int:
    """Stream ``src`` (a ``registry_rdbms_source`` row) into a CSV at
    ``dest_path`` — header = ``cols`` in registry order, then rows. The static
    range filter is always applied; the tenure window is applied when a
    ``tenure_column`` is set and a bound is given (a Full run passes neither).
    Returns the row count. Uses a server-side cursor so a huge table streams."""
    p = get_connection(src["connection_id"])
    schema, table = src["source_schema"], src["source_table"]

    where, params = _static_where(src.get("static_filter"))
    parts = [where] if where is not None else []
    tcol = src.get("tenure_column")
    if tcol and (tenure_from or tenure_to):
        ref = _sql.Identifier(tcol)
        if tenure_from:
            parts.append(_sql.SQL("{} >= %s").format(ref)); params = [*params, tenure_from]
        if tenure_to:
            parts.append(_sql.SQL("{} <= %s").format(ref)); params = [*params, tenure_to]

    stmt = _sql.SQL("SELECT {} FROM {}.{}").format(
        _sql.SQL(", ").join(_sql.Identifier(c) for c in cols),
        _sql.Identifier(schema), _sql.Identifier(table))
    if parts:
        stmt = stmt + _sql.SQL(" WHERE ") + _sql.SQL(" AND ").join(parts)

    dest_path = Path(dest_path)
    n = 0
    try:
        conn = psycopg.connect(p._dsn())          # not autocommit — server-side cursor
    except (psycopg.Error, OSError) as exc:
        raise RdbmsError(_clean_err(exc)) from exc
    try:
        with conn.cursor(name=f"odin_x_{uuid.uuid4().hex[:12]}") as cur:
            cur.itersize = settings.rdbms_batch_rows
            cur.execute(stmt, params)
            with dest_path.open("w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(cols)
                for row in cur:
                    w.writerow([_csv_cell(v) for v in row])
                    n += 1
        conn.rollback()
    except (psycopg.Error, OSError) as exc:
        raise RdbmsError(_clean_err(exc)) from exc
    finally:
        conn.close()
    return n


def peek(connection_id: str, schema: str, table: str, *, limit: int = 20) -> dict:
    """``{columns: [{name, data_type}], rows: [[cell, ...]]}`` — a tiny sample.
    Identifiers are quoted safely; no ORDER BY so a big table still returns fast."""
    cols = columns(connection_id, schema, table)
    stmt = _sql.SQL("SELECT * FROM {}.{} LIMIT {}").format(
        _sql.Identifier(schema), _sql.Identifier(table), _sql.Literal(int(limit)))
    try:
        with _source_conn(connection_id) as conn, conn.cursor() as cur:
            cur.execute(stmt)
            names = [d.name for d in cur.description]
            rows = [["" if v is None else str(v) for v in row] for row in cur.fetchall()]
    except (psycopg.Error, OSError) as exc:
        raise RdbmsError(_clean_err(exc)) from exc
    order = {c["name"]: c["data_type"] for c in cols}
    head = [{"name": n, "data_type": order.get(n, "")} for n in names]
    return {"columns": head, "rows": rows}
