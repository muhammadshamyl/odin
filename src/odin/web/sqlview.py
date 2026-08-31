"""Read-only view of the SQL Odin generates for the production table.

Rebuilds the exact ``psycopg.sql`` objects the real code path builds — reusing
the shared builders in :mod:`odin.transform` and :mod:`odin.ddl` so the shown
SQL can't drift from what runs — and renders them to strings. Nothing here
executes anything; it is for a DE to read and copy.

Scope is deliberately narrow: the production table's DDL and the insert that
writes rows into it. Staging / quarantine / waiting DDL, the extract COPY and the
structural-filter SQL are intentionally not shown here.
"""

from __future__ import annotations

from psycopg import sql

from odin import casts, ddl, registry, transform
from odin.db import connection
from odin.naming import qname

_EXAMPLE_DAY = "2026-01-15"


def pipeline_sql(source_id: str, table_name: str) -> list[dict]:
    """``[{title, sql}]`` — the production DDL, then the insert into production."""
    cfg = registry.get_table(source_id, table_name)
    cols_meta = registry.get_columns_meta(source_id, table_name)
    source_cols = [c["column_name"] for c in cols_meta]

    prd, stg = cfg.production_target, cfg.staging_target
    prd_i, stg_i = qname(prd), qname(stg)

    key_cols = cfg.natural_key_columns
    prod_cols = transform.prod_columns(source_cols) + (["nk"] if key_cols else [])
    col_list = sql.SQL(", ").join(sql.Identifier(c) for c in prod_cols)

    sel = list(transform.prod_select_list(cols_meta))
    if key_cols:
        tok = {c["column_name"]: c["target_data_type"] for c in cols_meta}
        sel.append(ddl.natural_key_sql(None, key_cols, [casts.pg_type(tok[c]) for c in key_cols]))
    sel_list = sql.SQL(",\n       ").join(sel)

    ddl_block = sql.SQL(
        "{};\n\n-- one daily partition, created on demand per load_date:\n{}"
    ).format(
        ddl.production_ddl(prd, cols_meta, nk=bool(key_cols)),
        ddl.production_partition_ddl(prd, _EXAMPLE_DAY),
    )
    if key_cols:
        ddl_block = sql.SQL("{};\n\n{}").format(ddl_block, ddl.nk_index_ddl(prd))

    insert_block = sql.SQL(
        "-- rows that pass the structural filter, each source column cast to its\n"
        "-- production type (identity for text), then the load metadata columns:\n"
        "INSERT INTO {p} ({cols})\nSELECT {sel}\nFROM {s};"
    ).format(p=prd_i, cols=col_list, sel=sel_list, s=stg_i)

    blocks = [
        ("Production table — DDL  (typed; partitioned by load_date)", ddl_block),
        ("Production table — insert from staging", insert_block),
    ]
    with connection() as conn:
        return [{"title": t, "sql": s.as_string(conn).strip()} for t, s in blocks]
