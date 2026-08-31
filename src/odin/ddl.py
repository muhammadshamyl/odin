"""Generate per-source staging / production / quarantine / waiting table DDL.

Base build (Slice 1):
  - staging.<table>    : every source column as text, plus metadata. No constraints, no indexes.
  - production.<table>  : every source column at its registered target type (text until
    "Configure"), NOT NULL only where the DE registered it, partitioned by load_date,
    plus `restated`. No row-level UNIQUE.
  - quarantine.<table> : LIKE the staging table + `qbatch_id`   (structurally bad rows)
  - waiting.<table>    : LIKE the production table + `wbatch_id` (colliding rows)

Every table name passed in is a fully-qualified ``schema.table`` string (see
:mod:`odin.naming`); :func:`odin.naming.qname` renders it safely.
"""

from __future__ import annotations

from psycopg import sql

from odin import casts
from odin.naming import qname, split_qual

# Metadata columns added at staging (Module 5 §5.3)
STAGING_META = [
    ("staging_record_id", "text"),
    ("load_date", "date"),
    ("load_timestamp", "timestamptz"),
    ("source_file_id", "text"),
    ("batch_id", "text"),
    ("source_system", "text"),
]

# Metadata columns on production (Module 6). Order matters — the transform's
# INSERT ... SELECT maps staging metadata onto these positionally.
PRODUCTION_META = [
    ("batch_id", "text"),
    ("load_date", "date NOT NULL"),
    ("load_timestamp", "timestamptz"),
    ("source_system", "text"),
    ("restated", "boolean NOT NULL DEFAULT false"),
]


def _col(name: str, type_sql: str) -> sql.Composed:
    return sql.SQL("{} {}").format(sql.Identifier(name), sql.SQL(type_sql))


# --------------------------------------------------------------------------- #
# composite natural key — the INCREMENTAL row-identity accelerator
# --------------------------------------------------------------------------- #

# Forbidden as key columns: their ::text form is not scale-canonical, so the same
# value in two textual forms ('1.5' / '1.50') would hash differently.
NK_FORBIDDEN_TOKENS = frozenset({"numeric", "unit_interval"})


def _colref(alias: str | None, col: str) -> sql.Composed:
    if alias:
        return sql.SQL("{}.{}").format(sql.Identifier(alias), sql.Identifier(col))
    return sql.SQL("{}").format(sql.Identifier(col))


def natural_key_sql(alias: str | None, cols: list[str], pg_types: list[str]) -> sql.Composed:
    """``hashtextextended(...)::bigint`` over the ordered key columns — each cast
    to its production type then to text, unit-separated (chr 31), NULLs tokenised
    (chr 1). Identical on staging (post-quarantine, every value casts) and on
    production, so the two agree row-for-row."""
    parts = [
        sql.SQL("coalesce(({c}::{t})::text, chr(1))").format(c=_colref(alias, c), t=sql.SQL(t))
        for c, t in zip(cols, pg_types)
    ]
    return sql.SQL("hashtextextended(concat_ws(chr(31), {}), 0)::bigint").format(
        sql.SQL(", ").join(parts)
    )


def natural_key_match(a: str, b: str, cols: list[str], pg_types: list[str]) -> sql.Composed:
    """Exact tie-break: every key column equal typed-to-typed (NULL-safe). Only
    evaluated on rows that already share an ``nk`` hash, so a 64-bit hash
    collision cannot cause a wrong route."""
    return sql.SQL(" AND ").join(
        sql.SQL("({a}::{t} IS NOT DISTINCT FROM {b}::{t})").format(
            a=_colref(a, c), b=_colref(b, c), t=sql.SQL(t)
        )
        for c, t in zip(cols, pg_types)
    )


def nk_index_ddl(table: str) -> sql.Composed:
    """btree index on the ``nk`` column. Name lives in the table's own schema."""
    _, tbl = split_qual(table)
    return sql.SQL("CREATE INDEX IF NOT EXISTS {idx} ON {tbl} (nk)").format(
        idx=sql.Identifier(f"{tbl}_nk_idx"), tbl=qname(table)
    )


def staging_ddl(table: str, columns: list[str]) -> sql.Composed:
    cols = [_col(c, "text") for c in columns]
    cols += [_col(n, t) for n, t in STAGING_META]
    return sql.SQL("CREATE TABLE IF NOT EXISTS {} (\n  {}\n)").format(
        qname(table), sql.SQL(",\n  ").join(cols)
    )


def production_ddl(table: str, columns: list[dict], *, nk: bool = False) -> sql.Composed:
    """`columns` rows: column_name, target_data_type (a `casts` token), is_nullable.
    `nk=True` adds the ``nk bigint`` composite-key column (index created separately
    via :func:`nk_index_ddl`)."""
    cols: list[sql.Composed] = []
    for c in columns:
        t = casts.pg_type(c["target_data_type"])
        if not c["is_nullable"]:
            t = f"{t} NOT NULL"
        cols.append(_col(c["column_name"], t))
    cols += [_col(n, t) for n, t in PRODUCTION_META]
    if nk:
        cols.append(_col("nk", "bigint"))
    return sql.SQL(
        "CREATE TABLE IF NOT EXISTS {} (\n  {}\n) PARTITION BY RANGE (load_date)"
    ).format(qname(table), sql.SQL(",\n  ").join(cols))


def production_partition_ddl(table: str, day: str) -> sql.Composed:
    """One daily partition, in the same schema as `table`. `day` is 'YYYY-MM-DD'. Re-runnable."""
    schema, tbl = split_qual(table)
    part = f"{tbl}_p{day.replace('-', '')}"
    part_qual = f"{schema}.{part}" if schema else part
    return sql.SQL(
        "CREATE TABLE IF NOT EXISTS {} PARTITION OF {} "
        "FOR VALUES FROM ({}) TO (({})::date + 1)"
    ).format(qname(part_qual), qname(table), sql.Literal(day), sql.Literal(day))


def quarantine_ddl(name: str, staging_target: str) -> sql.Composed:
    """`quarantine.<table>` — every staging column (all text) + metadata, plus qbatch_id."""
    return sql.SQL(
        "CREATE TABLE IF NOT EXISTS {} "
        "(LIKE {} INCLUDING DEFAULTS, qbatch_id text NOT NULL)"
    ).format(qname(name), qname(staging_target))


def waiting_ddl(name: str, production_target: str) -> sql.Composed:
    """`waiting.<table>` — an exact copy of the production columns, plus wbatch_id."""
    return sql.SQL(
        "CREATE TABLE IF NOT EXISTS {} "
        "(LIKE {} INCLUDING DEFAULTS, wbatch_id text NOT NULL)"
    ).format(qname(name), qname(production_target))
