"""FastAPI app for the Odin web UI (Slice 1).

Server-rendered HTML, no auth (local tool). Reads go straight to `registry` /
`resolve`; `extract` / `transform` are dispatched to a background worker
(`odin.jobs`) so the browser never blocks on a long run — the runs panel polls
for progress. Every path calls the same functions the CLI uses.

Run:  `odin web`  (or `uvicorn odin.web.app:app --reload`)
"""

from __future__ import annotations

import json
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg import sql

from odin import casts, jobs, registry, resolve, sqlconsole
from odin.config import settings
from odin.connectors import file as fc
from odin.db import connection
from odin.naming import qname
from odin.web import deck, sqlview

_HERE = Path(__file__).parent
_TEMPLATES = Jinja2Templates(directory=str(_HERE / "templates"))


def _compact(n) -> str:
    """Row count for a card: 999 -> "999", 1000 -> "1k", 1234 -> "1.23k",
    2_500_000 -> "2.5M" (up to 2 decimals, trailing zeros trimmed). Non-numbers
    pass straight through (so "—" / None-guards still work)."""
    try:
        x = float(n)
    except (TypeError, ValueError):
        return n
    if x != x:  # NaN
        return "—"
    a = abs(x)
    if a < 1000:
        return str(int(x))
    units = ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "k"))
    for i, (div, suf) in enumerate(units):
        if a >= div:
            s = f"{a / div:.2f}".rstrip("0").rstrip(".")
            if s == "1000" and i > 0:              # rounded up into the next unit
                div, suf, s = *units[i - 1][:2], "1"
            return ("-" if x < 0 else "") + s + suf
    return str(int(x))


_TEMPLATES.env.filters["compact"] = _compact


def _reconcile_stale_runs() -> None:
    """A `run_log` row still 'running' at startup can't actually be running — the
    process that owned it is gone (crash, or `uvicorn --reload` restart). Mark it
    failed so the UI stops polling and the state is honest. A live CLI run that
    finishes later will overwrite its own row via `runlog.finish`.
    """
    with connection() as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE run_log SET status = 'failed', ended_at = now(), "
            "error_message = concat_ws(' ', error_message, "
            "'[reconciled: process exited mid-run]') "
            "WHERE status = 'running' AND started_at < now() - interval '15 seconds'"
        )
        n = cur.rowcount
    if n:
        print(f"[startup] reconciled {n} stale 'running' run_log row(s) -> failed")


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _reconcile_stale_runs()
    yield


app = FastAPI(title="Odin", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _render(request: Request, name: str, **ctx) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, name, ctx)


def _redirect(path: str, **params) -> RedirectResponse:
    params = {k: v for k, v in params.items() if v}
    if params:
        path = f"{path}?{urlencode(params)}"
    return RedirectResponse(path, status_code=303)


def _is_hx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _hx(message: str, kind: str = "ok", **events) -> dict:
    """HX-Trigger header carrying a toast (+ optional extra client events)."""
    payload = {"odin:toast": {"message": message, "kind": kind}}
    payload.update(events)
    return {"HX-Trigger": json.dumps(payload)}


def _panel_url(source: str = "", table: str = "") -> str:
    qs = urlencode({k: v for k, v in {"source": source, "table": table}.items() if v})
    return "/partials/runs" + (f"?{qs}" if qs else "")


_PER_PAGE = 50


def _wq_page(which: str, source: str, table: str, page: int) -> dict:
    """A page of pending waiting / open quarantine batches for the inline panel:
    the slice of rows plus filter-wide totals (so the KPI tiles stay correct)."""
    s, t = source or None, table or None
    if which == "waiting":
        total = resolve.count_pending_waiting(s, t)
        get, stats = resolve.pending_waiting, resolve.waiting_stats(s, t)
    else:
        total = resolve.count_open_quarantine(s, t)
        get, stats = resolve.open_quarantine, resolve.quarantine_stats(s, t)
    pages = max((total + _PER_PAGE - 1) // _PER_PAGE, 1)
    page = min(max(page, 1), pages)
    items = get(s, t, limit=_PER_PAGE, offset=(page - 1) * _PER_PAGE)
    return {"which": which, "entries": items, "total": total, "page": page,
            "pages": pages, "per_page": _PER_PAGE, "source": source, "table": table,
            "stats": stats}


def _count(table: str) -> int:
    with connection() as conn, conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(qname(table)))
        return cur.fetchone()["n"]


def _count_safe(table: str) -> int | None:
    """`_count`, but `None` if the table is missing — the polled lineage partial
    must never 500 just because a table was dropped out from under it."""
    try:
        return _count(table)
    except Exception:  # noqa: BLE001 - missing/renamed table -> no number to show
        return None


def _stage_state(present: bool, running: bool, failed: bool) -> str | None:
    """Roll a stage's per-batch run_log rows into one state for the UI.
    None = the stage has not started for this run."""
    if not present:
        return None
    if failed:
        return "failed"
    if running:
        return "running"
    return "success"


def _recent_runs(source_id: str | None = None, table_name: str | None = None,
                 limit: int = 50) -> list[dict]:
    """One row per `run_id` (not per run_log row). Extract's per-100k-row batches
    and the transform collapse into a single row with a state per stage."""
    clauses, params = [], []
    if source_id:
        clauses.append("source_id = %s")
        params.append(source_id)
    if table_name:
        clauses.append("table_name = %s")
        params.append(table_name)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT run_id, "
            "  max(source_id)    AS source_id, "
            "  max(table_name)   AS table_name, "
            "  max(triggered_by) AS triggered_by, "
            "  min(started_at)   AS started_at, "
            "  max(started_at)   AS last_started_at, "
            "  max(ended_at)     AS ended_at, "
            "  EXTRACT(EPOCH FROM (max(ended_at) - min(started_at)))::numeric(12,2) AS duration_s, "
            "  count(*) FILTER (WHERE stage = 'EXTRACT')                      AS extract_batches, "
            "  bool_or(stage = 'EXTRACT')                                     AS has_extract, "
            "  bool_or(stage = 'EXTRACT'    AND status = 'running')           AS extract_running, "
            "  bool_or(stage = 'EXTRACT'    AND status = 'failed')            AS extract_failed, "
            "  bool_or(stage = 'PRODUCTION')                                  AS has_transform, "
            "  bool_or(stage = 'PRODUCTION' AND status = 'running')           AS transform_running, "
            "  bool_or(stage = 'PRODUCTION' AND status = 'failed')            AS transform_failed, "
            "  sum(rows_processed) FILTER (WHERE stage = 'EXTRACT')           AS rows_processed, "
            "  sum(rows_to_production)                                        AS rows_to_production, "
            "  sum(rows_to_waiting)                                           AS rows_to_waiting, "
            "  sum(rows_quarantined)                                          AS rows_quarantined, "
            "  min(error_message) FILTER (WHERE error_message IS NOT NULL)    AS error_message "
            f"FROM run_log {where} "
            "GROUP BY run_id ORDER BY max(id) DESC LIMIT %s",
            (*params, limit),
        )
        rows = cur.fetchall()
    for r in rows:
        r["extract_state"] = _stage_state(
            r["has_extract"], r["extract_running"], r["extract_failed"])
        r["transform_state"] = _stage_state(
            r["has_transform"], r["transform_running"], r["transform_failed"])
        # extract is now one run_log row per run; show the "N×100k" hint from the
        # row count instead of a physical batch-row tally.
        read = r["rows_processed"] or 0
        r["extract_batches"] = -(-read // settings.batch_rows) if read else 0
        r["status"] = (
            "failed" if (r["extract_failed"] or r["transform_failed"])
            else "running" if (r["extract_running"] or r["transform_running"])
            else "success"
        )
    return rows


def _pipeline_stage_state(source_id: str, table: str, *,
                          waiting_rows: int = 0, quarantine_rows: int = 0) -> dict:
    """Per-node state for the animated lineage on the table page: where is the
    most recent run right now? Node values: 'running' | 'done' | 'failed' | None.
    Falls back to live jobs for the beat before the first run_log row lands.
    The waiting / quarantine diversions light up **only** once a row has actually
    landed there (held rows now, or routed on the last run) — never just because
    a transform is running."""
    runs = _recent_runs(source_id, table, 1)
    r0 = runs[0] if runs else {}
    ex = r0.get("extract_state")
    tr = r0.get("transform_state")
    for j in jobs.active():
        if j.source_id != source_id or j.table_name != table:
            continue
        if j.kind in ("extract", "ingest") and ex is None:
            ex = "running"
        if j.kind == "transform" and tr is None:
            tr = "running"

    def d(s: str | None) -> str | None:
        return "done" if s == "success" else s  # 'running' / 'failed' / None pass

    ex, tr = d(ex), d(tr)
    to_waiting = r0.get("rows_to_waiting") or 0
    to_quarantined = r0.get("rows_quarantined") or 0
    return {
        "source": "done" if (runs or ex) else None,
        "extract": ex,
        "staging": "done" if ex == "done" else ("running" if ex == "running" else None),
        "transform": tr,
        "production": "done" if tr == "done" else ("running" if tr == "running" else None),
        "waiting": "done" if (waiting_rows or to_waiting) else None,
        "quarantine": "done" if (quarantine_rows or to_quarantined) else None,
    }


def _row_cols(rows: list[dict]) -> list[str]:
    return list(rows[0].keys()) if rows else []


def _resolve_panel(request: Request, which: str, source: str, table: str,
                   page: int, action):
    """Run `action()` (returns a success message; may raise), then re-render the
    inline batch panel for an htmx request (on the same page), or redirect."""
    try:
        msg, kind = action(), "ok"
    except Exception as exc:  # noqa: BLE001 - resolve.* are transactional; surface as a toast
        msg, kind = str(exc), "err"
    if _is_hx(request):
        macro = "waiting_panel" if which == "waiting" else "quarantine_panel"
        panel = _wq_page(which, source, table, page)
        html = getattr(_TEMPLATES.env.get_template("_macros.html").module, macro)(panel)
        return HTMLResponse(str(html), headers=_hx(msg, kind))
    base = "/waiting" if which == "waiting" else "/quarantine"
    return _redirect(base, msg=(msg if kind == "ok" else f"error: {msg}"))


async def _column_types_from_form(request: Request, columns: list[str]) -> tuple[dict, set]:
    """Pull `type_<col>` / `req_<col>` fields out of a submitted form.
    Absent ⇒ that column stays `text` / nullable (current behaviour)."""
    form = await request.form()
    column_types = {
        c: str(form[f"type_{c}"]) for c in columns if form.get(f"type_{c}")
    }
    required = {c for c in columns if form.get(f"req_{c}")}
    return column_types, required


async def _natural_key_from_form(request: Request, columns: list[str]) -> list[str]:
    """The repeated `nk_col` dropdowns → key columns, in pick order, de-duplicated."""
    form = await request.form()
    seen: set[str] = set()
    out: list[str] = []
    for v in form.getlist("nk_col"):
        if v and v in columns and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _save_upload(upload: UploadFile) -> Path:
    settings.ensure_dirs()
    suffix = Path(upload.filename or "upload").suffix.lower() or ".csv"
    dest = settings.upload_dir / f"{uuid.uuid4().hex}{suffix}"
    with dest.open("wb") as fh:
        shutil.copyfileobj(upload.file, fh)
    return dest


# --------------------------------------------------------------------------- #
# home + runs
# --------------------------------------------------------------------------- #

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return _render(request, "home.html", deck=deck.deck_summary())


@app.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request, source: str = "", table: str = ""):
    return _render(request, "runs.html", source=source, table=table,
                   panel_url=_panel_url(source, table))


@app.get("/partials/runs", response_class=HTMLResponse)
def partial_runs(request: Request, source: str = "", table: str = ""):
    from datetime import datetime, timedelta, timezone

    runs = _recent_runs(source or None, table or None, 40)
    act = jobs.active()
    fresh = datetime.now(timezone.utc) - timedelta(minutes=15)
    # keep polling only while a job is queued/running OR a run_log row is
    # genuinely fresh-and-running (an old 'running' row is a zombie)
    live_run = any(
        r["status"] == "running" and r["last_started_at"] and r["last_started_at"] > fresh
        for r in runs
    )
    polling = bool(act) or live_run
    return _render(request, "_runs_panel.html", runs=runs, jobs=act,
                   failed_jobs=jobs.recent_failed(120), polling=polling,
                   poll_url=_panel_url(source, table))


# --------------------------------------------------------------------------- #
# SQL console
# --------------------------------------------------------------------------- #

@app.get("/sql", response_class=HTMLResponse)
def sql_console_page(request: Request):
    tree = sqlconsole.schema_tree()
    db = settings.database_url.rsplit("/", 1)[-1].split("?")[0] or "odin"
    n_tables = sum(len(s["tables"]) for s in tree["sources"]) + len(tree["platform"])
    return _render(request, "sql.html", tree=tree,
                   hint_tables=json.dumps(sqlconsole.hint_tables(tree)),
                   snippets=sqlconsole.snippets(tree),
                   pending=sqlconsole.pending_view(),
                   history=sqlconsole.history(),
                   db_label=db, schema_table_count=n_tables,
                   timeout=settings.sql_timeout_seconds,
                   row_cap=settings.sql_row_cap)


@app.get("/partials/sql/schema", response_class=HTMLResponse)
def sql_console_schema(request: Request):
    return _render(request, "_sql_schema.html", tree=sqlconsole.schema_tree())


@app.post("/sql/run", response_class=HTMLResponse)
async def sql_console_run(request: Request):
    form = await request.form()
    res = sqlconsole.run(
        str(form.get("sql", "")),
        read_only=(form.get("write_mode") != "1"),
        by_label=str(form.get("by", "") or ""),
        full=(form.get("full") == "1"),
    )
    return _render(request, "_sql_result.html", **res)


@app.post("/sql/commit", response_class=HTMLResponse)
async def sql_console_commit(request: Request):
    form = await request.form()
    res = sqlconsole.commit(str(form.get("token", "")), str(form.get("confirm", "")))
    resp = _render(request, "_sql_result.html", **res)
    if res["committed"]:
        resp.headers.update(_hx("Committed", "ok", **{"odin:runs-changed": True}))
    return resp


@app.post("/sql/discard", response_class=HTMLResponse)
async def sql_console_discard(request: Request):
    form = await request.form()
    res = sqlconsole.discard(str(form.get("token", "")))
    return _render(request, "_sql_result.html", **res)


@app.get("/partials/sql/deregister", response_class=HTMLResponse)
def sql_console_deregister_preview(request: Request, source_id: str = "", table_name: str = "",
                                  physical_only: str = "", keep_history: str = ""):
    if not source_id or not table_name:
        return HTMLResponse("")
    try:
        plan = registry.deregister_plan(
            source_id, table_name,
            physical_only=(physical_only == "1"), keep_history=(keep_history == "1"))
    except registry.RegistryError as exc:
        return HTMLResponse(f'<p class="sql-err">{exc}</p>')
    busy = any(j.source_id == source_id and j.table_name == table_name
               for j in jobs.active())
    return _render(request, "_sql_deregister.html", plan=plan,
                   source_id=source_id, table_name=table_name, busy=busy)


@app.post("/sql/deregister", response_class=HTMLResponse)
async def sql_console_deregister(request: Request):
    form = await request.form()
    source_id = str(form.get("source_id", ""))
    table_name = str(form.get("table_name", ""))
    physical_only = form.get("physical_only") == "1"
    keep_history = form.get("keep_history") == "1"
    confirm = str(form.get("confirm", "")).strip()

    if any(j.source_id == source_id and j.table_name == table_name for j in jobs.active()):
        return HTMLResponse('<p class="sql-err">A job is running for this source — '
                            'wait for it to finish, then retry.</p>',
                            headers=_hx("Blocked — job running", "err"))
    if confirm != f"{source_id}.{table_name}":
        return HTMLResponse(f'<p class="sql-err">Type <code>{source_id}.{table_name}</code> '
                            'exactly to confirm.</p>', headers=_hx("Confirmation did not match", "err"))
    try:
        r = registry.deregister_source(source_id, table_name,
                                       physical_only=physical_only, keep_history=keep_history)
    except registry.RegistryError as exc:
        return HTMLResponse(f'<p class="sql-err">{exc}</p>', headers=_hx(str(exc), "err"))

    sqlconsole._audit("; ".join(r["dropped"]), "ddl", False, row_count=None,
                      elapsed_ms=None, status="ok",
                      error=None, by_label="deregister")
    n = sum(v for v in r["deleted"].values())
    return HTMLResponse(
        f'<p class="sql-ok">Removed <b>{source_id}.{table_name}</b> — dropped '
        f'{len(r["dropped"])} table(s), deleted {n:,} control row(s).</p>',
        headers=_hx(f"Deregistered {source_id}.{table_name}", "ok",
                    **{"odin:runs-changed": True}))


# --------------------------------------------------------------------------- #
# onboarding
# --------------------------------------------------------------------------- #

@app.get("/onboard", response_class=HTMLResponse)
def onboard_form(request: Request):
    return _render(request, "onboard.html")


@app.post("/onboard/upload")
def onboard_upload(file: UploadFile = File(...)):
    path = _save_upload(file)
    return _redirect("/onboard/preview", f=path.name)


@app.get("/onboard/preview", response_class=HTMLResponse)
def onboard_preview(request: Request, f: str):
    path = settings.upload_dir / f
    if not path.is_file():
        return _redirect("/onboard", msg="error: upload not found, try again")
    return _render(request, "preview.html", f=f, fmt=fc.detect_format(path),
                   header=fc.read_header(path), rows=fc.sample_rows(path, 50),
                   cast_labels=casts.LABELS)


@app.post("/onboard/create")
async def onboard_create(
    request: Request,
    f: str = Form(...),
    name: str = Form(...),
    table: str = Form(...),
    load_type: str = Form(...),
    existence_column: str = Form(""),
    recurrence: str = Form("ONE_TIME"),
    owner: str = Form(""),
    run_now: str = Form(""),
):
    path = settings.upload_dir / f
    if not path.is_file():
        return _redirect("/onboard", msg="error: upload not found, try again")
    header = fc.read_header(path)
    fmt = fc.detect_format(path)
    column_types, required = await _column_types_from_form(request, header)
    natural_key = await _natural_key_from_form(request, header)
    try:
        cfg = registry.onboard_file_source(
            source_name=name, file_format=fmt, table_name=table, columns=header,
            load_type=load_type, existence_check_column=(existence_column or None),
            load_recurrence=recurrence, owner=(owner or None),
            column_types=column_types, required=required,
            natural_key=natural_key,
        )
    except registry.RegistryError as exc:
        return _render(request, "preview.html", f=f, fmt=fmt, header=header,
                       rows=fc.sample_rows(path, 50), error=str(exc),
                       cast_labels=casts.LABELS)

    dest = f"/t/{cfg.source_id}/{cfg.table_name}"
    if run_now:
        jobs.submit("ingest", cfg.source_id, cfg.table_name,
                    file=str(path), triggered_by="onboarding")
        return _redirect(dest, msg="Pipeline created — first run started")
    return _redirect(dest, msg="Pipeline created")


# --------------------------------------------------------------------------- #
# per-table
# --------------------------------------------------------------------------- #

@app.get("/t/{source_id}/{table}", response_class=HTMLResponse)
def table_detail(request: Request, source_id: str, table: str):
    try:
        cfg = registry.get_table(source_id, table)
    except registry.RegistryError:
        return _redirect("/", msg=f"error: {source_id}.{table} not registered")
    live = any(j.source_id == source_id and j.table_name == table
               for j in jobs.active())
    col_meta = registry.get_columns_meta(source_id, table)
    waiting_rows = _count_safe(cfg.waiting_target) or 0
    quarantine_rows = _count_safe(cfg.quarantine_target) or 0
    return _render(request, "table.html",
                   cfg=cfg, columns=[c["column_name"] for c in col_meta],
                   cast_labels=casts.LABELS,
                   col_types={c["column_name"]: c["target_data_type"] for c in col_meta},
                   req_cols=[c["column_name"] for c in col_meta if not c["is_nullable"]],
                   sql_blocks=sqlview.pipeline_sql(source_id, table),
                   waiting=resolve.pending_waiting(source_id, table, limit=8),
                   quarantine=resolve.open_quarantine(source_id, table, limit=8),
                   waiting_count=resolve.count_pending_waiting(source_id, table),
                   quarantine_count=resolve.count_open_quarantine(source_id, table),
                   waiting_rows=waiting_rows, quarantine_rows=quarantine_rows,
                   prod_count=_count_safe(cfg.production_target),
                   staging_count=_count_safe(cfg.staging_target),
                   dereg=registry.deregister_plan(source_id, table),
                   panel_url=_panel_url(source_id, table),
                   state=_pipeline_stage_state(
                       source_id, table,
                       waiting_rows=waiting_rows, quarantine_rows=quarantine_rows,
                   ), polling=live)


@app.get("/partials/lineage/{source_id}/{table}", response_class=HTMLResponse)
def partial_lineage(request: Request, source_id: str, table: str):
    try:
        cfg = registry.get_table(source_id, table)
    except registry.RegistryError:
        return HTMLResponse("", status_code=404)
    live = any(j.source_id == source_id and j.table_name == table
               for j in jobs.active())
    waiting = resolve.pending_waiting(source_id, table)
    quarantine = resolve.open_quarantine(source_id, table)
    waiting_rows = _count_safe(cfg.waiting_target) or 0
    quarantine_rows = _count_safe(cfg.quarantine_target) or 0
    return _render(request, "_lineage.html", cfg=cfg,
                   state=_pipeline_stage_state(
                       source_id, table,
                       waiting_rows=waiting_rows, quarantine_rows=quarantine_rows,
                   ),
                   waiting=waiting, quarantine=quarantine,
                   waiting_rows=waiting_rows, quarantine_rows=quarantine_rows,
                   staging_count=_count_safe(cfg.staging_target),
                   prod_count=_count_safe(cfg.production_target),
                   polling=live)


@app.get("/partials/stats/{source_id}/{table}", response_class=HTMLResponse)
def partial_stats(request: Request, source_id: str, table: str):
    """The 4 KPI cards on the table page, self-refreshed on `odin:runs-changed`
    so an extract / transform / load updates them without a full page reload."""
    try:
        cfg = registry.get_table(source_id, table)
    except registry.RegistryError:
        return HTMLResponse("", status_code=404)
    live = any(j.source_id == source_id and j.table_name == table
               for j in jobs.active())
    return _render(request, "_data_stats.html", cfg=cfg,
                   staging_count=_count_safe(cfg.staging_target),
                   prod_count=_count_safe(cfg.production_target),
                   waiting_rows=_count_safe(cfg.waiting_target) or 0,
                   quarantine_rows=_count_safe(cfg.quarantine_target) or 0,
                   polling=live)


@app.post("/t/{source_id}/{table}/extract")
def table_extract(request: Request, source_id: str, table: str,
                  file: UploadFile = File(...)):
    path = _save_upload(file)
    jobs.submit("extract", source_id, table, file=str(path))
    msg = "Extract queued — progress shows in Runs"
    if _is_hx(request):
        return HTMLResponse("", headers=_hx(msg, "ok", **{"odin:runs-changed": True}))
    return _redirect(f"/t/{source_id}/{table}", msg=msg)


@app.post("/t/{source_id}/{table}/transform")
def table_transform(request: Request, source_id: str, table: str):
    jobs.submit("transform", source_id, table)
    msg = "Transform queued — progress shows in Runs"
    if _is_hx(request):
        return HTMLResponse("", headers=_hx(msg, "ok", **{"odin:runs-changed": True}))
    return _redirect(f"/t/{source_id}/{table}", msg=msg)


@app.post("/t/{source_id}/{table}/load", response_class=HTMLResponse)
def table_load(request: Request, source_id: str, table: str,
               file: UploadFile | None = File(None),
               saved: str = Form(""), flush: str = Form(""), discard: str = Form("")):
    """The primary "get this file into production" action: one `ingest` job that
    extracts to staging then transforms into production under one run_id. If
    staging still holds un-transformed rows, ask first — then either drain them
    with a transform (`flush=1`) or `TRUNCATE` them away (`discard=1`) before
    loading the new file (single worker ⇒ ordered)."""
    try:
        cfg = registry.get_table(source_id, table)
    except registry.RegistryError:
        return HTMLResponse("", headers=_hx(f"{source_id}.{table} is not registered", "err"))

    if any(j.source_id == source_id and j.table_name == table for j in jobs.active()):
        return HTMLResponse(
            "", headers=_hx("a job is already running for this pipeline — wait for it to finish", "err"))

    if file is not None and file.filename:
        path = _save_upload(file)
        original = file.filename
    elif saved:
        path = settings.upload_dir / Path(saved).name  # strip any path parts
        original = path.name
        if not path.is_file():
            return HTMLResponse("", headers=_hx("that upload expired — pick the file again", "err"))
    else:
        return HTMLResponse("", headers=_hx("no file selected", "err"))

    staged = _count_safe(cfg.staging_target) or 0
    if staged and flush != "1" and discard != "1":
        return _render(request, "_load_confirm.html",
                       cfg=cfg, staging_count=staged, saved=path.name, filename=original)

    if staged and discard == "1":
        with connection() as conn, conn.cursor() as cur:
            cur.execute(sql.SQL("TRUNCATE {}").format(qname(cfg.staging_target)))
        jobs.submit("ingest", source_id, table, file=str(path))
        msg = f"Discarded {staged:,} staged row(s); loading {original} → production"
    elif staged:  # flush == "1"
        jobs.submit("transform", source_id, table)
        jobs.submit("ingest", source_id, table, file=str(path))
        msg = f"Flushing {staged:,} staged row(s) to production, then loading {original}"
    else:
        jobs.submit("ingest", source_id, table, file=str(path))
        msg = f"Loading {original} → production — progress shows in Runs"

    return HTMLResponse("", headers=_hx(msg, "ok", **{"odin:runs-changed": True}))


@app.post("/t/{source_id}/{table}/retype")
async def table_retype(request: Request, source_id: str, table: str):
    """Re-cast production columns (migrating existing data). Runs synchronously
    so the DE sees the pass/fail — a failed cast names the offending values."""
    try:
        cols = registry.get_columns(source_id, table)
    except registry.RegistryError:
        return _redirect("/", msg=f"error: {source_id}.{table} not registered")
    column_types, required = await _column_types_from_form(request, cols)
    try:
        r = registry.retype_table(source_id, table, column_types, required)
    except registry.RegistryError as exc:
        return _redirect(f"/t/{source_id}/{table}", msg=f"error: {exc}")
    msg = (f"Re-typed {', '.join(r['changed'])} — {r['rows_migrated']:,} row(s) migrated"
           if r.get("changed") else "No type changes")
    return _redirect(f"/t/{source_id}/{table}", msg=msg)


@app.post("/t/{source_id}/{table}/deregister")
def table_deregister(request: Request, source_id: str, table: str,
                     confirm: str = Form(""), physical_only: str = Form(""),
                     keep_history: str = Form("")):
    """Delete the whole pipeline — the four physical tables + registry / control
    rows, in one transaction (`registry.deregister_source`). Blocked while a job
    is running for it; needs the `source.table` name typed to confirm."""
    try:
        registry.get_table(source_id, table)
    except registry.RegistryError:
        return _redirect("/", msg=f"error: {source_id}.{table} is not registered")
    if any(j.source_id == source_id and j.table_name == table for j in jobs.active()):
        return _redirect(f"/t/{source_id}/{table}",
                         msg="error: a job is running for this pipeline — wait for it to finish")
    if confirm.strip() != f"{source_id}.{table}":
        return _redirect(f"/t/{source_id}/{table}",
                         msg=f"error: type {source_id}.{table} exactly to confirm deletion")
    try:
        r = registry.deregister_source(
            source_id, table,
            physical_only=(physical_only == "1"), keep_history=(keep_history == "1"))
    except registry.RegistryError as exc:
        return _redirect(f"/t/{source_id}/{table}", msg=f"error: {exc}")
    n = sum(r["deleted"].values())
    return _redirect("/", msg=(f"Deleted pipeline {source_id}.{table} — dropped "
                               f"{len(r['dropped'])} table(s), {n:,} control row(s)"))


# --------------------------------------------------------------------------- #
# waiting review
# --------------------------------------------------------------------------- #

@app.get("/waiting", response_class=HTMLResponse)
def waiting_list(request: Request, source: str = "", table: str = "", page: int = 1):
    return _render(request, "waiting_list.html",
                   panel=_wq_page("waiting", source, table, page),
                   source=source, table=table)


@app.get("/partials/waiting", response_class=HTMLResponse)
def partial_waiting(request: Request, source: str = "", table: str = "", page: int = 1):
    return _render(request, "_wq_panel.html",
                   panel=_wq_page("waiting", source, table, page))


@app.get("/waiting/{wbatch_id}", response_class=HTMLResponse)
def waiting_detail(request: Request, wbatch_id: str):
    wb = resolve.waiting_batch(wbatch_id)
    if wb is None:
        return _redirect("/waiting", msg="error: batch not found")
    w_rows = resolve.waiting_rows(wbatch_id)
    p_rows = resolve.production_rows_for(wbatch_id)
    return _render(request, "waiting_detail.html",
                   wb=wb, w_rows=w_rows, p_rows=p_rows, cols=_row_cols(w_rows or p_rows),
                   compare=resolve.waiting_compare(wbatch_id))


@app.post("/waiting/{wbatch_id}/approve")
def waiting_approve(request: Request, wbatch_id: str, by: str = Form(""),
                    source: str = Form(""), table: str = Form(""), page: int = Form(1)):
    def act():
        r = resolve.approve_waiting(wbatch_id, resolved_by=(by or None))
        return (f"Approved {wbatch_id[:8]} — replaced {r['production_rows_replaced']}, "
                f"inserted {r['production_rows_inserted']}")
    return _resolve_panel(request, "waiting", source, table, page, act)


@app.post("/waiting/{wbatch_id}/merge")
def waiting_merge(request: Request, wbatch_id: str, by: str = Form(""),
                  source: str = Form(""), table: str = Form(""), page: int = Form(1)):
    def act():
        r = resolve.merge_waiting(wbatch_id, resolved_by=(by or None))
        return (f"Merged {wbatch_id[:8]} — kept existing, added "
                f"{r['production_rows_inserted']} row(s)")
    return _resolve_panel(request, "waiting", source, table, page, act)


@app.post("/waiting/{wbatch_id}/reject")
def waiting_reject(request: Request, wbatch_id: str, by: str = Form(""),
                   source: str = Form(""), table: str = Form(""), page: int = Form(1)):
    def act():
        r = resolve.reject_waiting(wbatch_id, resolved_by=(by or None))
        return f"Rejected {wbatch_id[:8]} — dropped {r['rows_dropped']} row(s)"
    return _resolve_panel(request, "waiting", source, table, page, act)


@app.post("/waiting/reject-all")
def waiting_reject_all(request: Request, by: str = Form(""),
                       source: str = Form(""), table: str = Form(""), page: int = Form(1)):
    def act():
        r = resolve.reject_all_waiting(source or None, table or None, resolved_by=(by or None))
        return f"Rejected {r['rejected']} batch(es) — dropped {r['rows_dropped']} held row(s)"
    return _resolve_panel(request, "waiting", source, table, page, act)


# --------------------------------------------------------------------------- #
# quarantine review
# --------------------------------------------------------------------------- #

@app.get("/quarantine", response_class=HTMLResponse)
def quarantine_list(request: Request, source: str = "", table: str = "", page: int = 1):
    return _render(request, "quarantine_list.html",
                   panel=_wq_page("quarantine", source, table, page),
                   source=source, table=table)


@app.get("/partials/quarantine", response_class=HTMLResponse)
def partial_quarantine(request: Request, source: str = "", table: str = "", page: int = 1):
    return _render(request, "_wq_panel.html",
                   panel=_wq_page("quarantine", source, table, page))


@app.get("/quarantine/{qbatch_id}", response_class=HTMLResponse)
def quarantine_detail(request: Request, qbatch_id: str):
    qb = resolve.quarantine_batch(qbatch_id)
    if qb is None:
        return _redirect("/quarantine", msg="error: batch not found")
    rows = resolve.quarantine_rows(qbatch_id)
    return _render(request, "quarantine_detail.html", qb=qb, rows=rows, cols=_row_cols(rows))


@app.post("/quarantine/{qbatch_id}/reinject")
def quarantine_reinject(request: Request, qbatch_id: str, by: str = Form(""),
                        source: str = Form(""), table: str = Form(""), page: int = Form(1)):
    def act():
        r = resolve.reinject_quarantine(qbatch_id, resolved_by=(by or None))
        return (f"Re-injected {r['rows_reinjected']} row(s) as batch "
                f"{r['new_batch_id'][:8]} — run the transform")
    return _resolve_panel(request, "quarantine", source, table, page, act)


@app.post("/quarantine/{qbatch_id}/ignore")
def quarantine_ignore(request: Request, qbatch_id: str, by: str = Form(""),
                      source: str = Form(""), table: str = Form(""), page: int = Form(1)):
    def act():
        r = resolve.ignore_quarantine(qbatch_id, resolved_by=(by or None))
        return f"Ignored {qbatch_id[:8]} — dropped {r['rows_dropped']} row(s)"
    return _resolve_panel(request, "quarantine", source, table, page, act)
