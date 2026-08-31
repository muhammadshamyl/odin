"""Operations Deck aggregation — the numbers behind the home screen.

One read-only pass over ``run_log`` + the two diversion logs + the registry,
rolled up into the exact shape ``home.html`` renders (KPIs, per-table layer
health, an alert feed, and four 16-hour sparkline series). Every query here is
set-based; nothing is computed row-by-row.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from odin import jobs, registry
from odin.db import connection

_SPARK_HOURS = 16
_QTN_PAUSE_LINE = 5.0                       # % — spec's auto-pause threshold
_WAIT_BACKLOG_AGE = timedelta(hours=24)


def _short_age(delta: timedelta | None) -> str:
    """A compact 'Xd Yh' / 'Xh Ym' / 'Xm' string for the KPI captions."""
    if delta is None:
        return "—"
    secs = max(int(delta.total_seconds()), 0)
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h {secs % 3600 // 60}m"
    return f"{secs // 86400}d {secs % 86400 // 3600}h"


def _dot(present: bool, running: bool, failed: bool, ok: bool) -> str:
    """Roll one stage's per-batch run_log rows into a single status-dot class."""
    if failed:
        return "crit"
    if running:
        return "run"
    if ok:
        return "ok"
    if present:
        return "warn"
    return "idle"


def deck_summary() -> dict:
    now = datetime.now(timezone.utc)
    tables = registry.list_tables()
    active_jobs = list(jobs.active())

    with connection() as conn, conn.cursor() as cur:
        # --- 24h vs prior-24h totals (the transform writes these on PRODUCTION rows) ---
        cur.execute(
            """
            SELECT
              coalesce(sum(rows_to_production)
                FILTER (WHERE ended_at > now() - interval '24 hours'), 0)          AS prod_24h,
              coalesce(sum(rows_to_production)
                FILTER (WHERE ended_at >  now() - interval '48 hours'
                        AND   ended_at <= now() - interval '24 hours'), 0)         AS prod_prev_24h,
              coalesce(sum(rows_quarantined)
                FILTER (WHERE ended_at > now() - interval '24 hours'), 0)          AS qtn_24h,
              coalesce(sum(rows_to_waiting)
                FILTER (WHERE ended_at > now() - interval '24 hours'), 0)          AS wait_24h
            FROM run_log
            WHERE stage = 'PRODUCTION' AND status = 'success'
            """
        )
        tot = cur.fetchone()

        # --- raw PRODUCTION rows for the last N hours -> Python-bucketed sparklines ---
        cur.execute(
            """
            SELECT ended_at, rows_to_production, rows_quarantined, rows_to_waiting
            FROM run_log
            WHERE stage = 'PRODUCTION' AND status = 'success'
              AND ended_at > now() - (%s * interval '1 hour')
            """,
            (_SPARK_HOURS,),
        )
        spark_rows = cur.fetchall()

        # --- per-table state of the most recent run ---
        cur.execute(
            """
            WITH latest AS (
              SELECT DISTINCT ON (source_id, table_name) source_id, table_name, run_id
              FROM run_log
              WHERE source_id IS NOT NULL
              ORDER BY source_id, table_name, id DESC
            )
            SELECT r.source_id, r.table_name,
              bool_or(r.stage = 'EXTRACT')                              AS has_ex,
              bool_or(r.stage = 'EXTRACT'    AND r.status = 'running')   AS ex_run,
              bool_or(r.stage = 'EXTRACT'    AND r.status = 'failed')    AS ex_fail,
              bool_or(r.stage = 'EXTRACT'    AND r.status = 'success')   AS ex_ok,
              bool_or(r.stage = 'PRODUCTION')                           AS has_pr,
              bool_or(r.stage = 'PRODUCTION' AND r.status = 'running')   AS pr_run,
              bool_or(r.stage = 'PRODUCTION' AND r.status = 'failed')    AS pr_fail,
              bool_or(r.stage = 'PRODUCTION' AND r.status = 'success')   AS pr_ok,
              min(r.error_message) FILTER (WHERE r.error_message IS NOT NULL) AS err
            FROM run_log r JOIN latest l USING (source_id, table_name, run_id)
            GROUP BY r.source_id, r.table_name
            """
        )
        latest = {(r["source_id"], r["table_name"]): r for r in cur.fetchall()}

        # --- all-time last success per table ---
        cur.execute(
            """
            SELECT source_id, table_name, max(ended_at) AS last_ok
            FROM run_log
            WHERE status = 'success' AND ended_at IS NOT NULL AND source_id IS NOT NULL
            GROUP BY source_id, table_name
            """
        )
        last_ok = {(r["source_id"], r["table_name"]): r["last_ok"] for r in cur.fetchall()}

        # --- open quarantine / pending waiting, grouped per table ---
        cur.execute(
            """
            SELECT source_id, table_name,
                   coalesce(sum(row_count), 0) AS rows, count(*) AS batches
            FROM quarantine_batch_log WHERE resolution_status = 'open'
            GROUP BY source_id, table_name
            """
        )
        qtn = {(r["source_id"], r["table_name"]): r for r in cur.fetchall()}

        cur.execute(
            """
            SELECT source_id, table_name,
                   coalesce(sum(row_count), 0) AS rows, count(*) AS batches,
                   min(created_at) AS oldest
            FROM waiting_batch_log WHERE status = 'pending'
            GROUP BY source_id, table_name
            """
        )
        wait = {(r["source_id"], r["table_name"]): r for r in cur.fetchall()}

        # --- waiting-batch history for the 'pending restatements' sparkline ---
        cur.execute(
            """
            SELECT created_at, resolved_at FROM waiting_batch_log
            WHERE status = 'pending'
               OR resolved_at > now() - (%s * interval '1 hour')
            """,
            (_SPARK_HOURS,),
        )
        wait_hist = cur.fetchall()

        # --- recent structural schema changes (drift alerts) ---
        cur.execute(
            """
            SELECT source_id, table_name, column_name, change_type, changed_timestamp
            FROM registry_change_log
            WHERE change_type IN ('ADD', 'REMOVE', 'TYPE_CHANGE', 'RENAME')
              AND changed_timestamp > now() - interval '7 days'
            ORDER BY changed_timestamp DESC LIMIT 5
            """
        )
        drift = cur.fetchall()

    # ---- KPI: rows -> production -------------------------------------------
    prod_24h = int(tot["prod_24h"])
    prev_24h = int(tot["prod_prev_24h"])
    delta_pct = ((prod_24h - prev_24h) / prev_24h * 100) if prev_24h else None

    qtn_24h, wait_24h = int(tot["qtn_24h"]), int(tot["wait_24h"])
    denom = prod_24h + qtn_24h + wait_24h
    qtn_rate = (qtn_24h / denom * 100) if denom else 0.0

    # ---- per-table layer health + live-job overlay ----------------------
    running_keys = {(j.source_id, j.table_name): j.kind for j in active_jobs}
    health, running_now, failed_now = [], 0, 0
    for t in tables:
        key = (t["source_id"], t["table_name"])
        jk = [j.kind for j in active_jobs if (j.source_id, j.table_name) == key]
        j_ex = any(k in ("extract", "ingest") for k in jk)
        j_tr = any(k == "transform" for k in jk)
        L = latest.get(key)
        if L:
            ex = _dot(L["has_ex"], L["ex_run"] or j_ex, L["ex_fail"], L["ex_ok"])
            pr = _dot(L["has_pr"], L["pr_run"] or j_tr, L["pr_fail"], L["pr_ok"])
        else:
            ex = "run" if j_ex else "idle"
            pr = "run" if j_tr else "idle"
        stg = {"ok": "ok", "run": "run", "crit": "crit"}.get(ex, "idle")
        q, w = qtn.get(key), wait.get(key)
        row_state = ("fail" if "crit" in (ex, pr)
                     else "run" if "run" in (ex, pr) else "")
        running_now += row_state == "run"
        failed_now += row_state == "fail"
        health.append({
            "source_id": t["source_id"], "table_name": t["table_name"],
            "load_type": t["load_type"],
            "extract": ex, "staging": stg, "production": pr, "business": "idle",
            "last_ok": last_ok.get(key),
            "qtn_rows": int(q["rows"]) if q else 0,
            "wait_rows": int(w["rows"]) if w else 0,
            "row_state": row_state,
        })
    total = len(tables)
    active_registered = sum(1 for t in tables if t["status"] == "ACTIVE")
    idle_now = max(total - running_now - failed_now, 0)

    # ---- pending restatements -------------------------------------------
    pending_batches = sum(int(w["batches"]) for w in wait.values())
    oldest_wait = min((w["oldest"] for w in wait.values() if w["oldest"]), default=None)

    # ---- sparklines: 16 hourly buckets, oldest -> newest ----------------
    edges = [now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=k)
             for k in range(_SPARK_HOURS - 1, -1, -1)]
    bucket = {e: [0, 0, 0] for e in edges}          # [prod, qtn, wait]
    for r in spark_rows:
        ea = r["ended_at"]
        if ea.tzinfo is None:
            ea = ea.replace(tzinfo=timezone.utc)
        e = ea.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        if e in bucket:
            bucket[e][0] += int(r["rows_to_production"] or 0)
            bucket[e][1] += int(r["rows_quarantined"] or 0)
            bucket[e][2] += int(r["rows_to_waiting"] or 0)

    prod_series, qtn_series, wait_series = [], [], []
    for e in edges:
        p, qv, wv = bucket[e]
        prod_series.append(p)
        d = p + qv + wv
        qtn_series.append(round(qv / d * 100, 2) if d else 0)
        wait_series.append(sum(
            1 for h in wait_hist
            if h["created_at"] <= e
            and (h["resolved_at"] is None or h["resolved_at"] > e)
        ))
    active_series = [total - failed_now] * _SPARK_HOURS

    # ---- alert feed ---------------------------------------------------
    alerts: list[dict] = []
    for h in health:
        if h["row_state"] != "fail":
            continue
        L = latest.get((h["source_id"], h["table_name"]), {})
        alerts.append({
            "kind": "crit",
            "title": f"Run failed — {h['source_id']} / {h['table_name']}",
            "meta": f"{L.get('err') or 'no error message'} · no auto-retry — DE action required",
        })
    for (sid, tbl), q in qtn.items():
        alerts.append({
            "kind": "warn",
            "title": f"Quarantine open — {sid} / {tbl}",
            "meta": f"{int(q['rows'])} row(s) held · {int(q['batches'])} batch(es)",
        })
    stale = [w for w in wait.values()
             if w["oldest"] and (now - w["oldest"]) > _WAIT_BACKLOG_AGE]
    if stale:
        n = sum(int(w["batches"]) for w in stale)
        alerts.append({
            "kind": "info",
            "title": f"Waiting backlog — {n} batch(es) pending > 24h",
            "meta": f"oldest {_short_age(now - min(w['oldest'] for w in stale))} ago",
        })
    for d in drift:
        col = f" {d['column_name']}" if d["column_name"] else ""
        alerts.append({
            "kind": "info",
            "title": f"Schema drift — {d['source_id']} / {d['table_name']}",
            "meta": f"{d['change_type']}{col} · {d['changed_timestamp']:%H:%M} UTC",
        })
    if not alerts:
        alerts.append({
            "kind": "info", "title": "All clear",
            "meta": "no failures · no open quarantine · no stale waiting batches",
        })

    return {
        "kpi": {
            "prod_24h": prod_24h,
            "prod_delta_pct": delta_pct,
            "qtn_rate": qtn_rate,
            "qtn_pause_line": _QTN_PAUSE_LINE,
            "pending_batches": pending_batches,
            "oldest_wait_age": _short_age(now - oldest_wait if oldest_wait else None),
            "pipelines_total": total,
            "pipelines_active": active_registered,
            "running_now": running_now,
            "failed_now": failed_now,
            "idle_now": idle_now,
        },
        "spark": {
            "prod": ",".join(map(str, prod_series)),
            "qtn": ",".join(map(str, qtn_series)),
            "waiting": ",".join(map(str, wait_series)),
            "active": ",".join(map(str, active_series)),
        },
        "health": health,
        "alerts": alerts,
    }
