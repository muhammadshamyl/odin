"""Stage 2 — the staging -> production transform (Modules 4 + 6).

One run = one blocking advisory lock + one transaction:

  1. structural filter (one set-based SQL) -> bad rows to quarantine.<table>
     + one quarantine_batch_log row per reason; delete them from staging
  2. route the survivors by load_type:
       FULL_SNAPSHOT -> delete production's rows for each load_date in the batch,
                        then insert everything
       INCREMENTAL   -> rows whose existence_check_column value already exists in
                        production (exact) -> waiting.<table> + a
                        waiting_batch_log row per value; the rest -> production
  3. TRUNCATE staging; commit (releases the lock)

Every step is set-based SQL over the whole staging table — never row-by-row.
The run_log row is written on a separate autocommit connection, so a rolled-back
run still records how far it got. No automatic retry: a failure marks the run
FAILED and re-raises.
"""

from __future__ import annotations

import uuid

from psycopg import sql

from odin import casts, ddl, registry, runlog
from odin.config import settings
from odin.db import connection
from odin.ddl import PRODUCTION_META
from odin.locks import lock
from odin.naming import qname

_DAG = "transform_file"


class TransformError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# pure SQL builders — shared with odin.web.sqlview so the shown SQL can't drift
# --------------------------------------------------------------------------- #

def reason_predicates(cols_meta: list[dict], cap: int) -> list[tuple[str, sql.Composed]]:
    """One ``(reason, WHERE-predicate)`` per structural-reject class, in check
    order. The transform removes a matched row from staging before the next
    class runs, so every bad row lands in exactly one quarantine batch.

      over_length(>cap)      — any text field longer than the btree-safe cap
      empty_required:<col>   — a NOT NULL column left blank
      cast:<col>             — a non-empty value that will not become <col>'s type
    """
    out: list[tuple[str, sql.Composed]] = []

    over = [
        sql.SQL("char_length({}) > {}").format(sql.Identifier(c["column_name"]), sql.Literal(cap))
        for c in cols_meta
    ]
    if over:
        out.append((f"over_length(>{cap})", sql.SQL("({})").format(sql.SQL(" OR ").join(over))))

    for c in cols_meta:
        if c["is_nullable"]:
            continue
        col = sql.Identifier(c["column_name"])
        out.append((
            f"empty_required:{c['column_name']}",
            sql.SQL("({0} IS NULL OR btrim({0}) = '')").format(col),
        ))

    for c in cols_meta:
        guard = casts.guard_sql(sql.Identifier(c["column_name"]), c["target_data_type"])
        if guard is None:
            continue
        col = sql.Identifier(c["column_name"])
        out.append((
            f"cast:{c['column_name']}",
            sql.SQL("({0} IS NOT NULL AND btrim({0}) <> '' AND NOT ({1}))").format(col, guard),
        ))
    return out


def prod_columns(source_cols: list[str]) -> list[str]:
    return source_cols + [n for n, _ in PRODUCTION_META]


def prod_select_list(cols_meta: list[dict], *, restated: str = "false") -> list[sql.Composable]:
    """SELECT list off staging for an INSERT into production / waiting: each
    source column cast to its production type (identity for ``text``), then the
    metadata columns, then the ``restated`` flag."""
    return [
        casts.cast_select(sql.Identifier(c["column_name"]), c["target_data_type"])
        for c in cols_meta
    ] + [
        sql.Identifier("batch_id"),
        sql.Identifier("load_date"),
        sql.Identifier("load_timestamp"),
        sql.Identifier("source_system"),
        sql.SQL(restated),
    ]


def existence_pg_type(cfg, cols_meta: list[dict]) -> str:
    """The production type of the INCREMENTAL existence-check column. The
    collision test casts the staging text to *this* type before comparing, so
    ``2026-8-14`` and ``2026-08-14`` match. Safe: every surviving staging row
    already passed the ``cast:<col>`` filter."""
    tok = next(
        (c["target_data_type"] for c in cols_meta
         if c["column_name"] == cfg.existence_check_column),
        "text",
    )
    return casts.pg_type(tok)


def _distinct_load_dates(cur, staging: str) -> list:
    cur.execute(
        sql.SQL("SELECT DISTINCT load_date FROM {} WHERE load_date IS NOT NULL").format(
            qname(staging)
        )
    )
    return [r["load_date"] for r in cur.fetchall()]


def staging_count(source_id: str, table_name: str) -> int:
    """Rows currently sitting in this table's staging buffer. A healthy run ends
    with staging empty (bad rows to quarantine, good rows to production); a
    non-zero count means a prior run died mid-transform."""
    cfg = registry.get_table(source_id, table_name)
    with connection() as conn, conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(qname(cfg.staging_target)))
        return cur.fetchone()["n"]


def run_transform(
    source_id: str, table_name: str, *, run_id: str | None = None, triggered_by: str = "manual"
) -> dict:
    run_id = run_id or runlog.new_run_id()
    cfg = registry.get_table(source_id, table_name)
    cols_meta = registry.get_columns_meta(source_id, table_name)
    source_cols = [c["column_name"] for c in cols_meta]

    stg = cfg.staging_target
    prd = cfg.production_target
    qtbl = cfg.quarantine_target
    wtbl = cfg.waiting_target
    prod_cols = prod_columns(source_cols)
    select_list = prod_select_list(cols_meta)

    log_id = runlog.start(
        dag_id=_DAG, run_id=run_id, stage="PRODUCTION",
        source_id=source_id, table_name=table_name, triggered_by=triggered_by,
    )
    quarantined = to_production = to_waiting = 0
    try:
        with connection() as conn, conn.transaction(), conn.cursor() as cur:
            lock(cur, stg)  # blocking, whole run

            cur.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(qname(stg)))
            staged = cur.fetchone()["n"]

            if not staged:
                runlog.finish(log_id, status="success", rows_processed=0,
                              rows_to_production=0, rows_to_waiting=0, rows_quarantined=0)
                return {
                    "run_id": run_id, "status": "noop", "rows_processed": 0,
                    "rows_to_production": 0, "rows_to_waiting": 0, "rows_quarantined": 0,
                    "detail": "staging is empty — nothing to transform; run extract first",
                }

            if staged:
                quarantined = _quarantine(cur, cfg, cols_meta, qtbl, run_id, source_id, table_name)

                if cfg.load_type == "FULL_SNAPSHOT":
                    to_production = _load_full_snapshot(cur, stg, prd, prod_cols, select_list)
                else:
                    to_production, to_waiting = _load_incremental(
                        cur, cfg, cols_meta, stg, prd, wtbl, prod_cols, select_list, run_id,
                        source_id, table_name, existence_pg_type(cfg, cols_meta),
                    )

            cur.execute(sql.SQL("TRUNCATE {}").format(qname(stg)))

        runlog.finish(
            log_id, status="success", rows_processed=staged,
            rows_to_production=to_production, rows_to_waiting=to_waiting,
            rows_quarantined=quarantined,
        )
        return {
            "run_id": run_id, "status": "done", "rows_processed": staged,
            "rows_to_production": to_production, "rows_to_waiting": to_waiting,
            "rows_quarantined": quarantined,
        }
    except Exception as exc:
        runlog.finish(log_id, status="failed", error_message=str(exc))
        raise


def _quarantine(cur, cfg, cols_meta, qtbl, run_id, source_id, table_name) -> int:
    """One quarantine batch per reject class that actually caught rows. A row
    matched by an earlier class is deleted from staging first, so it can only
    land in one batch."""
    stg_i = qname(cfg.staging_target)
    qtbl_i = qname(qtbl)
    total = 0

    for reason, pred in reason_predicates(cols_meta, settings.text_cap):
        where = sql.SQL(" WHERE ") + pred
        qbatch = uuid.uuid4().hex
        cur.execute(
            sql.SQL("INSERT INTO {} SELECT s.*, {} FROM {} s").format(
                qtbl_i, sql.Literal(qbatch), stg_i,
            )
            + where
        )
        moved = cur.rowcount
        if not moved:
            continue
        cur.execute(
            """INSERT INTO quarantine_batch_log
                 (qbatch_id, run_id, source_id, table_name, reason, row_count)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (qbatch, run_id, source_id, table_name, reason, moved),
        )
        cur.execute(sql.SQL("DELETE FROM {} s").format(stg_i) + where)
        total += moved
    return total


def _load_full_snapshot(cur, stg, prd, prod_cols, select_list) -> int:
    for d in _distinct_load_dates(cur, stg):
        cur.execute(ddl.production_partition_ddl(prd, d.isoformat()))
        cur.execute(
            sql.SQL("DELETE FROM {} WHERE load_date = {}").format(
                qname(prd), sql.Literal(d)
            )
        )
    cur.execute(
        sql.SQL("INSERT INTO {} ({}) SELECT {} FROM {}").format(
            qname(prd),
            sql.SQL(", ").join(sql.Identifier(c) for c in prod_cols),
            sql.SQL(", ").join(select_list),
            qname(stg),
        )
    )
    return cur.rowcount


def _load_incremental(
    cur, cfg, cols_meta, stg, prd, wtbl, prod_cols, select_list, run_id, source_id, table_name,
    e_type="text",
) -> tuple[int, int]:
    stg_i, prd_i = qname(stg), qname(prd)

    if cfg.natural_key:
        return _load_incremental_nk(
            cur, cfg, cols_meta, stg, prd, wtbl,
            prod_cols, select_list, run_id, source_id, table_name,
        )

    e = sql.Identifier(cfg.existence_check_column)
    et = sql.SQL(e_type)  # a validated `casts` pg type — safe to inline

    # staging value cast to the existence column's production type, then compared
    # typed-to-typed against production — not text-to-text, so equivalent values
    # in different textual form (2026-8-14 vs 2026-08-14) collide correctly.
    key = sql.SQL("s.{e}::{t}").format(e=e, t=et)
    collides = sql.SQL("EXISTS (SELECT 1 FROM {prd} p WHERE p.{e} = {key})").format(
        prd=prd_i, e=e, key=key
    )
    cur.execute(
        sql.SQL(
            "SELECT ({key})::text AS v, count(*) AS n "
            "FROM {stg} s WHERE {collides} GROUP BY ({key})::text"
        ).format(key=key, stg=stg_i, collides=collides)
    )
    colliding = cur.fetchall()

    to_waiting = 0
    for row in colliding:
        wbatch = uuid.uuid4().hex
        cur.execute(
            sql.SQL(
                "INSERT INTO {wtbl} ({cols}, wbatch_id) "
                "SELECT {sel}, %s FROM {stg} s WHERE ({key})::text = %s"
            ).format(
                wtbl=qname(wtbl),
                cols=sql.SQL(", ").join(sql.Identifier(c) for c in prod_cols),
                sel=sql.SQL(", ").join(select_list),
                stg=stg_i, key=key,
            ),
            (wbatch, row["v"]),
        )
        cur.execute(
            """INSERT INTO waiting_batch_log
                 (wbatch_id, run_id, source_id, table_name, existence_value, row_count)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (wbatch, run_id, source_id, table_name, row["v"], row["n"]),
        )
        to_waiting += row["n"]

    if colliding:
        cur.execute(
            sql.SQL("DELETE FROM {stg} s WHERE {collides}").format(stg=stg_i, collides=collides)
        )

    for d in _distinct_load_dates(cur, stg):
        cur.execute(ddl.production_partition_ddl(prd, d.isoformat()))
    cur.execute(
        sql.SQL("INSERT INTO {} ({}) SELECT {} FROM {}").format(
            prd_i,
            sql.SQL(", ").join(sql.Identifier(c) for c in prod_cols),
            sql.SQL(", ").join(select_list),
            stg_i,
        )
    )
    return cur.rowcount, to_waiting


def _load_incremental_nk(
    cur, cfg, cols_meta, stg, prd, wtbl, prod_cols, select_list,
    run_id, source_id, table_name,
) -> tuple[int, int]:
    """INCREMENTAL routing by a composite natural key. One indexed set-based
    join, not a per-value loop:

      collides = EXISTS (production p WHERE p.nk = <staging key hash>
                                       AND <raw-column tie-break>)
      1. colliding staging rows  -> waiting  (+ one waiting_batch_log row for the run)
      2. delete them from staging
      3. survivors -> production

    Both waiting and production carry the ``nk`` bigint (indexed); the same hash
    expression is used everywhere so the sides agree.
    """
    stg_i, prd_i, wtbl_i = qname(stg), qname(prd), qname(wtbl)
    key_cols = cfg.natural_key_columns
    tok = {c["column_name"]: c["target_data_type"] for c in cols_meta}
    pg_types = [casts.pg_type(tok[c]) for c in key_cols]

    nk_s = ddl.natural_key_sql("s", key_cols, pg_types)
    tie = ddl.natural_key_match("s", "p", key_cols, pg_types)
    collides = sql.SQL(
        "EXISTS (SELECT 1 FROM {prd} p WHERE p.nk = {nk} AND {tie})"
    ).format(prd=prd_i, nk=nk_s, tie=tie)

    cols_nk = sql.SQL(", ").join(sql.Identifier(c) for c in [*prod_cols, "nk"])
    sel_nk = sql.SQL(", ").join([*select_list, nk_s])

    wbatch = uuid.uuid4().hex
    cur.execute(
        sql.SQL(
            "INSERT INTO {w} ({cols}, wbatch_id) "
            "SELECT {sel}, {wb} FROM {stg} s WHERE {c}"
        ).format(w=wtbl_i, cols=cols_nk, sel=sel_nk, wb=sql.Literal(wbatch),
                 stg=stg_i, c=collides)
    )
    to_waiting = cur.rowcount
    if to_waiting:
        cur.execute(
            """INSERT INTO waiting_batch_log
                 (wbatch_id, run_id, source_id, table_name, existence_value, row_count)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (wbatch, run_id, source_id, table_name,
             "natural key: " + ", ".join(key_cols), to_waiting),
        )
        cur.execute(
            sql.SQL("DELETE FROM {stg} s WHERE {c}").format(stg=stg_i, c=collides)
        )

    for d in _distinct_load_dates(cur, stg):
        cur.execute(ddl.production_partition_ddl(prd, d.isoformat()))
    cur.execute(
        sql.SQL("INSERT INTO {p} ({cols}) SELECT {sel} FROM {stg} s").format(
            p=prd_i, cols=cols_nk, sel=sel_nk, stg=stg_i
        )
    )
    return cur.rowcount, to_waiting
