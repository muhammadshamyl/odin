"""Schema Registry access (Module 3) — the base-build subset for Slice 1.

`onboard_file_source` is the non-UI half of the onboarding wizard: it takes an
already-approved column list + load-type choice and writes the registry rows,
then creates the staging / production / quarantine / waiting tables from them.
"""

from __future__ import annotations

from dataclasses import dataclass

from psycopg import sql
from psycopg.types.json import Json

from odin import casts, ddl
from odin.db import connection
from odin.ddl import PRODUCTION_META, STAGING_META
from odin.locks import lock
from odin.naming import (
    bare,
    production_target,
    qname,
    quarantine_target,
    slug,
    staging_target,
    waiting_target,
)

_LOAD_TYPES = ("INCREMENTAL", "FULL_SNAPSHOT")

# column names Odin adds to staging / production — a source column may not reuse them
_RESERVED_COLS = frozenset(
    n.lower() for n, _ in (*STAGING_META, *PRODUCTION_META)
) | {"nk"}


@dataclass(frozen=True)
class TableConfig:
    source_id: str
    table_name: str
    staging_target: str
    production_target: str
    load_type: str
    existence_check_column: str | None
    load_recurrence: str
    status: str
    natural_key: str | None = None          # comma-separated key columns, in order

    @property
    def quarantine_target(self) -> str:
        return quarantine_target(self.table_name)

    @property
    def waiting_target(self) -> str:
        return waiting_target(self.table_name)

    @property
    def natural_key_columns(self) -> list[str]:
        return [c for c in (self.natural_key or "").split(",") if c]


class RegistryError(RuntimeError):
    pass


def _assert_targets_absent(cur, targets: list[str]) -> None:
    """SQL pre-flight for a brand-new table: none of the physical tables we are
    about to CREATE may already exist. `targets` is a list of ``schema.table``
    strings. Raises :class:`RegistryError` naming every clash. Uses
    ``to_regclass`` so a missing table reads back as NULL rather than erroring."""
    clashing: list[str] = []
    for qualified in targets:
        cur.execute("SELECT to_regclass(%s) AS oid", (qualified,))
        if cur.fetchone()["oid"] is not None:
            clashing.append(qualified)
    if clashing:
        raise RegistryError(
            "cannot create — table(s) already exist: "
            + ", ".join(clashing)
            + ". Drop them or choose a different table name."
        )


def onboard_file_source(
    *,
    source_name: str,
    file_format: str,               # 'CSV' | 'TXT'
    table_name: str,
    columns: list[str],
    load_type: str,
    existence_check_column: str | None = None,
    load_recurrence: str = "ONE_TIME",
    owner: str | None = None,
    column_types: dict[str, str] | None = None,
    required: set[str] | None = None,
    natural_key: list[str] | None = None,
) -> TableConfig:
    return _onboard(
        source_name=source_name, table_name=table_name,
        src_type=("FILE_CSV" if file_format == "CSV" else "FILE_TXT"),
        columns=columns, load_type=load_type,
        existence_check_column=existence_check_column, load_recurrence=load_recurrence,
        owner=owner, column_types=column_types, required=required, natural_key=natural_key,
    )


def onboard_rdbms_source(
    *,
    source_name: str,
    table_name: str,
    columns: list[str],
    load_type: str,
    connection_id: str,
    source_schema: str,
    source_table: str,
    tenure_column: str | None = None,
    static_filter: dict | None = None,
    existence_check_column: str | None = None,
    load_recurrence: str = "ONE_TIME",
    owner: str | None = None,
    column_types: dict[str, str] | None = None,
    required: set[str] | None = None,
    natural_key: list[str] | None = None,
) -> TableConfig:
    """Register an RDBMS-backed pipeline. Same tables + registry rows as the file
    path, plus a ``registry_rdbms_source`` row holding the connection, source
    schema/table, tenure column and static filter."""
    return _onboard(
        source_name=source_name, table_name=table_name, src_type="RDBMS",
        columns=columns, load_type=load_type,
        existence_check_column=existence_check_column, load_recurrence=load_recurrence,
        owner=owner, column_types=column_types, required=required, natural_key=natural_key,
        rdbms_sidecar={
            "connection_id": connection_id, "source_schema": source_schema,
            "source_table": source_table, "tenure_column": tenure_column,
            "static_filter": static_filter,
        },
    )


def _onboard(
    *,
    source_name: str,
    table_name: str,
    src_type: str,
    columns: list[str],
    load_type: str,
    existence_check_column: str | None = None,
    load_recurrence: str = "ONE_TIME",
    owner: str | None = None,
    column_types: dict[str, str] | None = None,
    required: set[str] | None = None,
    natural_key: list[str] | None = None,
    rdbms_sidecar: dict | None = None,
) -> TableConfig:
    source_id = slug(source_name)
    load_type = load_type.upper()
    column_types = column_types or {}
    required = set(required or ())
    natural_key = [c.strip() for c in (natural_key or []) if c.strip()]

    if load_type not in _LOAD_TYPES:
        raise RegistryError(f"load_type must be one of {_LOAD_TYPES}, got {load_type!r}")
    if len(columns) != len(set(columns)):
        raise RegistryError("duplicate column names in header")
    clash = [c for c in columns if c.lower() in _RESERVED_COLS]
    if clash:
        raise RegistryError(
            "column name(s) " + ", ".join(repr(c) for c in clash)
            + " collide with a reserved metadata column "
            + f"({', '.join(sorted(_RESERVED_COLS))}) — rename or exclude them at the source"
        )

    _validate_types(column_types, required, columns)

    if load_type == "INCREMENTAL":
        if natural_key:
            missing = [c for c in natural_key if c not in columns]
            if missing:
                raise RegistryError(f"natural_key column(s) not in the header: {missing}")
            bad = [c for c in natural_key
                   if casts.normalize(column_types.get(c)) in ddl.NK_FORBIDDEN_TOKENS]
            if bad:
                raise RegistryError(
                    f"natural_key cannot use numeric-typed column(s): {bad} — "
                    "pick text / int / date / timestamp / boolean columns"
                )
            existence_check_column = None          # the composite key supersedes it
        elif not existence_check_column:
            raise RegistryError(
                "INCREMENTAL load needs a natural_key or an existence_check_column"
            )
        elif existence_check_column not in columns:
            raise RegistryError(
                f"existence_check_column {existence_check_column!r} not in columns"
            )
    else:  # FULL_SNAPSHOT does no row-identity routing
        existence_check_column = None
        natural_key = []

    nk_flag = bool(natural_key)
    nk_str = ",".join(natural_key) or None

    stg = staging_target(table_name)
    prd = production_target(table_name)
    qtbl = quarantine_target(table_name)
    wtbl = waiting_target(table_name)

    with connection() as conn, conn.transaction():
        with conn.cursor() as cur:
            # A genuinely new (source_id, table_name) must not collide with any
            # existing physical table — checked in SQL before we create anything.
            cur.execute(
                "SELECT 1 FROM registry_tables WHERE source_id = %s AND table_name = %s",
                (source_id, table_name),
            )
            if cur.fetchone() is None:
                _assert_targets_absent(cur, [stg, prd, qtbl, wtbl])

            cur.execute(
                """INSERT INTO registry_sources (source_id, source_name, source_type, owner)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (source_id) DO UPDATE
                     SET source_name = EXCLUDED.source_name, last_updated = now()""",
                (source_id, source_name, src_type, owner),
            )
            cur.execute(
                """INSERT INTO registry_tables
                     (source_id, table_name, staging_target, production_target,
                      load_type, existence_check_column, natural_key,
                      extraction_strategy, load_recurrence)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, 'FULL', %s)
                   ON CONFLICT (source_id, table_name) DO UPDATE
                     SET load_type = EXCLUDED.load_type,
                         existence_check_column = EXCLUDED.existence_check_column,
                         natural_key = EXCLUDED.natural_key,
                         load_recurrence = EXCLUDED.load_recurrence,
                         last_updated = now()""",
                (source_id, table_name, stg, prd, load_type,
                 existence_check_column, nk_str, load_recurrence),
            )
            cur.execute(
                "DELETE FROM registry_columns WHERE source_id = %s AND table_name = %s",
                (source_id, table_name),
            )
            for i, name in enumerate(columns):
                cur.execute(
                    """INSERT INTO registry_columns
                         (source_id, table_name, column_name, column_order,
                          source_data_type, target_data_type, is_nullable)
                       VALUES (%s, %s, %s, %s, 'text', %s, %s)""",
                    (source_id, table_name, name, i,
                     casts.normalize(column_types.get(name)), name not in required),
                )
            cur.execute(
                """INSERT INTO registry_change_log
                     (source_id, table_name, change_type, new_value, schema_version)
                   VALUES (%s, %s, 'REGISTER', %s, 1)""",
                (source_id, table_name, f"{len(columns)} columns"),
            )

            cur.execute(ddl.staging_ddl(stg, columns))
            prod_cols = [
                {"column_name": c,
                 "target_data_type": casts.normalize(column_types.get(c)),
                 "is_nullable": c not in required}
                for c in columns
            ]
            cur.execute(ddl.production_ddl(prd, prod_cols, nk=nk_flag))
            cur.execute(ddl.quarantine_ddl(qtbl, stg))
            cur.execute(ddl.waiting_ddl(wtbl, prd))   # LIKE production ⇒ inherits nk
            if nk_flag:
                cur.execute(ddl.nk_index_ddl(prd))
                cur.execute(ddl.nk_index_ddl(wtbl))

            if rdbms_sidecar is not None:
                sf = rdbms_sidecar.get("static_filter")
                cur.execute(
                    """INSERT INTO registry_rdbms_source
                         (source_id, table_name, connection_id, source_schema,
                          source_table, tenure_column, static_filter)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (source_id, table_name) DO UPDATE SET
                         connection_id = EXCLUDED.connection_id,
                         source_schema = EXCLUDED.source_schema,
                         source_table  = EXCLUDED.source_table,
                         tenure_column = EXCLUDED.tenure_column,
                         static_filter = EXCLUDED.static_filter""",
                    (source_id, table_name, rdbms_sidecar["connection_id"],
                     rdbms_sidecar["source_schema"], rdbms_sidecar["source_table"],
                     rdbms_sidecar.get("tenure_column"),
                     Json(sf) if sf else None),
                )

    return TableConfig(
        source_id, table_name, stg, prd, load_type,
        existence_check_column, load_recurrence, "ACTIVE", nk_str,
    )


def get_table(source_id: str, table_name: str) -> TableConfig:
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT source_id, table_name, staging_target, production_target,
                      load_type, existence_check_column, load_recurrence, status,
                      natural_key
               FROM registry_tables WHERE source_id = %s AND table_name = %s""",
            (source_id, table_name),
        )
        row = cur.fetchone()
    if row is None:
        raise RegistryError(f"{source_id}.{table_name} is not registered")
    return TableConfig(**row)


def get_rdbms_source(source_id: str, table_name: str) -> dict | None:
    """The ``registry_rdbms_source`` row for a pipeline, or ``None`` for a file
    source."""
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT connection_id, source_schema, source_table,
                      tenure_column, static_filter
               FROM registry_rdbms_source
               WHERE source_id = %s AND table_name = %s""",
            (source_id, table_name),
        )
        return cur.fetchone()


def get_columns(source_id: str, table_name: str) -> list[str]:
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT column_name FROM registry_columns
               WHERE source_id = %s AND table_name = %s AND effective_to IS NULL
               ORDER BY column_order""",
            (source_id, table_name),
        )
        return [r["column_name"] for r in cur.fetchall()]


def get_columns_meta(source_id: str, table_name: str) -> list[dict]:
    """column_name, target_data_type, is_nullable — in column order."""
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT column_name, target_data_type, is_nullable
               FROM registry_columns
               WHERE source_id = %s AND table_name = %s AND effective_to IS NULL
               ORDER BY column_order""",
            (source_id, table_name),
        )
        return cur.fetchall()


def list_tables() -> list[dict]:
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT s.source_id, t.table_name, t.load_type, t.existence_check_column,
                      t.load_recurrence, t.status, s.source_type
               FROM registry_tables t JOIN registry_sources s USING (source_id)
               ORDER BY s.source_id, t.table_name"""
        )
        return cur.fetchall()


# --------------------------------------------------------------------------- #
# deregister — the safe teardown of an onboarded source/table
# --------------------------------------------------------------------------- #

# control-plane rows keyed on (source_id, table_name); order matters for FKs
# (registry_columns -> registry_tables; staging_file_control -> registry_sources).
_DEREG_TABLES = [
    "quarantine_batch_log",
    "waiting_batch_log",
    "registry_columns",
    "registry_change_log",
    "staging_file_control",
]


def deregister_plan(
    source_id: str, table_name: str, *,
    physical_only: bool = False, keep_history: bool = False,
) -> dict:
    """What :func:`deregister_source` would do — a read-only preview. Returns
    ``{cfg, statements, counts}`` where ``statements`` is a list of human-readable
    SQL lines and ``counts`` the current live row counts of each physical table.
    """
    cfg = get_table(source_id, table_name)  # raises RegistryError if unknown
    q = quarantine_target(table_name)
    w = waiting_target(table_name)

    where = f"WHERE source_id = '{source_id}' AND table_name = '{table_name}'"
    stmts = [
        f"DROP TABLE IF EXISTS {cfg.staging_target};",
        f"DROP TABLE IF EXISTS {cfg.production_target} CASCADE;  -- also its load_date partitions",
        f"DROP TABLE IF EXISTS {q};",
        f"DROP TABLE IF EXISTS {w};",
    ]
    if not physical_only:
        width = max(len(t) for t in _DEREG_TABLES + ["run_log", "registry_tables"])
        for t in _DEREG_TABLES:
            stmts.append(f"DELETE FROM {t.ljust(width)} {where};")
        if not keep_history:
            stmts.append(f"DELETE FROM {'run_log'.ljust(width)} {where};")
        stmts.append(f"DELETE FROM {'registry_tables'.ljust(width)} {where};")
        stmts.append(f"DELETE FROM registry_sources WHERE source_id = '{source_id}';"
                     "  -- only if no tables remain for it")

    counts: dict[str, int | None] = {}
    with connection() as conn, conn.cursor() as cur:
        for label, name in (("staging", cfg.staging_target),
                            ("production", cfg.production_target),
                            ("quarantine", q), ("waiting", w)):
            try:
                cur.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(qname(name)))
                counts[label] = cur.fetchone()["n"]
            except Exception:  # noqa: BLE001 — table already gone
                conn.rollback()
                counts[label] = None
    return {"cfg": cfg, "statements": stmts, "counts": counts,
            "physical_only": physical_only, "keep_history": keep_history}


def deregister_source(
    source_id: str, table_name: str, *,
    physical_only: bool = False, keep_history: bool = False,
) -> dict:
    """Drop the four physical tables for a source/table and (unless
    ``physical_only``) remove its control-plane rows, in one transaction under
    the per-table lock. If it was the source's last table the ``registry_sources``
    row goes too. Returns ``{dropped: [...], deleted: {table: n}}``.
    """
    cfg = get_table(source_id, table_name)
    q = quarantine_target(table_name)
    w = waiting_target(table_name)
    dropped: list[str] = []
    deleted: dict[str, int] = {}

    with connection() as conn, conn.transaction(), conn.cursor() as cur:
        lock(cur, cfg.staging_target)  # serialise against a running transform
        for stmt in (
            sql.SQL("DROP TABLE IF EXISTS {}").format(qname(cfg.staging_target)),
            sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(qname(cfg.production_target)),
            sql.SQL("DROP TABLE IF EXISTS {}").format(qname(q)),
            sql.SQL("DROP TABLE IF EXISTS {}").format(qname(w)),
        ):
            cur.execute(stmt)
            dropped.append(stmt.as_string(conn))

        if not physical_only:
            targets = list(_DEREG_TABLES)
            if not keep_history:
                targets.append("run_log")
            for t in targets:
                cur.execute(
                    sql.SQL("DELETE FROM {} WHERE source_id = %s AND table_name = %s")
                    .format(sql.Identifier(t)),
                    (source_id, table_name),
                )
                deleted[t] = cur.rowcount
            cur.execute(
                "DELETE FROM registry_tables WHERE source_id = %s AND table_name = %s",
                (source_id, table_name),
            )
            deleted["registry_tables"] = cur.rowcount
            cur.execute(
                "SELECT count(*) AS n FROM registry_tables WHERE source_id = %s",
                (source_id,),
            )
            if cur.fetchone()["n"] == 0:
                cur.execute("DELETE FROM registry_sources WHERE source_id = %s",
                            (source_id,))
                deleted["registry_sources"] = cur.rowcount

    return {"dropped": dropped, "deleted": deleted,
            "physical_only": physical_only, "keep_history": keep_history}


# --------------------------------------------------------------------------- #
# production column types  (the base-build CAST_SCHEMA — spec §8/§10)
# --------------------------------------------------------------------------- #

def _validate_types(
    column_types: dict[str, str], required: set[str], columns: list[str]
) -> None:
    known = set(columns)
    for name, tok in column_types.items():
        if name not in known:
            raise RegistryError(f"type set for unknown column {name!r}")
        if tok not in casts.TOKENS:
            raise RegistryError(
                f"column {name!r}: unknown type {tok!r} — one of {casts.TOKENS}"
            )
    for name in required:
        if name not in known:
            raise RegistryError(f"required flag on unknown column {name!r}")


def retype_table(
    source_id: str,
    table_name: str,
    column_types: dict[str, str],
    required: set[str] | None = None,
) -> dict:
    """Re-cast production columns to new `casts` tokens, migrating existing data.

    One transaction under the per-table blocking lock:
      1. every proposed cast is test-run over the current production + waiting
         values; a single value that will not cast aborts the whole thing with
         the column name and up to 5 offending samples — nothing changes.
      2. otherwise production is rebuilt with the new DDL, the data is moved
         across with the casts applied, waiting is rebuilt LIKE it, and the
         registry rows are updated (+ a TYPE_CHANGE change-log row per column).

    `column_types` is the full column -> token map; `required` the full set of
    non-empty columns. Returns {changed: [...], rows_migrated: n}.
    """
    required = set(required or ())
    cfg = get_table(source_id, table_name)
    meta = get_columns_meta(source_id, table_name)
    source_cols = [c["column_name"] for c in meta]

    _validate_types(column_types, required, source_cols)

    cur_tok = {c["column_name"]: casts.normalize(c["target_data_type"]) for c in meta}
    cur_req = {c["column_name"] for c in meta if not c["is_nullable"]}
    new_tok = {c: casts.normalize(column_types.get(c, cur_tok[c])) for c in source_cols}

    changed = [
        c for c in source_cols
        if new_tok[c] != cur_tok[c] or ((c in required) != (c in cur_req))
    ]
    if not changed:
        return {"changed": [], "rows_migrated": 0, "note": "no changes"}

    key_cols = cfg.natural_key_columns
    bad_key = [c for c in key_cols if new_tok[c] in ddl.NK_FORBIDDEN_TOKENS]
    if bad_key:
        raise RegistryError(
            f"cannot retype natural-key column(s) {bad_key} to a numeric type — "
            "it would break the composite key"
        )
    new_key_pg_types = [casts.pg_type(new_tok[c]) for c in key_cols]

    prd = cfg.production_target
    new_prd = f"{prd}__new"
    prd_i = qname(prd)
    wq = qname(cfg.waiting_target)

    # --- preconditions ---------------------------------------------------
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM quarantine_batch_log "
            "WHERE source_id=%s AND table_name=%s AND resolution_status='open'",
            (source_id, table_name),
        )
        if cur.fetchone()["n"]:
            raise RegistryError("resolve the open quarantine batches before re-typing")
        cur.execute(
            "SELECT count(*) AS n FROM waiting_batch_log "
            "WHERE source_id=%s AND table_name=%s AND status='pending'",
            (source_id, table_name),
        )
        if cur.fetchone()["n"]:
            raise RegistryError("resolve the pending waiting batches before re-typing")

    def _empty_pred(col: str) -> sql.Composed:
        c = sql.Identifier(col)
        return sql.SQL("{c} IS NULL OR btrim({c}::text) = ''").format(c=c)

    with connection() as conn, conn.transaction(), conn.cursor() as cur:
        lock(cur, cfg.staging_target)

        cur.execute(
            sql.SQL("SELECT count(*) AS n FROM {}").format(qname(cfg.staging_target))
        )
        if cur.fetchone()["n"]:
            raise RegistryError("staging is not empty — run or clear the pending load first")

        # 1. test-cast every changing column over production + waiting
        for col in changed:
            tok = new_tok[col]
            guard = casts.guard_sql(sql.Identifier(col), tok)
            if guard is not None:
                bad = sql.SQL(
                    "{c} IS NOT NULL AND btrim({c}::text) <> '' AND NOT ({g})"
                ).format(c=sql.Identifier(col), g=guard)
                for tref in (prd_i, wq):
                    cur.execute(
                        sql.SQL("SELECT count(*) AS n FROM {t} WHERE {p}").format(t=tref, p=bad)
                    )
                    n = cur.fetchone()["n"]
                    if n:
                        cur.execute(
                            sql.SQL(
                                "SELECT DISTINCT {c}::text AS v FROM {t} WHERE {p} LIMIT 5"
                            ).format(c=sql.Identifier(col), t=tref, p=bad)
                        )
                        egs = ", ".join(repr(r["v"]) for r in cur.fetchall())
                        raise RegistryError(
                            f"column {col!r}: {n} value(s) will not cast to {tok} — e.g. {egs}"
                        )
            if col in required and col not in cur_req:
                for tref in (prd_i, wq):
                    cur.execute(
                        sql.SQL("SELECT count(*) AS n FROM {t} WHERE {p}").format(
                            t=tref, p=_empty_pred(col)
                        )
                    )
                    if cur.fetchone()["n"]:
                        raise RegistryError(
                            f"column {col!r}: has empty value(s) — cannot mark required"
                        )

        # 2. rebuild production with the new types, move the data across
        new_meta = [
            {"column_name": c, "target_data_type": new_tok[c], "is_nullable": c not in required}
            for c in source_cols
        ]
        cur.execute(ddl.production_ddl(new_prd, new_meta, nk=bool(key_cols)))

        cur.execute(
            sql.SQL("SELECT DISTINCT load_date FROM {} WHERE load_date IS NOT NULL").format(prd_i)
        )
        dates = [r["load_date"] for r in cur.fetchall()]
        for d in dates:
            cur.execute(ddl.production_partition_ddl(new_prd, d.isoformat()))

        meta_names = [n for n, _ in PRODUCTION_META]
        all_cols = source_cols + meta_names + (["nk"] if key_cols else [])
        select_list = [casts.cast_select(sql.Identifier(c), new_tok[c]) for c in source_cols] + [
            sql.Identifier(n) for n in meta_names
        ] + ([ddl.natural_key_sql(None, key_cols, new_key_pg_types)] if key_cols else [])
        cur.execute(
            sql.SQL("INSERT INTO {new} ({cols}) SELECT {sel} FROM {old}").format(
                new=qname(new_prd),
                cols=sql.SQL(", ").join(sql.Identifier(c) for c in all_cols),
                sel=sql.SQL(", ").join(select_list),
                old=prd_i,
            )
        )
        migrated = cur.rowcount

        cur.execute(sql.SQL("DROP TABLE {} CASCADE").format(prd_i))
        # RENAME TO takes a bare name (can't move schema); new_prd is already in
        # the right schema, we just strip the "__new" suffix.
        cur.execute(sql.SQL("ALTER TABLE {} RENAME TO {}").format(
            qname(new_prd), sql.Identifier(bare(prd))))
        for d in dates:
            tag = "p" + d.isoformat().replace("-", "")
            cur.execute(
                sql.SQL("ALTER TABLE {} RENAME TO {}").format(
                    qname(f"{new_prd}_{tag}"), sql.Identifier(f"{bare(prd)}_{tag}")
                )
            )

        # waiting is empty (precondition) — just rebuild it LIKE the new production
        cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(wq))
        cur.execute(ddl.waiting_ddl(cfg.waiting_target, prd))
        if key_cols:
            cur.execute(ddl.nk_index_ddl(prd))
            cur.execute(ddl.nk_index_ddl(cfg.waiting_target))

        # 3. registry
        for col in changed:
            cur.execute(
                "UPDATE registry_columns SET target_data_type=%s, is_nullable=%s "
                "WHERE source_id=%s AND table_name=%s AND column_name=%s",
                (new_tok[col], col not in required, source_id, table_name, col),
            )
            cur.execute(
                """INSERT INTO registry_change_log
                     (source_id, table_name, column_name, change_type,
                      old_value, new_value, changed_by)
                   VALUES (%s, %s, %s, 'TYPE_CHANGE', %s, %s, 'retype')""",
                (source_id, table_name, col, cur_tok[col], new_tok[col]),
            )
        cur.execute(
            "UPDATE registry_tables SET last_updated = now() "
            "WHERE source_id=%s AND table_name=%s",
            (source_id, table_name),
        )

    return {"changed": changed, "rows_migrated": migrated}
