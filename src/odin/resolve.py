"""Manual resolution of the two diversions (Module 4 §4.6-4.7, Module 10 §10.5).

Nothing here is automatic — a DE calls these after reviewing a batch. Recovery
is always manual: "if it failed once it will fail again" unless the cause is fixed.

Waiting batches  (waiting_batch_log + waiting.<table>):
  approve_waiting -> replace production's rows for the existence value with the
                    waiting rows, restated = TRUE; batch -> approved
  reject_waiting  -> drop the waiting rows; batch -> rejected

Quarantine batches  (quarantine_batch_log + quarantine.<table>):
  reinject_quarantine -> copy the rows back into staging under a NEW batch_id
                         (never patched in place), then a transform re-processes
                         them; batch -> reinjected
  ignore_quarantine   -> drop the rows; batch -> ignored
"""

from __future__ import annotations

import uuid

from psycopg import sql

from odin import casts, ddl, registry, runlog
from odin.db import connection
from odin.ddl import PRODUCTION_META, STAGING_META
from odin.locks import lock
from odin.naming import qname

_DAG = "resolve"


class ResolveError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# read helpers (for a CLI / UI)
# --------------------------------------------------------------------------- #

def pending_waiting(source_id: str | None = None, table_name: str | None = None,
                    *, limit: int | None = None, offset: int = 0) -> list[dict]:
    """Open waiting batches, oldest first. `limit` paginates."""
    return _list_log("waiting_batch_log", "status = 'pending'", source_id, table_name,
                     limit=limit, offset=offset)


def open_quarantine(source_id: str | None = None, table_name: str | None = None,
                    *, limit: int | None = None, offset: int = 0) -> list[dict]:
    """Open quarantine batches, oldest first. `limit` paginates."""
    return _list_log("quarantine_batch_log", "resolution_status = 'open'", source_id, table_name,
                     limit=limit, offset=offset)


def count_pending_waiting(source_id: str | None = None, table_name: str | None = None) -> int:
    return _count_log("waiting_batch_log", "status = 'pending'", source_id, table_name)


def count_open_quarantine(source_id: str | None = None, table_name: str | None = None) -> int:
    return _count_log("quarantine_batch_log", "resolution_status = 'open'", source_id, table_name)


def waiting_stats(source_id: str | None = None, table_name: str | None = None) -> dict:
    """Filter-wide totals for the Waiting KPI tiles (independent of the page)."""
    where, args = _log_where("status = 'pending'", source_id, table_name)
    q = sql.SQL(
        "SELECT count(*) AS batches, coalesce(sum(row_count), 0) AS rows, "
        "min(created_at) AS oldest, count(DISTINCT source_id) AS sources "
        "FROM waiting_batch_log WHERE "
    ) + where
    with connection() as conn, conn.cursor() as cur:
        cur.execute(q, args)
        return cur.fetchone()


def quarantine_stats(source_id: str | None = None, table_name: str | None = None) -> dict:
    """Filter-wide totals for the Quarantine KPI tiles (independent of the page)."""
    where, args = _log_where("resolution_status = 'open'", source_id, table_name)
    q = sql.SQL(
        "SELECT count(*) AS batches, coalesce(sum(row_count), 0) AS rows, "
        "min(created_at) AS oldest, count(DISTINCT reason) AS reasons "
        "FROM quarantine_batch_log WHERE "
    ) + where
    with connection() as conn, conn.cursor() as cur:
        cur.execute(q, args)
        return cur.fetchone()


def _log_where(cond: str, source_id: str | None, table_name: str | None):
    clauses, args = [cond], []
    if source_id:
        clauses.append("source_id = %s")
        args.append(source_id)
    if table_name:
        clauses.append("table_name = %s")
        args.append(table_name)
    return sql.SQL(" AND ".join(clauses)), args


def _list_log(table: str, cond: str, source_id: str | None, table_name: str | None,
              *, limit: int | None = None, offset: int = 0) -> list[dict]:
    where, args = _log_where(cond, source_id, table_name)
    query = (
        sql.SQL("SELECT * FROM {} WHERE ").format(sql.Identifier(table))
        + where + sql.SQL(" ORDER BY created_at")
    )
    if limit is not None:
        query += sql.SQL(" LIMIT %s OFFSET %s")
        args = [*args, limit, offset]
    with connection() as conn, conn.cursor() as cur:
        cur.execute(query, args)
        return cur.fetchall()


def _count_log(table: str, cond: str, source_id: str | None, table_name: str | None) -> int:
    where, args = _log_where(cond, source_id, table_name)
    query = sql.SQL("SELECT count(*) AS n FROM {} WHERE ").format(sql.Identifier(table)) + where
    with connection() as conn, conn.cursor() as cur:
        cur.execute(query, args)
        return cur.fetchone()["n"]


def waiting_batch(wbatch_id: str) -> dict | None:
    with connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM waiting_batch_log WHERE wbatch_id = %s", (wbatch_id,))
        return cur.fetchone()


def quarantine_batch(qbatch_id: str) -> dict | None:
    with connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM quarantine_batch_log WHERE qbatch_id = %s", (qbatch_id,))
        return cur.fetchone()


def waiting_rows(wbatch_id: str, *, limit: int = 500) -> list[dict]:
    """The held rows for one waiting batch (from waiting.<table>)."""
    wb = waiting_batch(wbatch_id)
    if wb is None:
        return []
    cfg = registry.get_table(wb["source_id"], wb["table_name"])
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT * FROM {} WHERE wbatch_id = %s LIMIT %s").format(
                qname(cfg.waiting_target)
            ),
            (wbatch_id, limit),
        )
        return cur.fetchall()


def _nk_types(source_id: str, table_name: str, key_cols: list[str]) -> list[str]:
    tok = {c["column_name"]: c["target_data_type"]
           for c in registry.get_columns_meta(source_id, table_name)}
    return [casts.pg_type(tok[c]) for c in key_cols]


def production_rows_for(wbatch_id: str, *, limit: int = 500) -> list[dict]:
    """Production's current rows that this waiting batch would replace — matched
    by the composite ``nk`` (natural-key pipelines) or by the single
    existence-check value (legacy). For side-by-side review."""
    wb = waiting_batch(wbatch_id)
    if wb is None:
        return []
    cfg = registry.get_table(wb["source_id"], wb["table_name"])
    prd, wt = qname(cfg.production_target), qname(cfg.waiting_target)
    key_cols = cfg.natural_key_columns
    with connection() as conn, conn.cursor() as cur:
        if key_cols:
            tie = ddl.natural_key_match(
                "p", "w", key_cols, _nk_types(wb["source_id"], wb["table_name"], key_cols)
            )
            cur.execute(
                sql.SQL(
                    "SELECT p.* FROM {p} p WHERE EXISTS (SELECT 1 FROM {w} w "
                    "WHERE w.wbatch_id = %s AND w.nk = p.nk AND {tie}) LIMIT %s"
                ).format(p=prd, w=wt, tie=tie),
                (wbatch_id, limit),
            )
        else:
            cur.execute(
                sql.SQL("SELECT * FROM {} WHERE {}::text = %s LIMIT %s").format(
                    prd, sql.Identifier(cfg.existence_check_column)
                ),
                (wb["existence_value"], limit),
            )
        return cur.fetchall()


def waiting_compare(wbatch_id: str) -> dict:
    """Computed held-vs-current-production comparison for one waiting batch — no
    column guessing. Always the row count; plus ``sum`` / ``min`` / ``max`` for
    every **numeric-typed** source column (the registry decides which by type).
    ``{metrics: [{label, production, incoming, changed}], numeric_cols: [...]}``.
    """
    wb = waiting_batch(wbatch_id)
    if wb is None:
        return {"metrics": [], "numeric_cols": []}
    cfg = registry.get_table(wb["source_id"], wb["table_name"])
    meta = registry.get_columns_meta(wb["source_id"], wb["table_name"])
    num_cols = [c["column_name"] for c in meta if casts.is_numeric(c["target_data_type"])]

    aggs = [sql.SQL("count(*) AS r_rows")]
    for c in num_cols:
        ci = sql.Identifier(c)
        aggs += [
            sql.SQL("sum({0}) AS {1}").format(ci, sql.Identifier(f"sum_{c}")),
            sql.SQL("min({0}) AS {1}").format(ci, sql.Identifier(f"min_{c}")),
            sql.SQL("max({0}) AS {1}").format(ci, sql.Identifier(f"max_{c}")),
        ]
    agg = sql.SQL(", ").join(aggs)
    prd = qname(cfg.production_target)
    wt = qname(cfg.waiting_target)
    key_cols = cfg.natural_key_columns

    with connection() as conn, conn.cursor() as cur:
        if key_cols:
            tie = ddl.natural_key_match(
                "p", "w", key_cols, _nk_types(wb["source_id"], wb["table_name"], key_cols)
            )
            cur.execute(
                sql.SQL(
                    "SELECT {a} FROM {p} p WHERE EXISTS (SELECT 1 FROM {w} w "
                    "WHERE w.wbatch_id = %s AND w.nk = p.nk AND {tie})"
                ).format(a=agg, p=prd, w=wt, tie=tie),
                (wbatch_id,),
            )
        else:
            cur.execute(
                sql.SQL("SELECT {a} FROM {p} p WHERE {e}::text = %s").format(
                    a=agg, p=prd, e=sql.Identifier(cfg.existence_check_column)
                ),
                (wb["existence_value"],),
            )
        prod = cur.fetchone()
        cur.execute(
            sql.SQL("SELECT {a} FROM {w} WHERE wbatch_id = %s").format(a=agg, w=wt),
            (wbatch_id,),
        )
        inc = cur.fetchone()

    def _row(label: str, key: str) -> dict:
        p, i = prod.get(key), inc.get(key)
        return {"label": label, "production": p, "incoming": i, "changed": p != i}

    metrics = [_row("rows", "r_rows")]
    for c in num_cols:
        metrics += [_row(f"sum({c})", f"sum_{c}"),
                    _row(f"min({c})", f"min_{c}"),
                    _row(f"max({c})", f"max_{c}")]
    return {"metrics": metrics, "numeric_cols": num_cols}


def quarantine_rows(qbatch_id: str, *, limit: int = 500) -> list[dict]:
    """The held rows for one quarantine batch (from quarantine.<table>)."""
    qb = quarantine_batch(qbatch_id)
    if qb is None:
        return []
    cfg = registry.get_table(qb["source_id"], qb["table_name"])
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT * FROM {} WHERE qbatch_id = %s LIMIT %s").format(
                qname(cfg.quarantine_target)
            ),
            (qbatch_id, limit),
        )
        return cur.fetchall()


# --------------------------------------------------------------------------- #
# waiting
# --------------------------------------------------------------------------- #

def approve_waiting(wbatch_id: str, *, resolved_by: str | None = None) -> dict:
    """Replace production's rows for this batch's existence value with the
    waiting rows (restated = TRUE). Serialised against the transform by the
    per-table lock."""
    with connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM waiting_batch_log WHERE wbatch_id = %s", (wbatch_id,))
        wb = cur.fetchone()
    if wb is None:
        raise ResolveError(f"unknown wbatch_id {wbatch_id}")
    if wb["status"] != "pending":
        raise ResolveError(f"wbatch {wbatch_id} is {wb['status']}, not pending")

    src, tbl, value = wb["source_id"], wb["table_name"], wb["existence_value"]
    cfg = registry.get_table(src, tbl)
    source_cols = registry.get_columns(src, tbl)
    key_cols = cfg.natural_key_columns
    prod_cols = source_cols + [n for n, _ in PRODUCTION_META] + (["nk"] if key_cols else [])
    # select off waiting.<w>: every column as-is, but force restated = true
    select_list = [sql.Identifier(c) for c in source_cols] + [
        sql.Identifier("batch_id"),
        sql.Identifier("load_date"),
        sql.Identifier("load_timestamp"),
        sql.Identifier("source_system"),
        sql.SQL("true"),
    ] + ([sql.Identifier("nk")] if key_cols else [])
    prd = qname(cfg.production_target)
    wt = qname(cfg.waiting_target)
    if key_cols:
        meta = registry.get_columns_meta(src, tbl)
        tok = {c["column_name"]: c["target_data_type"] for c in meta}
        pg_types = [casts.pg_type(tok[c]) for c in key_cols]
        nk_tie = ddl.natural_key_match("p", "w", key_cols, pg_types)
    else:
        e = sql.Identifier(cfg.existence_check_column)

    run_id = runlog.new_run_id()
    log_id = runlog.start(
        dag_id=_DAG, run_id=run_id, stage="PRODUCTION",
        source_id=src, table_name=tbl, batch_id=wbatch_id, triggered_by="manual",
    )
    try:
        with connection() as conn, conn.transaction(), conn.cursor() as cur:
            lock(cur, cfg.staging_target)

            cur.execute(
                sql.SQL(
                    "SELECT DISTINCT load_date FROM {} "
                    "WHERE wbatch_id = %s AND load_date IS NOT NULL"
                ).format(wt),
                (wbatch_id,),
            )
            for r in cur.fetchall():
                cur.execute(
                    ddl.production_partition_ddl(cfg.production_target, r["load_date"].isoformat())
                )

            if key_cols:
                cur.execute(
                    sql.SQL(
                        "DELETE FROM {prd} p USING {wt} w "
                        "WHERE w.wbatch_id = %s AND p.nk = w.nk AND {tie}"
                    ).format(prd=prd, wt=wt, tie=nk_tie),
                    (wbatch_id,),
                )
            else:
                cur.execute(
                    sql.SQL("DELETE FROM {} WHERE {}::text = %s").format(prd, e), (value,)
                )
            replaced = cur.rowcount
            cur.execute(
                sql.SQL(
                    "INSERT INTO {} ({}) SELECT {} FROM {} WHERE wbatch_id = %s"
                ).format(
                    prd,
                    sql.SQL(", ").join(sql.Identifier(c) for c in prod_cols),
                    sql.SQL(", ").join(select_list),
                    wt,
                ),
                (wbatch_id,),
            )
            inserted = cur.rowcount
            cur.execute(
                sql.SQL("DELETE FROM {} WHERE wbatch_id = %s").format(wt),
                (wbatch_id,),
            )
            cur.execute(
                """UPDATE waiting_batch_log
                   SET status = 'approved', resolved_by = %s, resolved_at = now()
                   WHERE wbatch_id = %s""",
                (resolved_by, wbatch_id),
            )
        runlog.finish(
            log_id, status="success", rows_processed=inserted, rows_to_production=inserted
        )
        return {
            "wbatch_id": wbatch_id, "status": "approved",
            "production_rows_replaced": replaced, "production_rows_inserted": inserted,
        }
    except Exception as exc:
        runlog.finish(log_id, status="failed", error_message=str(exc))
        raise


def reject_waiting(wbatch_id: str, *, resolved_by: str | None = None) -> dict:
    """Drop the waiting rows; production keeps what it had."""
    with connection() as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT * FROM waiting_batch_log WHERE wbatch_id = %s", (wbatch_id,))
        wb = cur.fetchone()
        if wb is None:
            raise ResolveError(f"unknown wbatch_id {wbatch_id}")
        if wb["status"] != "pending":
            raise ResolveError(f"wbatch {wbatch_id} is {wb['status']}, not pending")
        cfg = registry.get_table(wb["source_id"], wb["table_name"])
        cur.execute(
            sql.SQL("DELETE FROM {} WHERE wbatch_id = %s").format(qname(cfg.waiting_target)),
            (wbatch_id,),
        )
        dropped = cur.rowcount
        cur.execute(
            """UPDATE waiting_batch_log
               SET status = 'rejected', resolved_by = %s, resolved_at = now()
               WHERE wbatch_id = %s""",
            (resolved_by, wbatch_id),
        )
    return {"wbatch_id": wbatch_id, "status": "rejected", "rows_dropped": dropped}


def reject_all_waiting(
    source_id: str | None = None, table_name: str | None = None,
    *, resolved_by: str | None = None,
) -> dict:
    """Reject every pending waiting batch matching the filter — drop all held
    rows, mark every batch rejected. Production is untouched (no lock needed;
    this never writes production or staging). One transaction."""
    batches = pending_waiting(source_id, table_name)
    if not batches:
        return {"rejected": 0, "rows_dropped": 0}

    by_table: dict[tuple[str, str], list[str]] = {}
    for b in batches:
        by_table.setdefault((b["source_id"], b["table_name"]), []).append(b["wbatch_id"])

    dropped = 0
    with connection() as conn, conn.transaction(), conn.cursor() as cur:
        for (src, tbl), ids in by_table.items():
            cfg = registry.get_table(src, tbl)
            cur.execute(
                sql.SQL("DELETE FROM {} WHERE wbatch_id = ANY(%s)").format(
                    qname(cfg.waiting_target)
                ),
                (ids,),
            )
            dropped += cur.rowcount
        all_ids = [i for ids in by_table.values() for i in ids]
        cur.execute(
            "UPDATE waiting_batch_log SET status='rejected', resolved_by=%s, resolved_at=now() "
            "WHERE wbatch_id = ANY(%s) AND status='pending'",
            (resolved_by, all_ids),
        )
    return {"rejected": len(all_ids), "rows_dropped": dropped}


# --------------------------------------------------------------------------- #
# quarantine
# --------------------------------------------------------------------------- #

def reinject_quarantine(qbatch_id: str, *, resolved_by: str | None = None) -> dict:
    """Copy the batch's rows back into the staging table under a fresh batch_id.
    A subsequent transform re-processes them; still-bad rows re-quarantine under a
    new qbatch (never patched in place)."""
    with connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM quarantine_batch_log WHERE qbatch_id = %s", (qbatch_id,))
        qb = cur.fetchone()
    if qb is None:
        raise ResolveError(f"unknown qbatch_id {qbatch_id}")
    if qb["resolution_status"] != "open":
        raise ResolveError(f"qbatch {qbatch_id} is {qb['resolution_status']}, not open")

    src, tbl = qb["source_id"], qb["table_name"]
    cfg = registry.get_table(src, tbl)
    source_cols = registry.get_columns(src, tbl)
    meta = [n for n, _ in STAGING_META]
    staging_cols = source_cols + meta
    new_batch = uuid.uuid4().hex
    # select off quarantine.<q>: all staging columns, but override batch_id
    select_list = [sql.Identifier(c) for c in source_cols] + [
        sql.SQL("%s") if n == "batch_id" else sql.Identifier(n) for n in meta
    ]
    stg = qname(cfg.staging_target)
    qt = qname(cfg.quarantine_target)

    run_id = runlog.new_run_id()
    log_id = runlog.start(
        dag_id=_DAG, run_id=run_id, stage="STAGING",
        source_id=src, table_name=tbl, batch_id=new_batch, triggered_by="manual",
    )
    try:
        with connection() as conn, conn.transaction(), conn.cursor() as cur:
            lock(cur, cfg.staging_target)
            cur.execute(
                sql.SQL(
                    "INSERT INTO {} ({}) SELECT {} FROM {} WHERE qbatch_id = %s"
                ).format(
                    stg,
                    sql.SQL(", ").join(sql.Identifier(c) for c in staging_cols),
                    sql.SQL(", ").join(select_list),
                    qt,
                ),
                (new_batch, qbatch_id),
            )
            moved = cur.rowcount
            cur.execute(
                sql.SQL("DELETE FROM {} WHERE qbatch_id = %s").format(qt),
                (qbatch_id,),
            )
            cur.execute(
                """UPDATE quarantine_batch_log
                   SET resolution_status = 'reinjected', resolved_by = %s, resolved_at = now()
                   WHERE qbatch_id = %s""",
                (resolved_by, qbatch_id),
            )
        runlog.finish(log_id, status="success", rows_processed=moved)
        return {
            "qbatch_id": qbatch_id, "status": "reinjected",
            "new_batch_id": new_batch, "rows_reinjected": moved,
            "note": "run the transform to re-process; still-bad rows will re-quarantine",
        }
    except Exception as exc:
        runlog.finish(log_id, status="failed", error_message=str(exc))
        raise


def ignore_quarantine(qbatch_id: str, *, resolved_by: str | None = None) -> dict:
    """Drop the batch's rows; mark it ignored (a known / accepted exception)."""
    with connection() as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT * FROM quarantine_batch_log WHERE qbatch_id = %s", (qbatch_id,))
        qb = cur.fetchone()
        if qb is None:
            raise ResolveError(f"unknown qbatch_id {qbatch_id}")
        if qb["resolution_status"] != "open":
            raise ResolveError(f"qbatch {qbatch_id} is {qb['resolution_status']}, not open")
        cfg = registry.get_table(qb["source_id"], qb["table_name"])
        cur.execute(
            sql.SQL("DELETE FROM {} WHERE qbatch_id = %s").format(qname(cfg.quarantine_target)),
            (qbatch_id,),
        )
        dropped = cur.rowcount
        cur.execute(
            """UPDATE quarantine_batch_log
               SET resolution_status = 'ignored', resolved_by = %s, resolved_at = now()
               WHERE qbatch_id = %s""",
            (resolved_by, qbatch_id),
        )
    return {"qbatch_id": qbatch_id, "status": "ignored", "rows_dropped": dropped}
