"""Identifier helpers. Keeps generated SQL identifiers safe and predictable.

Physical tables are named for the **table name the DE gave** — normalised to a
safe slug — and live in a schema per layer:

    staging.<table>   production.<table>   quarantine.<table>   waiting.<table>

The source is registry metadata, not part of the physical name. Names stored in
`registry_tables.staging_target` / `production_target` are the fully-qualified
`schema.table` strings; :func:`qname` turns one into a safe identifier. A name
with no dot (pre-schema-per-layer pipelines) resolves unqualified — i.e. in
`public` — so existing pipelines keep working untouched.
"""

from __future__ import annotations

import re

from psycopg import sql

_SLUG_RE = re.compile(r"[^a-z0-9]+")

STAGING_SCHEMA = "staging"
PRODUCTION_SCHEMA = "production"
QUARANTINE_SCHEMA = "quarantine"
WAITING_SCHEMA = "waiting"


def slug(text: str) -> str:
    """Lowercase, non-alnum -> underscore, trimmed. `"ERP Sales!"` -> `"erp_sales"`."""
    s = _SLUG_RE.sub("_", text.strip().lower()).strip("_")
    if not s:
        raise ValueError(f"cannot slugify {text!r}")
    if s[0].isdigit():
        s = f"_{s}"
    return s


def qname(qualified: str) -> sql.Composed:
    """``'staging.customers'`` -> a safe ``"staging"."customers"`` identifier.
    A dotless name -> a bare identifier (resolves via search_path, i.e. public)."""
    return sql.SQL(".").join(sql.Identifier(p) for p in qualified.split("."))


def split_qual(qualified: str) -> tuple[str | None, str]:
    """``'staging.customers'`` -> ``('staging', 'customers')``; ``'x'`` -> ``(None, 'x')``."""
    if "." in qualified:
        schema, table = qualified.split(".", 1)
        return schema, table
    return None, qualified


def bare(qualified: str) -> str:
    """The table part, unqualified: ``'staging.customers'`` -> ``'customers'``."""
    return split_qual(qualified)[1]


def staging_target(table_name: str) -> str:
    return f"{STAGING_SCHEMA}.{slug(table_name)}"


def production_target(table_name: str) -> str:
    return f"{PRODUCTION_SCHEMA}.{slug(table_name)}"


def quarantine_target(table_name: str) -> str:
    return f"{QUARANTINE_SCHEMA}.{slug(table_name)}"


def waiting_target(table_name: str) -> str:
    return f"{WAITING_SCHEMA}.{slug(table_name)}"
