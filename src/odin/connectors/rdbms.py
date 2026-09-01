"""RDBMS source connector (Slice 2) — PostgreSQL only for now.

Phase 1: dial a remote Postgres read-only, list its schemas, and persist the
connection. Credentials live in Odin's own DB under the ``secret`` schema; the
password is symmetric-encrypted with pgcrypto (key: ``settings.rdbms_secret_key``).

Later phases add: FK-graph introspection, table peek, typed sample, and the
batched extract-to-CSV that feeds the existing staging → production tail.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import psycopg
from psycopg import conninfo

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
