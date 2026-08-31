"""Apply ordered SQL migrations from `sql/*.sql`.

Each file runs once, inside its own transaction. Applied files are tracked in
`schema_migrations`. Re-running is a no-op.
"""

from __future__ import annotations

import sys

import psycopg

from odin.config import settings

_TRACK_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


def migrate() -> list[str]:
    """Apply any pending migrations. Returns the filenames applied this run."""
    files = sorted(p for p in settings.sql_dir.glob("*.sql"))
    applied: list[str] = []

    with psycopg.connect(settings.database_url, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(_TRACK_TABLE)
        conn.commit()

        with conn.cursor() as cur:
            cur.execute("SELECT filename FROM schema_migrations")
            done = {r[0] for r in cur.fetchall()}

        for path in files:
            if path.name in done:
                continue
            sql = path.read_text()
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,)
                )
            conn.commit()
            applied.append(path.name)

    return applied


def main() -> None:
    applied = migrate()
    if applied:
        print("applied:", ", ".join(applied))
    else:
        print("up to date")


if __name__ == "__main__":
    sys.exit(main())
