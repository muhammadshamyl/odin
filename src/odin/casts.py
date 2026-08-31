"""Production column cast types — the base-build ``CAST_SCHEMA`` (spec §8, §10).

Staging is all text. When a production table is created the DE assigns each
column a *cast token* from `TOKENS`; the transform then casts text -> that type
set-based, diverting rows that fail to ``quarantine`` with reason ``cast:<col>``.

Range-checked tokens (`nonneg_int`, `unit_interval`) resolve to SQL DOMAINs
created by ``sql/002_try_cast.sql`` so the bound is enforced inside the
non-throwing ``odin_try_cast`` probe and permanently by the column type.
"""

from __future__ import annotations

from psycopg import sql

# token -> (postgres type / domain, is-a-number?)
_SPEC: dict[str, tuple[str, bool]] = {
    "text":          ("text",               False),
    "int":           ("integer",            True),
    "nonneg_int":    ("odin_nonneg_int",     True),
    "bigint":        ("bigint",              True),
    "numeric":       ("numeric",             True),
    "unit_interval": ("odin_unit_interval",  True),
    "boolean":       ("boolean",             False),
    "date":          ("date",                False),
    "timestamptz":   ("timestamptz",         False),
}

TOKENS: tuple[str, ...] = tuple(_SPEC)
DEFAULT = "text"

# human labels for the onboarding <select>
LABELS: dict[str, str] = {
    "text":          "text",
    "int":           "integer",
    "nonneg_int":    "integer ≥ 0  (count / quantity)",
    "bigint":        "bigint",
    "numeric":       "numeric  (exact decimal)",
    "unit_interval": "float in [0, 1]  (confidence)",
    "boolean":       "boolean",
    "date":          "date",
    "timestamptz":   "timestamp (tz)",
}

PG_TYPE: dict[str, str] = {t: v[0] for t, v in _SPEC.items()}


def normalize(token: str | None) -> str:
    """Any unknown / legacy value collapses to ``text``."""
    return token if token in _SPEC else DEFAULT


def pg_type(token: str | None) -> str:
    return PG_TYPE[normalize(token)]


def is_numeric(token: str | None) -> bool:
    return _SPEC[normalize(token)][1]


def guard_sql(col: sql.Composable, token: str | None) -> sql.Composed | None:
    """Predicate that is TRUE when ``col``'s text value is a valid instance of
    ``token`` (or NULL). ``None`` for ``text`` — nothing type-specific to check.

    The whole check (cast + any range bound) happens inside ``odin_try_cast``,
    so this never raises for a bad value.
    """
    tok = normalize(token)
    if tok == "text":
        return None
    return sql.SQL("odin_try_cast({c}::text, {t})").format(
        c=col, t=sql.Literal(PG_TYPE[tok])
    )


def cast_select(col: sql.Composable, token: str | None) -> sql.Composable:
    """``col::<type>`` for a typed token; ``col`` unchanged for ``text``.
    Safe to use only on rows that have already passed :func:`guard_sql`.
    """
    tok = normalize(token)
    if tok == "text":
        return col
    return sql.SQL("{c}::{t}").format(c=col, t=sql.SQL(PG_TYPE[tok]))
