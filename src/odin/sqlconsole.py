"""SQL Console (web ``/sql``) — run ad-hoc SQL against the Odin database with
guard rails.

Design (see the approved proposal):

* **Read-only by default.** Every statement is classified from its leading
  keyword. ``read`` runs in a read-only transaction that is always rolled back.
  ``write`` / ``ddl`` need Write mode and run inside a transaction that is held
  open **pending** — the caller sees the row counts, then Commits or Discards.
  ``admin`` (everything else — ``GRANT``, ``VACUUM``, ``SET`` …) runs in
  autocommit with no undo; allowed for now, a future permission control will
  gate it. ``BEGIN`` / ``COMMIT`` / ``ROLLBACK`` typed by hand are refused —
  the console owns the transaction.
* **statement_timeout** on every connection.
* **Row cap** — a returning statement renders at most ``sql_row_cap`` rows; the
  result carries a flag so the UI can offer "Load all" (up to the hard cap).
* **Dedicated connections**, never the pool — a runaway query here can't wedge
  pipeline work.
* **Audit** — every executed statement is written to ``sql_console_log`` on a
  side connection, including ones that were Discarded.

State is process-local (single-user local tool): one pending transaction at a
time, guarded by a lock, reaped after :data:`_PENDING_TTL` seconds.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from typing import Any

import psycopg
from psycopg.rows import dict_row

from odin import registry
from odin.config import settings
from odin.db import log_connection

# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #

_READ = {"select", "table", "values", "show", "with"}
_WRITE = {"insert", "update", "delete", "merge", "truncate", "copy"}
_DDL = {"create", "drop", "alter", "rename", "comment", "import", "reindex"}
_REFUSE = {"begin", "start", "commit", "end", "rollback", "savepoint", "release", "abort"}

_MAIN_VERB_RE = re.compile(r"\b(select|insert|update|delete|merge)\b", re.I)


def _strip_leading_noise(s: str) -> str:
    """Drop leading whitespace + SQL comments so we can read the first keyword."""
    prev = None
    while s != prev:
        prev = s
        s = s.lstrip()
        if s.startswith("--"):
            s = s.split("\n", 1)[1] if "\n" in s else ""
        elif s.startswith("/*"):
            s = s.split("*/", 1)[1] if "*/" in s else ""
    return s


def classify(statement: str) -> str:
    """``'read' | 'write' | 'ddl' | 'admin' | 'refuse'`` for one statement."""
    s = _strip_leading_noise(statement)
    if not s:
        return "read"
    m = re.match(r"[a-zA-Z_]+", s)
    if not m:
        return "admin"
    kw = m.group(0).lower()

    if kw == "explain":
        rest = s[m.end():]
        # EXPLAIN ANALYZE actually runs the statement — classify by the inner verb.
        rest = re.sub(r"^\s*(\([^)]*\)|analyze|verbose|costs|buffers|format\s+\w+|,|\s)+",
                      " ", rest, flags=re.I)
        inner = re.match(r"\s*([a-zA-Z_]+)", rest)
        if inner and inner.group(1).lower() in _WRITE:
            return "write"
        if inner and inner.group(1).lower() in _DDL:
            return "ddl"
        return "read"

    if kw in _REFUSE:
        return "refuse"
    if kw == "with":
        verb = _MAIN_VERB_RE.search(s)
        v = verb.group(1).lower() if verb else "select"
        return "write" if v in _WRITE else "read"
    if kw in _READ:
        return "read"
    if kw in _WRITE:
        return "write"
    if kw in _DDL:
        return "ddl"
    return "admin"


# --------------------------------------------------------------------------- #
# statement splitting  (quote / dollar-quote / comment aware)
# --------------------------------------------------------------------------- #

def split_statements(text: str) -> list[str]:
    out: list[str] = []
    buf: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        two = text[i:i + 2]
        if two == "--":
            j = text.find("\n", i)
            j = n if j == -1 else j + 1
            buf.append(text[i:j])
            i = j
            continue
        if two == "/*":
            j = text.find("*/", i + 2)
            j = n if j == -1 else j + 2
            buf.append(text[i:j])
            i = j
            continue
        if c in "'\"":
            j = i + 1
            while j < n:
                if text[j] == c:
                    if j + 1 < n and text[j + 1] == c:  # doubled quote escape
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            buf.append(text[i:j])
            i = j
            continue
        if c == "$":
            m = re.match(r"\$[A-Za-z_]*\$", text[i:])
            if m:
                tag = m.group(0)
                j = text.find(tag, i + len(tag))
                j = n if j == -1 else j + len(tag)
                buf.append(text[i:j])
                i = j
                continue
        if c == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


_DROP_RE = re.compile(
    r"\bdrop\s+(?:table|materialized\s+view|view|index|sequence|schema)\s+"
    r"(?:if\s+exists\s+)?([\w.\"]+)", re.I)
_TRUNC_RE = re.compile(r"\btruncate\s+(?:table\s+)?([\w.\",\s]+?)"
                       r"(?:\s+(?:restart|continue|cascade|restrict)\b|$)", re.I)


def _confirm_targets(statements: list[str]) -> list[str]:
    """Object names a DROP / TRUNCATE in the batch will hit — the pending bar
    makes the user type these before Commit."""
    names: list[str] = []
    for s in statements:
        for m in _DROP_RE.finditer(s):
            names.append(m.group(1).strip().strip('"'))
        for m in _TRUNC_RE.finditer(s):
            for part in m.group(1).split(","):
                p = part.strip().strip('"')
                if p:
                    names.append(p)
    seen: list[str] = []
    for x in names:
        if x not in seen:
            seen.append(x)
    return seen


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #

_LOCK = threading.RLock()
_PENDING: dict[str, Any] | None = None
_PENDING_TTL = 300.0
_HISTORY: list[dict] = []
_HISTORY_MAX = 40


def _oneline(s: str, limit: int = 160) -> str:
    s = " ".join(s.split())
    return s if len(s) <= limit else s[:limit - 1] + "…"


def _connect(*, autocommit: bool) -> psycopg.Connection:
    conn = psycopg.connect(settings.database_url, autocommit=autocommit,
                           row_factory=dict_row)
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('statement_timeout', %s, false)",
                    (str(settings.sql_timeout_seconds * 1000),))
    if autocommit:
        return conn
    conn.commit()  # settle the SET; the console's own tx starts on next execute
    return conn


def _audit(text: str, klass: str, read_only: bool, *, row_count: int | None,
           elapsed_ms: float | None, status: str, error: str | None,
           by_label: str | None) -> None:
    try:
        with log_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO sql_console_log
                     (statement_text, statement_class, read_only, row_count,
                      elapsed_ms, status, error_message, by_label)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (text, klass if klass in ("read", "write", "ddl", "admin") else "unknown",
                 read_only, row_count, elapsed_ms, status, error, by_label or None),
            )
    except Exception:  # noqa: BLE001 — the audit log must never break a query
        pass


def _grid_from_cursor(cur: psycopg.Cursor, cap: int) -> dict:
    rows = cur.fetchmany(cap + 1)
    truncated = len(rows) > cap
    if truncated:
        rows = rows[:cap]
    cols = [d.name for d in (cur.description or [])]
    return {
        "columns": cols,
        "rows": [[_cell(r.get(c)) for c in cols] for r in rows],
        "row_count": len(rows),
        "truncated": truncated,
    }


def _cell(v: Any) -> str:
    if v is None:
        return ""
    return str(v)


def _result(**kw: Any) -> dict:
    base = {
        "error": None, "need_write": False, "grid": None, "messages": [],
        "pending": None, "committed": False, "discarded": False, "no_undo": False,
        "elapsed_ms": None, "history": list(_HISTORY),
    }
    base.update(kw)
    return base


def _remember(text: str, klass: str, status: str, detail: str) -> None:
    _HISTORY.insert(0, {
        "sql": _oneline(text, 200), "klass": klass, "status": status,
        "detail": detail, "at": time.time(), "ts": time.strftime("%H:%M:%S"),
    })
    del _HISTORY[_HISTORY_MAX:]


def _reap() -> None:
    global _PENDING
    if _PENDING and time.time() - _PENDING["opened_at"] > _PENDING_TTL:
        try:
            _PENDING["conn"].rollback()
            _PENDING["conn"].close()
        except Exception:  # noqa: BLE001
            pass
        _PENDING = None


def _discard_locked(reason: str = "superseded") -> None:
    global _PENDING
    if _PENDING:
        try:
            _PENDING["conn"].rollback()
            _PENDING["conn"].close()
        except Exception:  # noqa: BLE001
            pass
        for s, c in zip(_PENDING["stmts"], _PENDING["classes"]):
            _audit(s, c, _PENDING["read_only"], row_count=None, elapsed_ms=None,
                   status="discarded", error=reason, by_label=_PENDING["by_label"])
        _PENDING = None


def run(text: str, *, read_only: bool, by_label: str = "", full: bool = False) -> dict:
    """Entry point for ``POST /sql/run``."""
    with _LOCK:
        _reap()
        statements = split_statements(text)
        if not statements:
            return _result(error="Nothing to run.")

        classes = [classify(s) for s in statements]
        if "refuse" in classes:
            return _result(error="BEGIN / COMMIT / ROLLBACK / SAVEPOINT are not "
                                 "allowed — the console manages the transaction. "
                                 "Use Write mode, then Commit or Discard.")

        if "write" in classes or "ddl" in classes:
            batch = "write"
        elif "admin" in classes:
            batch = "admin"
        else:
            batch = "read"

        if batch == "write" and read_only:
            names = ", ".join(sorted({c for c in classes if c in ("write", "ddl")}))
            return _result(
                need_write=True,
                error=f"This batch contains {names} statements. Turn on Write mode "
                      f"(top-right) and run again — you'll get a Commit / Discard step.")

        cap = settings.sql_row_hard_cap if full else settings.sql_row_cap
        if batch == "read":
            return _run_read(statements, classes, cap, by_label)
        if batch == "admin":
            return _run_admin(statements, classes, cap, by_label)
        return _begin_pending(statements, classes, cap, read_only, by_label)


def _run_read(statements, classes, cap, by_label) -> dict:
    conn = _connect(autocommit=False)
    grid, messages = None, []
    t0 = time.time()
    try:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            for s, c in zip(statements, classes):
                st = time.time()
                cur.execute(s)
                ms = round((time.time() - st) * 1000, 2)
                if cur.description:
                    grid = _grid_from_cursor(cur, cap)
                    grid["sql"] = s
                    grid["elapsed_ms"] = ms
                    _audit(s, c, True, row_count=grid["row_count"], elapsed_ms=ms,
                           status="ok", error=None, by_label=by_label)
                    _remember(s, c, "ok", f"{grid['row_count']} row(s)")
                else:
                    messages.append({"sql": _oneline(s), "klass": c,
                                     "rowcount": cur.rowcount, "elapsed_ms": ms})
                    _audit(s, c, True, row_count=cur.rowcount, elapsed_ms=ms,
                           status="ok", error=None, by_label=by_label)
        conn.rollback()
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        _audit(statements[-1], "read", True, row_count=None, elapsed_ms=None,
               status="error", error=str(exc), by_label=by_label)
        _remember(text_of(statements), "read", "error", str(exc))
        return _result(error=str(exc), messages=messages)
    finally:
        conn.close()
    return _result(grid=grid, messages=messages,
                   elapsed_ms=round((time.time() - t0) * 1000, 2))


def _run_admin(statements, classes, cap, by_label) -> dict:
    conn = _connect(autocommit=True)
    grid, messages = None, []
    t0 = time.time()
    try:
        with conn.cursor() as cur:
            for s, c in zip(statements, classes):
                st = time.time()
                cur.execute(s)
                ms = round((time.time() - st) * 1000, 2)
                if cur.description:
                    grid = _grid_from_cursor(cur, cap)
                    grid["sql"] = s
                    grid["elapsed_ms"] = ms
                    rc = grid["row_count"]
                else:
                    messages.append({"sql": _oneline(s), "klass": c,
                                     "rowcount": cur.rowcount, "elapsed_ms": ms})
                    rc = cur.rowcount
                _audit(s, c, False, row_count=rc, elapsed_ms=ms, status="ok",
                       error=None, by_label=by_label)
                _remember(s, c, "ok", "ran (no undo)")
    except Exception as exc:  # noqa: BLE001
        _audit(statements[-1], "admin", False, row_count=None, elapsed_ms=None,
               status="error", error=str(exc), by_label=by_label)
        _remember(text_of(statements), "admin", "error", str(exc))
        return _result(error=str(exc), messages=messages, no_undo=True)
    finally:
        conn.close()
    return _result(grid=grid, messages=messages, no_undo=True,
                   elapsed_ms=round((time.time() - t0) * 1000, 2))


def _begin_pending(statements, classes, cap, read_only, by_label) -> dict:
    global _PENDING
    _discard_locked()
    conn = _connect(autocommit=False)
    grid, messages, affected = None, [], 0
    t0 = time.time()
    cur = conn.cursor()
    try:
        for s, c in zip(statements, classes):
            st = time.time()
            cur.execute(s)
            ms = round((time.time() - st) * 1000, 2)
            if cur.description:
                grid = _grid_from_cursor(cur, cap)
                grid["sql"] = s
                grid["elapsed_ms"] = ms
            else:
                rc = max(cur.rowcount, 0)
                affected += rc
                messages.append({"sql": _oneline(s), "klass": c,
                                 "rowcount": cur.rowcount, "elapsed_ms": ms})
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        conn.close()
        for s, c in zip(statements, classes):
            _audit(s, c, read_only, row_count=None, elapsed_ms=None,
                   status="error", error=str(exc), by_label=by_label)
        _remember(text_of(statements), "write", "error", str(exc))
        return _result(error=str(exc), messages=messages)

    token = uuid.uuid4().hex
    _PENDING = {
        "token": token, "conn": conn, "cur": cur, "stmts": statements,
        "classes": classes, "messages": messages, "grid": grid,
        "affected": affected, "opened_at": time.time(), "read_only": read_only,
        "by_label": by_label, "confirm": _confirm_targets(statements),
    }
    return _result(pending=pending_view(),
                   elapsed_ms=round((time.time() - t0) * 1000, 2))


def pending_view() -> dict | None:
    if not _PENDING:
        return None
    return {
        "token": _PENDING["token"],
        "count": len(_PENDING["stmts"]),
        "affected": _PENDING["affected"],
        "messages": _PENDING["messages"],
        "grid": _PENDING["grid"],
        "confirm": _PENDING["confirm"],
        "statements": [_oneline(s, 220) for s in _PENDING["stmts"]],
    }


def commit(token: str, confirm: str = "") -> dict:
    with _LOCK:
        global _PENDING
        if not _PENDING or _PENDING["token"] != token:
            return _result(error="Nothing staged to commit — it may have expired. "
                                 "Run the statement again.")
        need = _PENDING["confirm"]
        if need and confirm.strip() != ", ".join(need) and confirm.strip() not in need:
            return _result(error=f"Type {', '.join(need)} to confirm the drop.",
                           pending=pending_view())
        p = _PENDING
        try:
            p["conn"].commit()
        except Exception as exc:  # noqa: BLE001
            p["conn"].rollback()
            p["conn"].close()
            _PENDING = None
            return _result(error=f"Commit failed: {exc}")
        p["conn"].close()
        for s, c, in zip(p["stmts"], p["classes"]):
            _audit(s, c, p["read_only"], row_count=None,
                   elapsed_ms=None, status="ok", error=None, by_label=p["by_label"])
        _remember(text_of(p["stmts"]), "write", "ok",
                  f"committed · {p['affected']:,} row(s) changed")
        _PENDING = None
        return _result(committed=True, messages=p["messages"], grid=p["grid"])


def discard(token: str) -> dict:
    with _LOCK:
        global _PENDING
        if not _PENDING or _PENDING["token"] != token:
            return _result(discarded=True)
        p = _PENDING
        try:
            p["conn"].rollback()
        finally:
            p["conn"].close()
        for s, c in zip(p["stmts"], p["classes"]):
            _audit(s, c, p["read_only"], row_count=None, elapsed_ms=None,
                   status="discarded", error=None, by_label=p["by_label"])
        _remember(text_of(p["stmts"]), "write", "discarded", "rolled back")
        _PENDING = None
        return _result(discarded=True, messages=p["messages"])


def text_of(statements: list[str]) -> str:
    return _oneline("; ".join(statements), 200)


def history() -> list[dict]:
    return list(_HISTORY)


# --------------------------------------------------------------------------- #
# schema sidebar + autocomplete + snippets
# --------------------------------------------------------------------------- #

_META_COLS = ["batch_id", "load_date", "load_timestamp", "source_system", "restated"]
_STAGING_META = ["staging_record_id", "load_date", "load_timestamp",
                 "source_file_id", "batch_id", "source_system"]

_PLATFORM = [
    ("run_log", ["id", "run_id", "stage", "source_id", "table_name", "status",
                 "rows_processed", "rows_to_production", "rows_to_waiting",
                 "rows_quarantined", "error_message", "started_at", "ended_at",
                 "triggered_by"]),
    ("staging_file_control", ["file_id", "source_id", "table_name", "file_path",
                              "file_format", "landing_timestamp", "processing_status",
                              "processed_timestamp", "row_count", "error_message"]),
    ("registry_sources", ["source_id", "source_name", "source_type", "owner", "status"]),
    ("registry_tables", ["source_id", "table_name", "staging_target",
                         "production_target", "load_type", "existence_check_column",
                         "load_recurrence", "status"]),
    ("registry_columns", ["source_id", "table_name", "column_name", "column_order",
                          "target_data_type", "is_nullable"]),
    ("registry_change_log", ["source_id", "table_name", "column_name",
                             "change_type", "old_value", "new_value",
                             "changed_timestamp"]),
    ("quarantine_batch_log", ["qbatch_id", "run_id", "source_id", "table_name",
                              "reason", "row_count", "created_at", "resolution_status"]),
    ("waiting_batch_log", ["wbatch_id", "run_id", "source_id", "table_name",
                           "existence_value", "row_count", "status", "created_at"]),
    ("sql_console_log", ["id", "ran_at", "statement_text", "statement_class",
                         "read_only", "row_count", "elapsed_ms", "status",
                         "error_message", "by_label"]),
    ("schema_migrations", ["filename", "applied_at"]),
]


def _safe_count(qualified: str) -> int | None:
    try:
        with log_connection() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT count(*) AS n FROM {_ident(qualified)}")
            return cur.fetchone()["n"]
    except Exception:  # noqa: BLE001 — missing table, etc.
        return None


def _ident(qualified: str) -> str:
    parts = qualified.split(".")
    return ".".join('"' + p.replace('"', '""') + '"' for p in parts)


def schema_tree() -> dict:
    sources = []
    for t in registry.list_tables():
        sid, tname = t["source_id"], t["table_name"]
        try:
            cfg = registry.get_table(sid, tname)
            cols = registry.get_columns(sid, tname)
        except registry.RegistryError:
            continue
        q = cfg.quarantine_target
        w = cfg.waiting_target
        sources.append({
            "source_id": sid, "table_name": tname, "load_type": t["load_type"],
            "tables": [
                {"name": cfg.staging_target, "kind": "staging",
                 "count": _safe_count(cfg.staging_target), "columns": cols + _STAGING_META},
                {"name": cfg.production_target, "kind": "production",
                 "count": _safe_count(cfg.production_target), "columns": cols + _META_COLS},
                {"name": q, "kind": "quarantine", "count": _safe_count(q),
                 "columns": cols + _STAGING_META + ["qbatch_id"]},
                {"name": w, "kind": "waiting", "count": _safe_count(w),
                 "columns": cols + _META_COLS + ["wbatch_id"]},
            ],
        })
    platform = [{"name": n, "count": _safe_count(n), "columns": c} for n, c in _PLATFORM]
    return {"sources": sources, "platform": platform}


def hint_tables(tree: dict) -> dict:
    """`{table_name: [col, ...]}` for CodeMirror's sql-hint addon."""
    out: dict[str, list[str]] = {}
    for src in tree["sources"]:
        for tbl in src["tables"]:
            out[tbl["name"]] = list(tbl["columns"])
    for p in tree["platform"]:
        out[p["name"]] = list(p["columns"])
    return out


def snippets(tree: dict) -> list[dict]:
    out: list[dict] = []
    if tree["sources"]:
        lines = []
        for src in tree["sources"]:
            names = {t["kind"]: t["name"] for t in src["tables"]}
            lines.append(
                f"SELECT '{src['source_id']}.{src['table_name']}' AS pipeline,\n"
                f"       (SELECT count(*) FROM {names['staging']})            AS staging,\n"
                f"       (SELECT count(*) FROM {_ident(names['production'])})  AS production,\n"
                f"       (SELECT count(*) FROM {_ident(names['quarantine'])})  AS quarantine,\n"
                f"       (SELECT count(*) FROM {_ident(names['waiting'])})     AS waiting")
        out.append({"label": "Row counts — every layer",
                    "sql": "\nUNION ALL\n".join(lines) + ";"})
    out += [
        {"label": "Last 20 runs",
         "sql": "SELECT run_id, stage, status, rows_processed, rows_to_production,\n"
                "       rows_to_waiting, rows_quarantined, error_message, started_at\n"
                "FROM run_log\nORDER BY id DESC\nLIMIT 20;"},
        {"label": "Open quarantine — reasons",
         "sql": "SELECT source_id, table_name, reason,\n"
                "       sum(row_count) AS rows, count(*) AS batches\n"
                "FROM quarantine_batch_log\nWHERE resolution_status = 'open'\n"
                "GROUP BY 1, 2, 3\nORDER BY rows DESC;"},
        {"label": "Pending waiting collisions",
         "sql": "SELECT source_id, table_name, existence_value, row_count, created_at\n"
                "FROM waiting_batch_log\nWHERE status = 'pending'\n"
                "ORDER BY created_at;"},
        {"label": "Production partition sizes",
         "sql": "SELECT c.relname AS partition,\n"
                "       pg_size_pretty(pg_total_relation_size(c.oid)) AS size,\n"
                "       c.reltuples::bigint AS est_rows\n"
                "FROM pg_inherits i\n"
                "JOIN pg_class c ON c.oid = i.inhrelid\n"
                "JOIN pg_class p ON p.oid = i.inhparent\n"
                "WHERE p.relname LIKE 'production\\_%'\n"
                "ORDER BY 1;"},
        {"label": "Orphaned file-control rows",
         "sql": "SELECT f.file_id, f.source_id, f.table_name, f.processing_status,\n"
                "       f.row_count, f.landing_timestamp\n"
                "FROM staging_file_control f\n"
                "LEFT JOIN run_log r\n"
                "  ON r.source_id = f.source_id AND r.table_name = f.table_name\n"
                " AND r.stage = 'EXTRACT'\n"
                "WHERE r.id IS NULL\nORDER BY f.landing_timestamp DESC;"},
        {"label": "Recent console activity",
         "sql": "SELECT ran_at, statement_class, status, row_count, elapsed_ms,\n"
                "       left(statement_text, 90) AS statement\n"
                "FROM sql_console_log\nORDER BY id DESC\nLIMIT 30;"},
    ]
    return out
