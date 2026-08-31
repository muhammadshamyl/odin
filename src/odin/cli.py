"""Command-line interface for Odin (Slice 1).

    odin migrate
    odin tables
    odin sample <file> [-n N]
    odin onboard --name NAME (--from-file F | --columns c1,c2,...) [--format CSV|TXT]
                 --table T --load-type INCREMENTAL|FULL_SNAPSHOT
                 [--existence-column COL] [--recurrence ONE_TIME|RECURRING] [--owner WHO]
    odin run-extract   <source_id> <table> <file>
    odin run-transform <source_id> <table>
    odin ingest        <source_id> <table> <file>      # run-extract then run-transform
    odin runs [--source S] [--table T] [--limit N]
    odin waiting    list [--source S] [--table T]
    odin waiting    approve|reject  <wbatch_id> [--by WHO]
    odin quarantine list [--source S] [--table T]
    odin quarantine reinject|ignore <qbatch_id> [--by WHO]
    odin web [--host H] [--port P] [--reload]

Add --json before the subcommand for machine-readable output.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# --------------------------------------------------------------------------- #
# output helpers
# --------------------------------------------------------------------------- #

def _json(obj) -> None:
    print(json.dumps(obj, default=str, indent=2))


def _table(rows: list[dict], cols: list[str]) -> None:
    if not rows:
        print("(none)")
        return
    width = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    print("  ".join(c.ljust(width[c]) for c in cols))
    print("  ".join("-" * width[c] for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(width[c]) for c in cols))


def _result(res: dict, as_json: bool) -> None:
    if as_json:
        _json(res)
        return
    for k, v in res.items():
        print(f"{k}: {v}")


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

def cmd_migrate(args) -> None:
    from odin.migrate import migrate

    applied = migrate()
    print("applied: " + ", ".join(applied) if applied else "up to date")


def cmd_tables(args) -> None:
    from odin import registry

    rows = registry.list_tables()
    if args.json:
        _json(rows)
        return
    _table(rows, ["source_id", "table_name", "source_type", "load_type",
                  "existence_check_column", "load_recurrence", "status"])


def cmd_sample(args) -> None:
    from odin.connectors import file as fc

    p = Path(args.file)
    header = fc.read_header(p)
    rows = fc.sample_rows(p, args.n)
    if args.json:
        _json({"format": fc.detect_format(p), "header": header, "rows": rows})
        return
    print("format:", fc.detect_format(p))
    print("header:", ", ".join(header))
    _table(rows, header)


def cmd_onboard(args) -> None:
    from odin import registry
    from odin.connectors import file as fc

    if args.from_file:
        p = Path(args.from_file)
        columns = fc.read_header(p)
        fmt = args.format or fc.detect_format(p)
    else:
        columns = [c.strip() for c in args.columns.split(",") if c.strip()]
        fmt = args.format
        if not fmt:
            raise SystemExit("--format is required when using --columns")

    column_types = {}
    if getattr(args, "types", None):
        for pair in args.types.split(","):
            col, _, tok = pair.partition(":")
            if col.strip() and tok.strip():
                column_types[col.strip()] = tok.strip()
    required = {c.strip() for c in (getattr(args, "required", "") or "").split(",") if c.strip()}
    natural_key = [c.strip() for c in (getattr(args, "natural_key", "") or "").split(",") if c.strip()]

    cfg = registry.onboard_file_source(
        source_name=args.name,
        file_format=fmt,
        table_name=args.table,
        columns=columns,
        load_type=args.load_type,
        existence_check_column=args.existence_column,
        load_recurrence=args.recurrence,
        owner=args.owner,
        column_types=column_types,
        required=required,
        natural_key=natural_key,
    )
    out = {
        "source_id": cfg.source_id,
        "table_name": cfg.table_name,
        "staging_target": cfg.staging_target,
        "production_target": cfg.production_target,
        "quarantine_target": cfg.quarantine_target,
        "waiting_target": cfg.waiting_target,
        "load_type": cfg.load_type,
        "existence_check_column": cfg.existence_check_column,
        "natural_key": cfg.natural_key,
        "load_recurrence": cfg.load_recurrence,
    }
    if args.json:
        _json(out)
        return
    print(f"onboarded {cfg.source_id}.{cfg.table_name}")
    for k, v in out.items():
        print(f"  {k}: {v}")


def cmd_run_extract(args) -> None:
    from odin import extract

    _result(extract.run_extract(args.source_id, args.table, args.file), args.json)


def cmd_run_transform(args) -> None:
    from odin import transform

    _result(transform.run_transform(args.source_id, args.table), args.json)


def cmd_ingest(args) -> None:
    from odin import extract, transform

    ex = extract.run_extract(args.source_id, args.table, args.file)
    out = {"extract": ex}
    if ex["status"] == "loaded":
        out["transform"] = transform.run_transform(args.source_id, args.table)
    if args.json:
        _json(out)
        return
    for stage, res in out.items():
        print(f"[{stage}]")
        for k, v in res.items():
            print(f"  {k}: {v}")


def cmd_runs(args) -> None:
    from odin.db import connection

    clauses, params = [], []
    if args.source:
        clauses.append("source_id = %s")
        params.append(args.source)
    if args.table:
        clauses.append("table_name = %s")
        params.append(args.table)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, run_id, stage, source_id, table_name, status, rows_processed, "
            "rows_to_production, rows_to_waiting, rows_quarantined, started_at, error_message "
            f"FROM run_log {where} ORDER BY id DESC LIMIT %s",
            (*params, args.limit),
        )
        rows = cur.fetchall()
    if args.json:
        _json(rows)
        return
    _table(rows, ["id", "stage", "source_id", "table_name", "status", "rows_processed",
                  "rows_to_production", "rows_to_waiting", "rows_quarantined"])


def cmd_waiting(args) -> None:
    from odin import resolve

    if args.action == "list":
        rows = resolve.pending_waiting(args.source, args.table)
        if args.json:
            _json(rows)
            return
        _table(rows, ["wbatch_id", "source_id", "table_name", "existence_value",
                      "row_count", "status", "created_at"])
    elif args.action == "approve":
        _result(resolve.approve_waiting(args.wbatch_id, resolved_by=args.by), args.json)
    elif args.action == "reject":
        _result(resolve.reject_waiting(args.wbatch_id, resolved_by=args.by), args.json)


def cmd_quarantine(args) -> None:
    from odin import resolve

    if args.action == "list":
        rows = resolve.open_quarantine(args.source, args.table)
        if args.json:
            _json(rows)
            return
        _table(rows, ["qbatch_id", "source_id", "table_name", "reason",
                      "row_count", "resolution_status", "created_at"])
    elif args.action == "reinject":
        _result(resolve.reinject_quarantine(args.qbatch_id, resolved_by=args.by), args.json)
    elif args.action == "ignore":
        _result(resolve.ignore_quarantine(args.qbatch_id, resolved_by=args.by), args.json)


def cmd_web(args) -> None:
    import uvicorn

    print(f"Odin web UI on http://{args.host}:{args.port}")
    uvicorn.run("odin.web.app:app", host=args.host, port=args.port, reload=args.reload)


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="odin", description="Odin pipeline CLI (Slice 1)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="apply pending SQL migrations")
    sub.add_parser("tables", help="list registered tables")

    sp = sub.add_parser("sample", help="preview a file's header + rows (no DB)")
    sp.add_argument("file")
    sp.add_argument("-n", type=int, default=20, help="rows to show (default 20)")

    sp = sub.add_parser("onboard", help="register a file source + create its tables")
    sp.add_argument("--name", required=True, help="source name (slugged to source_id)")
    sp.add_argument("--table", required=True)
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--from-file", dest="from_file", help="read the column list from this file's header")
    g.add_argument("--columns", help="comma-separated column list")
    sp.add_argument("--format", choices=["CSV", "TXT"], help="required with --columns")
    sp.add_argument("--load-type", dest="load_type", required=True,
                    choices=["INCREMENTAL", "FULL_SNAPSHOT"])
    sp.add_argument("--existence-column", dest="existence_column",
                    help="single date column checked against production (INCREMENTAL)")
    sp.add_argument("--natural-key", dest="natural_key",
                    help="composite INCREMENTAL key, comma-separated columns "
                         "(overrides --existence-column)")
    sp.add_argument("--recurrence", default="ONE_TIME", choices=["ONE_TIME", "RECURRING"])
    sp.add_argument("--owner")
    sp.add_argument("--types", help="production column types, e.g. 'amount:nonneg_int,sale_date:date' (default: all text)")
    sp.add_argument("--required", help="comma-separated columns that must be non-empty")

    sp = sub.add_parser("run-extract", help="land a file + bulk-load it to staging")
    sp.add_argument("source_id")
    sp.add_argument("table")
    sp.add_argument("file")

    sp = sub.add_parser("run-transform", help="run the staging -> production transform")
    sp.add_argument("source_id")
    sp.add_argument("table")

    sp = sub.add_parser("ingest", help="run-extract then run-transform")
    sp.add_argument("source_id")
    sp.add_argument("table")
    sp.add_argument("file")

    sp = sub.add_parser("runs", help="recent run_log rows")
    sp.add_argument("--source")
    sp.add_argument("--table")
    sp.add_argument("--limit", type=int, default=20)

    wp = sub.add_parser("waiting", help="waiting-pipeline review")
    ws = wp.add_subparsers(dest="action", required=True)
    wl = ws.add_parser("list")
    wl.add_argument("--source")
    wl.add_argument("--table")
    for name in ("approve", "reject"):
        wa = ws.add_parser(name)
        wa.add_argument("wbatch_id")
        wa.add_argument("--by", help="resolved_by")

    qp = sub.add_parser("quarantine", help="quarantine review")
    qs = qp.add_subparsers(dest="action", required=True)
    ql = qs.add_parser("list")
    ql.add_argument("--source")
    ql.add_argument("--table")
    for name in ("reinject", "ignore"):
        qa = qs.add_parser(name)
        qa.add_argument("qbatch_id")
        qa.add_argument("--by", help="resolved_by")

    sp = sub.add_parser("web", help="run the web UI")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8000)
    sp.add_argument("--reload", action="store_true", help="auto-reload on code change")

    return p


_DISPATCH = {
    "migrate": cmd_migrate,
    "tables": cmd_tables,
    "sample": cmd_sample,
    "onboard": cmd_onboard,
    "run-extract": cmd_run_extract,
    "run-transform": cmd_run_transform,
    "ingest": cmd_ingest,
    "runs": cmd_runs,
    "waiting": cmd_waiting,
    "quarantine": cmd_quarantine,
    "web": cmd_web,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _DISPATCH[args.command](args)
    except Exception as exc:  # noqa: BLE001 - CLI boundary: report and exit non-zero
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
